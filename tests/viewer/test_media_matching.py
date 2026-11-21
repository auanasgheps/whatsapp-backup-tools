"""Tests for wab_viewer."""

from __future__ import annotations

import os

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

import os
import re
import sqlite3

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
    _get_app_js,
    _make_android_app,
    make_cache_db,
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
            INSERT INTO message VALUES (1, 1, 0, 1, 1700000000000, 'Hello', 0);
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

        # Gallery grid returns both the archived HD photo and genuine missing photo (LQ parent excluded)
        assert len(gallery) == 2
        gallery_ids = {item["msg_id"] for item in gallery}
        assert gallery_ids == {101, 102}
        hd_item = next(item for item in gallery if item["msg_id"] == 101)
        missing_item = next(item for item in gallery if item["msg_id"] == 102)
        assert hd_item["archive_path"] == "photos/hd.jpg"
        assert missing_item["archive_path"] is None
        assert missing_item["is_missing"] == 1

        # Gallery with types filter also deduplicates and returns the 2 images
        gallery_types = client.get("/api/media?chat_id=10&chat_type=group&types=image").get_json()
        assert len(gallery_types) == 2
        assert {item["msg_id"] for item in gallery_types} == {101, 102}


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
        js = _get_app_js()
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

    def test_gallery_pills_allow_images_and_videos_selected_together(self):
        """app.js must allow images and videos to be selected together via gallery stat pills."""
        js = _get_app_js()
        assert "PRIMARY_MEDIA_TYPES" in js
        match = re.search(r"PRIMARY_MEDIA_TYPES\s*=\s*new\s+Set\(\[([^\]]+)\]\)", js)
        assert match, "PRIMARY_MEDIA_TYPES Set definition missing in app.js"
        types = [t.strip().strip("'\"") for t in match.group(1).split(",")]
        assert "image" in types
        assert "video" in types

        # Ensure multi-selection logic exists in _toggleTypeFilter
        assert "PRIMARY_MEDIA_TYPES.has(t)" in js
        assert "activeTypes.add(type)" in js
        assert "activeTypes.delete(type)" in js

    def test_gallery_default_stat_pills_selection_state(self):
        """app.js must visually mark active media types (image + video) as active,
        not the total pill, so the visual focus matches the default selection."""
        js = _get_app_js()
        assert "pill.classList.toggle('active', isShowAll)" in js
        assert "pill.classList.toggle('active', activeTypes.has(t))" in js
        assert "pill.classList.toggle('dimmed', !activeTypes.has(t))" in js

    def test_gallery_total_pill_switching_initiates_unified_stream_and_avoids_stale_window_leakage(
        self,
    ):
        """app.js must transition cleanly from default (photos+videos) to unified all-media stream
        when clicking the Total stat pill, ensuring non-photo/non-video items are loaded for
        the existing window rather than leaving temporal holes.
        """
        js = _get_app_js()
        assert "allMediaStreamLoaded" in js
        assert "streamHasFilteredTail" in js
        assert "allMediaStreamLoaded && !streamHasFilteredTail" in js
        assert "Loading all items" in js
        assert "allMediaStreamLoaded = true" in js

    def test_gallery_link_viewport_hydration_and_stream_consolidation(self):
        """Gallery link cards must use tight viewport-bound hydration, avoid forced layout thrashing,
        retain thumbnails on scroll-out, and maintain single-stream pagination."""
        js = _get_app_js()
        assert "galleryLinkObserver" in js
        assert "rootMargin: '40px 0px 40px 0px'" in js
        assert ':not([data-type="link"])' in js
        assert "activeTypes.size === 1 && activeTypes.has('link')" in js
        assert "linksLoadingMore || linksAllLoaded" in js
        assert "galleryLoadingMore || linksLoadingMore" in js

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


class TestVoiceWaveform:
    """Tests for voice message waveform extraction, serialization, and cache persistence."""

    def test_android_waveform_extraction_and_serialization(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)

        wa_conn.executescript("""
            CREATE TABLE IF NOT EXISTS audio_data (
                message_row_id INTEGER PRIMARY KEY,
                waveform BLOB
            );
            INSERT INTO jid (_id, user, raw_string) VALUES (1, '123456789', '123456789@s.whatsapp.net');
            INSERT INTO chat (_id, jid_row_id, sort_timestamp) VALUES (1, 1, 1700000000000);
            INSERT INTO message (_id, chat_row_id, from_me, timestamp, message_type, text_data, key_id)
            VALUES (301, 1, 0, 1700000001000, 2, 'PTT voice note', 'WAVE_KEY_1');
            INSERT INTO message_media (message_row_id, chat_row_id, file_path, mime_type)
            VALUES (301, 1, 'WhatsApp/Media/WhatsApp Voice Notes/202601/PTT-20260101-WA0001.opus', 'audio/ogg; codecs=opus');
        """)
        dummy_wave = bytes([i % 100 for i in range(64)])
        wa_conn.execute(
            "INSERT INTO audio_data (message_row_id, waveform) VALUES (301, ?)", (dummy_wave,)
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=123456789&chat_type=contact")
            assert resp.status_code == 200
            data = resp.get_json()
            assert len(data) == 1
            msg = data[0]
            assert msg["msg_id"] == 301
            assert msg["is_voice_note"] == 1
            assert isinstance(msg["waveform"], list)
            assert len(msg["waveform"]) == 64
            assert msg["waveform"] == list(dummy_wave)

    def test_waveform_fallback_when_audio_data_missing(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)

        wa_conn.executescript("""
            INSERT INTO jid (_id, user, raw_string) VALUES (1, '123456789', '123456789@s.whatsapp.net');
            INSERT INTO chat (_id, jid_row_id, sort_timestamp) VALUES (1, 1, 1700000000000);
            INSERT INTO message (_id, chat_row_id, from_me, timestamp, message_type, text_data, key_id)
            VALUES (302, 1, 0, 1700000001000, 2, 'PTT note without wave table', 'WAVE_KEY_2');
        """)
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=123456789&chat_type=contact")
            assert resp.status_code == 200
            data = resp.get_json()
            assert len(data) == 1
            msg = data[0]
            assert msg["msg_id"] == 302
            assert msg["waveform"] is None

    def test_recent_messages_cache_persists_waveform(self, tmp_path):
        archive_path = tmp_path / ".wa_media_archiver.db"
        archive_conn = make_archive_db(archive_path)
        dummy_wave = bytes([5, 10, 25, 50, 75, 100] + [15] * 58)
        archive_conn.execute(
            """
            INSERT INTO recent_messages
            (chat_id, chat_type, msg_id, timestamp_ms, sender, from_me, archive_path, media_type, media_name, text_body, waveform)
            VALUES ('123456789', 'contact', 303, 1700000000000, 'Alice', 0, 'WhatsApp Voice Notes/voice.opus', 'audio', 'Voice', '', ?)
        """,
            (dummy_wave,),
        )
        archive_conn.commit()
        archive_conn.close()

        wa_conn = make_android_db(tmp_path / "msgstore.db")
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=123456789&chat_type=contact")
            assert resp.status_code == 200
            data = resp.get_json()
            assert len(data) == 1
            msg = data[0]
            assert msg["msg_id"] == 303
            assert msg["waveform"] == list(dummy_wave)

    def test_gallery_api_media_with_audio_waveform_bytes(self, tmp_path):
        """Ensure /api/media endpoints do not crash with TypeError when audio_waveform BLOB is present."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)

        wa_conn.executescript("""
            CREATE TABLE IF NOT EXISTS audio_data (
                message_row_id INTEGER PRIMARY KEY,
                waveform BLOB
            );
            INSERT INTO jid (_id, user, raw_string) VALUES (1, '123456789', '123456789@s.whatsapp.net');
            INSERT INTO chat (_id, jid_row_id, sort_timestamp) VALUES (1, 1, 1700000000000);
            INSERT INTO message (_id, chat_row_id, from_me, timestamp, message_type, text_data, key_id)
            VALUES (401, 1, 0, 1700000001000, 2, 'Voice note with wave', 'WAVE_KEY_401');
            INSERT INTO message_media (message_row_id, chat_row_id, file_path, mime_type)
            VALUES (401, 1, 'WhatsApp/Media/WhatsApp Voice Notes/202601/PTT-401.opus', 'audio/ogg; codecs=opus');
        """)
        dummy_wave = bytes([i % 80 for i in range(64)])
        wa_conn.execute(
            "INSERT INTO audio_data (message_row_id, waveform) VALUES (401, ?)", (dummy_wave,)
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            # Default media query
            resp = client.get("/api/media?chat_id=123456789&chat_type=contact")
            assert resp.status_code == 200
            data = resp.get_json()
            assert len(data) == 1
            assert data[0]["msg_id"] == 401
            assert "audio_waveform" not in data[0]

            # types=voice
            resp_voice = client.get("/api/media?chat_id=123456789&chat_type=contact&types=voice")
            assert resp_voice.status_code == 200
            data_voice = resp_voice.get_json()
            assert len(data_voice) == 1
            assert data_voice[0]["msg_id"] == 401

            # types=all
            resp_all = client.get("/api/media?chat_id=123456789&chat_type=contact&types=all")
            assert resp_all.status_code == 200
            assert len(resp_all.get_json()) == 1

            # /api/media/documents
            resp_docs = client.get("/api/media/documents?chat_id=123456789&chat_type=contact")
            assert resp_docs.status_code == 200

            # /api/media/links
            resp_links = client.get("/api/media/links?chat_id=123456789&chat_type=contact")
            assert resp_links.status_code == 200

            # Jump to message in chat view via /api/messages/at
            resp_at = client.get(
                "/api/messages/at?chat_id=123456789&chat_type=contact&ts=1700000001000"
            )
            assert resp_at.status_code == 200
            data_at = resp_at.get_json()
            assert len(data_at) == 1
            assert data_at[0]["msg_id"] == 401
            assert "audio_waveform" not in data_at[0]
            assert data_at[0]["waveform"] == list(dummy_wave)
