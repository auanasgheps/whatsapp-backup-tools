"""Comprehensive edge-case and boundary tests for wab_viewer.hydration.

Tests all uncovered fallback paths, error handling, table absence cascades,
chunking logic, and protobuf wire decoding using 100% synthetic data.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tests.conftest import make_archive_db
from tests.fixtures.protobuf_factory import (
    build_field,
    encode_varint,
    lid_field1_bytes,
)
from wab_viewer import hydration

# ===========================================================================
# 1. Normalization & JID Resolution Edge Cases
# ===========================================================================


class TestNormalizationAndJidResolution:
    """Test message row normalization and iOS JID resolution fallbacks."""

    def test_strip_none_reactions(self) -> None:
        rows = [
            {"msg_id": 1, "reactions": "❤️"},
            {"msg_id": 2, "reactions": None},
            {"msg_id": 3},
        ]
        res = hydration._strip_none_reactions(rows)
        assert res[0]["reactions"] == "❤️"
        assert "reactions" not in res[1]
        assert "reactions" not in res[2]

    def test_normalize_message_rows_waveforms(self) -> None:
        rows = [
            {"msg_id": 1, "audio_waveform": b"\x01\x02\x03", "_raw_metadata": b"meta"},
            {"msg_id": 2, "waveform": [4, 5, 6]},
            {"msg_id": 3, "audio_waveform": "not_bytes"},
            {"msg_id": 4},
        ]
        normalized = hydration._normalize_message_rows(rows)
        assert normalized[0]["waveform"] == [1, 2, 3]
        assert "_raw_metadata" not in normalized[0]
        assert normalized[1]["waveform"] == [4, 5, 6]
        assert normalized[2]["waveform"] is None
        assert normalized[3]["waveform"] is None

    def test_resolve_ios_jid_edge_cases(self) -> None:
        contacts_map = {
            "15550001111@s.whatsapp.net": (None, "15550001111"),
            "103000000000001@lid": ("Alice Lid", None),
            "103000000000002@lid": (None, "15550002222"),
            "15550003333": (None, "15550003333"),
            "103000000000004": (None, "15550004444"),
        }

        # empty JID
        assert hydration._resolve_ios_jid("", contacts_map) == "Someone"
        # WhatsApp official JIDs
        assert hydration._resolve_ios_jid("0@s.whatsapp.net", contacts_map) == "WhatsApp"
        assert hydration._resolve_ios_jid("0", contacts_map) == "WhatsApp"

        # alt_s with pn only
        assert hydration._resolve_ios_jid("15550001111", contacts_map) == "+15550001111"

        # alt_lid with fn only
        assert hydration._resolve_ios_jid("103000000000001", contacts_map) == "Alice Lid"

        # alt_lid with pn only
        assert hydration._resolve_ios_jid("103000000000002", contacts_map) == "+15550002222"

        # bare s.whatsapp.net with pn only
        assert (
            hydration._resolve_ios_jid("15550003333@s.whatsapp.net", contacts_map) == "+15550003333"
        )

        # bare lid with pn only
        assert hydration._resolve_ios_jid("103000000000004@lid", contacts_map) == "+15550004444"

        # unknown JID ending in @lid
        assert hydration._resolve_ios_jid("999999999999999@lid", contacts_map) == "Unknown"

        # unknown digit string
        assert hydration._resolve_ios_jid("15559998888", contacts_map) == "+15559998888"

        # arbitrary non-digit text
        assert hydration._resolve_ios_jid("ArbitraryActor", contacts_map) == "ArbitraryActor"

    def test_split_jid_parts_and_masked_lid(self) -> None:
        cu, s, canon = hydration._split_jid_parts("15550001111:2@s.whatsapp.net")
        assert cu == "15550001111"
        assert s == "s.whatsapp.net"
        assert canon == "15550001111@s.whatsapp.net"

        assert hydration._is_masked_lid_name(None) is True
        assert hydration._is_masked_lid_name("   ") is True
        assert hydration._is_masked_lid_name("Alice ••") is True
        assert hydration._is_masked_lid_name("Bob ∙∙") is True
        assert hydration._is_masked_lid_name("Charlie") is False

    def test_media_type_from_path(self) -> None:
        assert hydration._media_type_from_path(None) == "text"
        assert hydration._media_type_from_path("card.vcf") == "vcard"
        assert hydration._media_type_from_path("sticker.webp") == "sticker"
        assert hydration._media_type_from_path("stk-123.was") == "sticker"
        assert hydration._media_type_from_path("anim.gif") == "gif"
        assert hydration._media_type_from_path("photo.jpg") == "image"
        assert hydration._media_type_from_path("video.mp4") == "video"
        assert hydration._media_type_from_path("audio.opus") == "audio"
        assert hydration._media_type_from_path("doc.pdf") == "document"

    def test_ios_read_without_timestamp(self) -> None:
        assert hydration._ios_read_without_timestamp(8, None) is True
        assert hydration._ios_read_without_timestamp(8, 1700000000) is False
        assert hydration._ios_read_without_timestamp(4, None) is False

    def test_table_exists(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE test_tab (id INT);")
        assert hydration._table_exists(conn, "test_tab", "main") is True
        assert hydration._table_exists(conn, "non_existent_table", "main") is False
        assert hydration._table_exists(conn, "test_tab", "bad_schema") is False
        conn.close()


# ===========================================================================
# 2. iOS & Android Service Events Formatting
# ===========================================================================


class TestServiceEventFormattingEdgeCases:
    """Test error handling and fallback branches in service event formatters."""

    def test_format_ios_service_row_corrupt_json_event12(self) -> None:
        # ev 12 with broken JSON string
        row = {
            "group_event_type": 12,
            "text_body": '{"subject": "Broken',
            "from_me": 1,
            "sender": "Someone",
        }
        res = hydration._format_ios_service_row(row, {})
        assert "You created group" in res

    def test_format_ios_service_row_event12_actor_fallbacks(self) -> None:
        # actor from member_jid
        row1 = {
            "group_event_type": 12,
            "text_body": "Test Group",
            "from_me": 0,
            "member_jid": "15550001111@s.whatsapp.net",
        }
        res1 = hydration._format_ios_service_row(
            row1, {"15550001111@s.whatsapp.net": ("Alice", None)}
        )
        assert "Alice created group" in res1

        # actor from sender
        row2 = {
            "group_event_type": 12,
            "text_body": "Test Group",
            "from_me": 0,
            "sender": "Bob",
        }
        res2 = hydration._format_ios_service_row(row2, {})
        assert "Bob created group" in res2

        # actor fallback to Someone
        row3 = {
            "group_event_type": 12,
            "text_body": "Test Group",
            "from_me": 0,
            "sender": "Someone",
        }
        res3 = hydration._format_ios_service_row(row3, {})
        assert "Someone created group" in res3

    def test_format_ios_service_row_events_21_50_no_semicolon(self) -> None:
        contacts = {
            "15550001111@s.whatsapp.net": ("Alice", None),
            "15550002222@s.whatsapp.net": ("Bob", None),
        }
        # ev 50: Multiple participants added
        row_50 = {
            "group_event_type": 50,
            "text_body": "15550001111@s.whatsapp.net, 15550002222@s.whatsapp.net",
            "from_me": 1,
        }
        res_50 = hydration._format_ios_service_row(row_50, contacts)
        assert "You added Alice and Bob" in res_50

        # ev 21: Contact shared (falls back to Group event)
        row_21 = {
            "group_event_type": 21,
            "text_body": "15550001111@s.whatsapp.net",
            "from_me": 0,
        }
        res_21 = hydration._format_ios_service_row(row_21, contacts)
        assert res_21 == "Group event"

    def test_format_ios_service_row_event7_removals(self) -> None:
        contacts = {
            "15550001111@s.whatsapp.net": ("Alice", None),
            "15550002222@s.whatsapp.net": ("Bob", None),
        }
        # member_jid removed by actor txt
        row1 = {
            "group_event_type": 7,
            "text_body": "15550001111@s.whatsapp.net",
            "member_jid": "15550002222@s.whatsapp.net",
            "from_me": 0,
        }
        res1 = hydration._format_ios_service_row(row1, contacts)
        assert "Alice removed Bob" in res1

        # removed by WhatsApp
        row2 = {
            "group_event_type": 7,
            "text_body": "15550002222@s.whatsapp.net",
            "sender": "WhatsApp",
            "from_me": 0,
        }
        res2 = hydration._format_ios_service_row(row2, contacts)
        assert "WhatsApp removed Bob" in res2

        # from_me == 1 removal
        row3 = {
            "group_event_type": 7,
            "text_body": "15550002222@s.whatsapp.net",
            "from_me": 1,
        }
        res3 = hydration._format_ios_service_row(row3, contacts)
        assert "You removed Bob" in res3

    def test_format_ios_service_row_events_6_56_and_fallbacks(self) -> None:
        # ev 6 phone number changed
        row_ev6 = {
            "group_event_type": 6,
            "text_body": "15550009999",
            "sender": "Alice",
            "from_me": 0,
        }
        res_ev6 = hydration._format_ios_service_row(row_ev6, {})
        assert "Alice changed their phone number to +15550009999" in res_ev6

        # ev 56 security code changed
        row_ev56 = {
            "group_event_type": 56,
            "member_jid": "15550001111@s.whatsapp.net",
            "sender": "",
            "from_me": 0,
        }
        contacts = {"15550001111@s.whatsapp.net": ("Alice", None)}
        res_ev56 = hydration._format_ios_service_row(row_ev56, contacts)
        assert "Your security code with Alice changed" in res_ev56

        # unhandled plain text fallback vs generic Group event
        row_txt = {"group_event_type": 999, "text_body": "System notification message"}
        assert hydration._format_ios_service_row(row_txt, {}) == "System notification message"

        row_generic = {"group_event_type": 999, "text_body": "123456"}
        assert hydration._format_ios_service_row(row_generic, {}) == "Group event"

    def test_format_android_service_row_deleted_in_contact_chat(self) -> None:
        # 1-to-1 chat deletion
        row_self = {
            "chat_type": "contact",
            "action_type": 1,
            "from_me": 1,
        }
        assert hydration._format_android_service_row(row_self) == "You deleted this message"

        row_peer = {
            "chat_type": "contact",
            "action_type": 1,
            "from_me": 0,
        }
        assert hydration._format_android_service_row(row_peer) == "This message was deleted"


# ===========================================================================
# 3. Service Rows In-Place Hydration & Schema Errors
# ===========================================================================


class TestServiceRowsHydration:
    """Test _format_service_rows for iOS and Android including missing tables."""

    def test_hydrate_android_service_participants_missing_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        # Table message_system_chat_participant does not exist
        res = hydration._hydrate_android_service_participants(
            conn,
            [1, 2],
            has_mcp=True,
            has_lid_dn=False,
            has_jid_server=False,
            has_jid_raw_string=False,
        )
        assert res == {}
        conn.close()

    def test_format_service_rows_ios_pushname_and_errors(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
            INSERT INTO ZWAPROFILEPUSHNAME VALUES (1, '15550008888@s.whatsapp.net', 'PushUser');
        """)

        # JID variants to trigger normalization: author with bare number and author ending in @s.whatsapp.net
        json_bare = '{"author": "15550007777", "subject": "Subject 1"}'
        json_at = '{"author": "15550006666@s.whatsapp.net", "subject": "Subject 2"}'
        rows = [
            {
                "media_type": "service",
                "group_event_type": 1,
                "text_body": "New Group Subject",
                "member_jid": "15550008888@s.whatsapp.net",
                "from_me": 0,
            },
            {
                "media_type": "service",
                "group_event_type": 12,
                "text_body": json_bare,
                "from_me": 0,
            },
            {
                "media_type": "service",
                "group_event_type": 12,
                "text_body": json_at,
                "from_me": 0,
            },
        ]

        hydration._format_service_rows(
            rows,
            "ios",
            conn,
            has_mcp=False,
            has_lid_dn=False,
            has_jid_server=False,
            has_jid_raw_string=False,
        )
        assert "PushUser changed the subject" in rows[0]["text_body"]
        conn.close()

    def test_format_service_rows_android_metadata_and_deleted(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE message_ephemeral_setting (message_row_id INTEGER PRIMARY KEY, setting_duration INTEGER);
            CREATE TABLE message_system_initial_privacy_provider (message_row_id INTEGER PRIMARY KEY, verified_biz_name TEXT);
            INSERT INTO message_ephemeral_setting VALUES (10, 86400);
            INSERT INTO message_system_initial_privacy_provider VALUES (20, 'Acme Corp');
        """)

        rows = [
            {
                "msg_id": 10,
                "media_type": "service",
                "action_type": 67,
                "text_body": "86400",
                "from_me": 1,
            },
            {
                "msg_id": 20,
                "media_type": "service",
                "action_type": 53,
                "text_body": "",
                "from_me": 0,
            },
            {
                "msg_id": 30,
                "chat_type": "contact",
                "media_type": "service",
                "action_type": 1,
                "from_me": 1,
            },
        ]

        hydration._format_service_rows(
            rows,
            "android",
            conn,
            has_mcp=False,
            has_lid_dn=False,
            has_jid_server=False,
            has_jid_raw_string=False,
        )
        assert rows[0]["service_metadata"]["setting_duration"] == "86400"
        assert rows[1]["service_metadata"]["verified_biz_name"] == "Acme Corp"
        assert rows[2]["media_type"] == "deleted"
        assert rows[2]["text_body"] == "You deleted this message"
        conn.close()


# ===========================================================================
# 4. Reactions & Reaction Caching Edge Cases
# ===========================================================================


class TestReactionsEdgeCases:
    """Test _ios_reactions and _resolve_and_cache_reactions guards."""

    def test_ios_reactions_guards(self) -> None:
        conn = sqlite3.connect(":memory:")
        # Empty msg_ids
        assert hydration._ios_reactions(conn, "1", []) == {}

        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER, ZMESSAGEINFO INTEGER, ZGROUPMEMBER INTEGER, ZFROMJID TEXT);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT);
            CREATE TABLE ZWAMESSAGEINFO (Z_PK INTEGER PRIMARY KEY, ZRECEIPTINFO BLOB);
            INSERT INTO ZWAMESSAGE VALUES (1, 10, 1, 100, NULL, '15550001111@s.whatsapp.net');
            INSERT INTO ZWAMESSAGEINFO VALUES (100, NULL);
        """)
        # Row with null receipt info
        res = hydration._ios_reactions(conn, "10", [1])
        assert res == {}
        conn.close()

    def test_resolve_and_cache_reactions_guards(self) -> None:
        conn = sqlite3.connect(":memory:")
        # source_type != android
        hydration._resolve_and_cache_reactions("ios", conn, conn, [{"msg_id": 1}])

        # rows with msg_id=None
        hydration._resolve_and_cache_reactions("android", conn, conn, [{"msg_id": None}])
        conn.close()


# ===========================================================================
# 5. Poll Hydration Fallbacks & Error Resilience
# ===========================================================================


class TestPollHydrationEdgeCases:
    """Test poll hydration candidates and OperationalError handling."""

    def test_hydrate_poll_rows_candidate_detection_and_addon67(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE message_poll (message_row_id INTEGER PRIMARY KEY, selectable_options_count INTEGER);
            CREATE TABLE message_poll_option (_id INTEGER PRIMARY KEY, message_row_id INTEGER, option_name TEXT, vote_total INTEGER);
            CREATE TABLE message_add_on (_id INTEGER PRIMARY KEY, parent_message_row_id INTEGER, from_me INTEGER, message_add_on_type INTEGER);
            CREATE TABLE message_add_on_poll_vote_selected_option (message_add_on_row_id INTEGER, message_poll_option_id INTEGER);

            INSERT INTO message_poll VALUES (100, 1);
            INSERT INTO message_poll_option VALUES (1, 100, 'Yes', 2);
            INSERT INTO message_poll_option VALUES (2, 100, 'No', 1);

            INSERT INTO message_add_on VALUES (50, 100, 1, 67);
            INSERT INTO message_add_on_poll_vote_selected_option VALUES (50, 1);
        """)

        # Message 100 starts as text, should be promoted to poll
        rows = [{"msg_id": 100, "media_type": "text", "text_body": "Are tests green?"}]
        hydration._hydrate_poll_rows(rows, "android", conn)

        assert rows[0]["media_type"] == "poll"
        assert rows[0]["poll"]["question"] == "Are tests green?"
        assert rows[0]["poll"]["total_votes"] == 3
        assert rows[0]["poll"]["options"][0]["user_selected"] is True
        conn.close()

    def test_hydrate_poll_rows_ios_error_handling(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        # ZWAMESSAGEINFO does not exist
        rows = [{"msg_id": 1, "media_type": "poll"}]
        hydration._hydrate_poll_rows(rows, "ios", conn)

        conn.executescript("""
            CREATE TABLE ZWAMESSAGEINFO (ZMESSAGE INTEGER PRIMARY KEY, ZRECEIPTINFO BLOB);
            INSERT INTO ZWAMESSAGEINFO VALUES (1, NULL);
            INSERT INTO ZWAMESSAGEINFO VALUES (2, X'010203'); -- invalid protobuf
        """)
        rows_bad = [
            {"msg_id": 1, "media_type": "poll"},
            {"msg_id": 2, "media_type": "poll"},
        ]
        hydration._hydrate_poll_rows(rows_bad, "ios", conn)
        assert rows_bad[0].get("poll") is None
        assert rows_bad[1].get("poll") is None
        conn.close()


# ===========================================================================
# 6. Quoted Rows & Android Thumbnail Cascade
# ===========================================================================


class TestQuotedRowsThumbnailCascade:
    """Test thumbnail discovery cascade across 5 tables for Android quoted messages."""

    def test_hydrate_quoted_rows_android_thumbnail_cascade(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE message (_id INTEGER PRIMARY KEY, message_type INTEGER);
            CREATE TABLE message_quoted_media (message_row_id INTEGER PRIMARY KEY, thumbnail BLOB);
            CREATE TABLE message_quoted_text (message_row_id INTEGER PRIMARY KEY, thumbnail BLOB);
            CREATE TABLE message_thumbnail (message_row_id INTEGER PRIMARY KEY, thumbnail BLOB);
            CREATE TABLE media_hash_thumbnail (media_hash TEXT PRIMARY KEY, thumbnail BLOB);
            CREATE TABLE message_media (message_row_id INTEGER PRIMARY KEY, file_hash TEXT);

            INSERT INTO message_quoted_media VALUES (10, X'FFD8');
            INSERT INTO message_quoted_text VALUES (20, X'FFD8');
            INSERT INTO message_thumbnail VALUES (30, X'FFD8');
            INSERT INTO message_media VALUES (40, 'hash1');
            INSERT INTO media_hash_thumbnail VALUES ('hash1', X'FFD8');
            INSERT INTO message VALUES (50, 1); -- Image message type
        """)

        rows = [
            {"msg_id": 1, "quoted_msg_id": 10, "quoted_text": "Quote 1"},
            {"msg_id": 2, "quoted_msg_id": 20, "quoted_text": "Quote 2"},
            {"msg_id": 3, "quoted_msg_id": 30, "quoted_text": "Quote 3"},
            {"msg_id": 4, "quoted_msg_id": 40, "quoted_text": "Quote 4"},
            {"msg_id": 5, "quoted_msg_id": 50, "quoted_text": "Quote 5"},
            {"msg_id": 6, "quoted_msg_id": 99, "quoted_text": "No thumb"},
        ]

        hydration._hydrate_quoted_rows(rows, "android", conn)

        assert rows[0].get("quoted_has_thumb") == 1
        assert rows[1].get("quoted_has_thumb") == 1
        assert rows[2].get("quoted_has_thumb") == 1
        assert rows[3].get("quoted_has_thumb") == 1
        assert rows[4].get("quoted_has_thumb") == 1
        assert "quoted_has_thumb" not in rows[5]
        conn.close()

    def test_hydrate_quoted_rows_ios_sender_resolution(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TEMP TABLE _ios_contacts (jid TEXT PRIMARY KEY, full_name TEXT, phone_number TEXT);
            INSERT INTO _ios_contacts VALUES ('15550001111@s.whatsapp.net', 'Alice', '15550001111');
        """)

        # metadata containing quoted stanza & sender: field 5 = stanza, field 6 = sender, field 19 = sub_msg
        sub_msg = build_field(1, 2, b"Quoted text")
        raw_meta = (
            build_field(5, 2, b"STANZA_QUOTE_1")
            + build_field(6, 2, b"15550001111@s.whatsapp.net")
            + build_field(19, 2, sub_msg)
        )

        rows = [
            {"msg_id": 1, "_raw_metadata": raw_meta, "media_type": "text"},
            {"msg_id": 2, "from_me": 1, "media_type": "text"},
            {"msg_id": 3, "sender": "Bob", "media_type": "text"},
        ]

        hydration._hydrate_quoted_rows(rows, "ios", conn)

        assert rows[0]["quoted_stanza_id"] == "STANZA_QUOTE_1"
        assert rows[0]["quoted_sender"] == "Alice"
        conn.close()


# ===========================================================================
# 7. Mentions Engine, Legacy Tokens & Preserved Chats
# ===========================================================================


class TestMentionsEngineEdgeCases:
    """Test contact chats loading with preserved chats, mention chunking, and resolution."""

    def test_load_contact_chats_with_preserved_chats(self) -> None:
        wa_conn = sqlite3.connect(":memory:")
        wa_conn.executescript("""
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER DEFAULT 0);
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            INSERT INTO jid VALUES (1, '15550001111', 's.whatsapp.net', '15550001111@s.whatsapp.net');
            INSERT INTO chat VALUES (10, 1, NULL, 0);
        """)

        pres_conn = sqlite3.connect(":memory:")
        pres_conn.executescript("""
            CREATE TABLE preserved_chats (chat_id TEXT PRIMARY KEY, chat_type TEXT);
            INSERT INTO preserved_chats VALUES ('15550009999', 'contact');
        """)

        token_to_chat, _ = hydration._load_android_contact_chats(wa_conn, pres_conn)
        assert token_to_chat["15550001111"] == "15550001111"
        assert token_to_chat["15550009999"] == "15550009999"

        # Test iOS contact chats with preserved_chats
        ios_wa = sqlite3.connect(":memory:")
        ios_wa.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZHIDDEN INTEGER DEFAULT 0, ZSESSIONTYPE INTEGER DEFAULT 0);
            INSERT INTO ZWACHATSESSION VALUES (20, '15550002222@s.whatsapp.net', 0, 0);
        """)
        token_to_chat_ios, jid_to_chat_ios = hydration._load_ios_contact_chats(ios_wa, pres_conn)
        assert token_to_chat_ios["15550002222"] == "20"
        assert token_to_chat_ios["15550009999"] == "15550009999"

        wa_conn.close()
        pres_conn.close()
        ios_wa.close()

    def test_resolve_android_mention_tokens_chunking_and_fallbacks(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE message_mentions (message_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE lid_display_name (lid_row_id INTEGER PRIMARY KEY, display_name TEXT);

            INSERT INTO jid VALUES (1, '15550001111', 's.whatsapp.net', '15550001111@s.whatsapp.net');
            INSERT INTO jid VALUES (2, '103000000000002', 'lid', '103000000000002@lid');
            INSERT INTO lid_display_name VALUES (2, 'Bob Clean');

            INSERT INTO message_mentions VALUES (1, 1);
            INSERT INTO message_mentions VALUES (2, 2);
        """)

        # >400 msg_ids to trigger chunking loop
        msg_ids = list(range(1, 450))
        per_msg, _ = hydration._resolve_android_mention_tokens(
            conn, msg_ids, set(), {"15550001111": "chat_1"}
        )

        assert 1 in per_msg
        assert per_msg[1]["15550001111"]["name"] == "+15550001111"
        assert per_msg[1]["15550001111"]["chat_id"] == "chat_1"

        assert 2 in per_msg
        assert per_msg[2]["103000000000002"]["name"] == "Bob Clean"
        conn.close()

    def test_hydrate_mentions_invalid_json_and_preserved(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            INSERT INTO jid VALUES (1, '15550007777', 's.whatsapp.net', '15550007777@s.whatsapp.net');
        """)

        rows = [
            # Invalid JSON string in mentions
            {"msg_id": 1, "mentions": "{invalid_json", "text_body": "Hello"},
            # Preserved message with mention token
            {
                "msg_id": 2,
                "is_preserved": 1,
                "text_body": "Hey @15550007777 check this out",
            },
        ]

        hydration._hydrate_mentions(rows, "android", conn, None)

        assert rows[0].get("mentions") is None
        assert "15550007777" in rows[1]["mentions"]
        assert rows[1]["mentions"]["15550007777"]["name"] == "+15550007777"
        conn.close()


# ===========================================================================
# 8. Live / Motion Photos & Link Previews
# ===========================================================================


class TestLivePhotosAndLinkPreviews:
    """Test link preview extraction and live photo child video identification."""

    def test_hydrate_link_preview_rows_www_extraction(self) -> None:
        rows = [
            {"media_type": "link", "text_body": "Visit www.example.org/news today!"},
            {"media_type": "text"},
        ]
        hydration._hydrate_link_preview_rows(rows, "android")
        assert rows[0]["link_url"] == "https://www.example.org/news"

    def test_hydrate_live_photos_android_child_video(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE message_association (
                parent_message_row_id INTEGER,
                child_message_row_id INTEGER,
                association_type INTEGER
            );
            CREATE TABLE message_media (message_row_id INTEGER PRIMARY KEY, file_path TEXT);
            INSERT INTO message_association VALUES (10, 20, 11);
            INSERT INTO message_media VALUES (20, 'Media/video.mp4');
        """)

        rows = [
            {"msg_id": 10, "media_type": "image"},
            {"msg_id": 20, "media_type": "video"},
        ]

        hydration._hydrate_live_photos(rows, "android", conn)

        assert rows[0].get("is_live_photo") == 1
        assert rows[0]["live_photo_path"] == "Media/video.mp4"
        assert rows[1].get("is_live_photo_video") == 1
        conn.close()

    def test_hydrate_live_photos_ios_child_video(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            ATTACH DATABASE ':memory:' AS ext;
            CREATE TABLE ext.message_parent_association (
                parent_stanza_id TEXT,
                stanza_id TEXT,
                type INTEGER
            );
            CREATE TABLE ZWAMESSAGE (ZSTANZAID TEXT PRIMARY KEY, ZMEDIAITEM INTEGER);
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT);

            INSERT INTO ext.message_parent_association VALUES ('STANZA_PARENT', 'STANZA_CHILD', 12);
            INSERT INTO ZWAMESSAGE VALUES ('STANZA_CHILD', 1);
            INSERT INTO ZWAMEDIAITEM VALUES (1, 'video.mp4');
        """)

        rows = [
            {"stanza_id": "STANZA_PARENT", "media_type": "image"},
            {"stanza_id": "STANZA_CHILD", "media_type": "video"},
        ]

        hydration._hydrate_live_photos(rows, "ios", conn)

        assert rows[0].get("is_live_photo") == 1
        assert rows[0]["live_photo_path"] == "Message/video.mp4"
        assert rows[1].get("is_live_photo_video") == 1
        conn.close()


# ===========================================================================
# 9. Recent Messages Backfill & Cache Invariants
# ===========================================================================


class TestRecentMessagesBackfillEdgeCases:
    """Test _backfill_recent_messages guards and waveform serialization."""

    def test_backfill_recent_messages_guards_and_waveform(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test_archiver.db"
        make_archive_db(db_path)
        conn = sqlite3.connect(str(db_path))

        rows = [
            # Synthetic / preserved message (should be skipped)
            {"msg_id": 1, "is_preserved": 1, "media_type": "text"},
            # Call event (should be skipped)
            {"msg_id": 2, "media_type": "call"},
            # Valid live message with list waveform
            {
                "chat_id": "15550001111@s.whatsapp.net",
                "chat_type": "contact",
                "msg_id": 10,
                "timestamp_ms": 1700000000000,
                "sender": "Alice",
                "from_me": 0,
                "media_type": "audio",
                "text_body": "",
                "waveform": [10, 20, 30],
            },
        ]

        # Pass active connection
        hydration._backfill_recent_messages(conn, rows)

        res = conn.execute(
            "SELECT msg_id, waveform FROM recent_messages WHERE msg_id = 10"
        ).fetchall()
        assert len(res) == 1
        assert res[0][0] == 10
        assert res[0][1] == bytes([10, 20, 30])
        conn.close()

        # Non-existent DB path guard
        hydration._backfill_recent_messages(tmp_path / "missing.db", rows)


# ===========================================================================
# 10. iOS Protobuf Receipts & Multi-Device Rows
# ===========================================================================


class TestReceiptsProtobufEdgeCases:
    """Test iOS ZRECEIPTINFO protobuf wire decoders and device rows aggregation."""

    def test_parse_ios_receipt_blob_wire1_and_wire5_noise(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TEMP TABLE _ios_contacts (jid TEXT PRIMARY KEY, full_name TEXT, phone_number TEXT);
            INSERT INTO _ios_contacts VALUES ('103000000000099@lid', 'LID Contact', '15550009999');
        """)

        # Build member entry with wire 1 (8 bytes) and wire 5 (4 bytes) noise fields
        lid_bytes = lid_field1_bytes("103000000000099")
        entry = (
            build_field(1, 2, lid_bytes)
            + build_field(11, 1, b"\x01\x02\x03\x04\x05\x06\x07\x08")  # wire 1 noise
            + build_field(12, 5, b"\x01\x02\x03\x04")  # wire 5 noise
            + build_field(5, 0, 15)  # read delta = 15s
        )

        # Field 10 delivery event containing wire 1/5 noise
        event = (
            build_field(1, 0, 10)  # delta = 10s
            + build_field(2, 0, 1)
            + build_field(7, 1, b"\xaa\xbb\xcc\xdd\xee\xff\x00\x11")  # wire 1 inside event
        )
        entry += build_field(10, 2, event)

        # Top container: field 3 = base_ts (1700000000), field 2 = member entry
        blob = build_field(3, 0, 1700000000) + build_field(2, 2, entry)

        res_group = hydration._parse_ios_receipt_blob(blob, conn, is_group=True)
        assert len(res_group) == 1
        assert res_group[0]["delivered_ts"] == (1700000000 + 10) * 1000
        assert res_group[0]["read_ts"] == (1700000000 + 15) * 1000

        # 1-to-1 chat aggregation
        res_1to1 = hydration._parse_ios_receipt_blob(blob, conn, is_group=False)
        assert len(res_1to1) == 1
        assert res_1to1[0]["delivered_ts"] == (1700000000 + 10) * 1000
        assert res_1to1[0]["read_ts"] == (1700000000 + 15) * 1000
        conn.close()

    def test_parse_ios_receipt_blob_corrupt_varint(self) -> None:
        # Corrupt varint with continuation bit never terminated
        corrupt_blob = b"\x80\x80\x80\x80\x80"
        conn = sqlite3.connect(":memory:")
        assert hydration._parse_ios_receipt_blob(corrupt_blob, conn, is_group=True) == []
        conn.close()

    def test_parse_ios_receipt_device_rows_aggregation_and_contacts(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TEMP TABLE _ios_contacts (jid TEXT PRIMARY KEY, full_name TEXT, phone_number TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
            CREATE TABLE contacts (number TEXT PRIMARY KEY, display_name TEXT);

            INSERT INTO _ios_contacts VALUES ('15550001111@s.whatsapp.net', 'Alice', '15550001111');
            INSERT INTO ZWAPROFILEPUSHNAME VALUES (1, '15550002222@s.whatsapp.net', 'Bob Push');
        """)

        infra_rows = [
            # User 1 Device A
            {
                "user_jid": "15550001111@s.whatsapp.net",
                "delivered_timestamp": 1700000010,
                "read_timestamp": 1700000025,
                "played_timestamp": None,
            },
            # User 1 Device B
            {
                "user_jid": "15550001111@s.whatsapp.net",
                "delivered_timestamp": 1700000005,  # earlier
                "read_timestamp": 1700000020,  # earlier
                "played_timestamp": 1700000030,
            },
            # User 2 Device A (push name fallback)
            {
                "user_jid": "15550002222@s.whatsapp.net",
                "delivered_timestamp": 1700000015,
                "read_timestamp": None,
                "played_timestamp": None,
            },
        ]

        members = hydration._parse_ios_receipt_device_rows(infra_rows, conn, is_group=True)
        assert len(members) == 2

        m_alice = next(m for m in members if m["jid"] == "15550001111")
        assert m_alice["name"] == "Alice"
        assert m_alice["delivered_ts"] == 1700000005 * 1000
        assert m_alice["read_ts"] == 1700000020 * 1000
        assert m_alice["played_ts"] == 1700000030 * 1000
        assert m_alice["read_known"] is True

        m_bob = next(m for m in members if m["jid"] == "15550002222")
        assert m_bob["name"] == "Bob Push"
        assert m_bob["read_known"] is False

        # 1-to-1 chat aggregation
        one_to_one = hydration._parse_ios_receipt_device_rows(infra_rows[:2], conn, is_group=False)
        assert len(one_to_one) == 1
        assert one_to_one[0]["delivered_ts"] == 1700000005 * 1000
        assert one_to_one[0]["read_ts"] == 1700000020 * 1000
        conn.close()


# ===========================================================================
# 11. Comprehensive Edge Coverage Booster
# ===========================================================================


class TestComprehensiveEdgeCoverage:
    """Target remaining gap lines to maximize test coverage."""

    def test_hydrate_quoted_rows_guards_and_fallbacks(self) -> None:
        # Guard: empty rows
        hydration._hydrate_quoted_rows([], "android", None)
        hydration._hydrate_quoted_rows([], "ios", None)

        # Guard: unknown source_type
        rows = [{"msg_id": 1}]
        hydration._hydrate_quoted_rows(rows, "unknown", None)

        # iOS sender fallbacks when extracted_senders has no contact entry
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "CREATE TEMP TABLE _ios_contacts (jid TEXT PRIMARY KEY, full_name TEXT, phone_number TEXT);"
        )

        sub_msg = build_field(1, 2, b"Quoted text")
        meta1 = (
            build_field(5, 2, b"STANZA_1")
            + build_field(6, 2, b"15550009999@s.whatsapp.net")
            + build_field(19, 2, sub_msg)
        )
        meta2 = (
            build_field(5, 2, b"STANZA_2")
            + build_field(6, 2, b"non_numeric_sender")
            + build_field(19, 2, sub_msg)
        )
        meta3 = build_field(5, 2, b"STANZA_3") + build_field(19, 2, sub_msg)  # no quoted sender

        test_rows = [
            {"msg_id": 1, "_raw_metadata": meta1, "media_type": "text"},
            {"msg_id": 2, "_raw_metadata": meta2, "media_type": "text"},
            {"msg_id": 3, "_raw_metadata": meta3, "from_me": 1, "media_type": "text"},
            {
                "msg_id": 4,
                "_raw_metadata": meta3,
                "sender": "Charlie",
                "from_me": 0,
                "media_type": "text",
            },
        ]

        hydration._hydrate_quoted_rows(test_rows, "ios", conn)
        assert test_rows[0]["quoted_sender"] == "+15550009999"
        assert test_rows[1]["quoted_sender"] == "non_numeric_sender"
        assert test_rows[2]["quoted_sender"] == "You"
        assert test_rows[3]["quoted_sender"] == "Charlie"
        conn.close()

    def test_resolve_android_mention_tokens_additional_paths(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TEMP TABLE _jid_map_resolved (lid_row_id INTEGER PRIMARY KEY, jid_row_id INTEGER);
            CREATE TEMP TABLE _contacts_resolved (clean_number TEXT PRIMARY KEY, display_name TEXT, folder TEXT);
            CREATE TABLE lid_display_name (lid_row_id INTEGER PRIMARY KEY, display_name TEXT);
            CREATE TABLE message_mentions (message_row_id INTEGER, jid_row_id INTEGER);

            INSERT INTO jid VALUES (1, '', 's.whatsapp.net', ''); -- empty cu
            INSERT INTO jid VALUES (2, '15550002222', 's.whatsapp.net', '15550002222@s.whatsapp.net');
            INSERT INTO _contacts_resolved VALUES ('15550002222', NULL, 'FolderContact');

            INSERT INTO jid VALUES (3, '103000000000003', 'lid', '103000000000003@lid');
            INSERT INTO jid VALUES (4, '15550004444', 's.whatsapp.net', '15550004444@s.whatsapp.net');
            INSERT INTO _jid_map_resolved VALUES (3, 4); -- resolves to j2: 15550004444

            INSERT INTO jid VALUES (5, '103000000000005', 'lid', '103000000000005@lid'); -- unknown LID

            INSERT INTO message_mentions VALUES (1, 1);
            INSERT INTO message_mentions VALUES (2, 2);
            INSERT INTO message_mentions VALUES (3, 3);
            INSERT INTO message_mentions VALUES (4, 5);
        """)

        per_msg, _ = hydration._resolve_android_mention_tokens(conn, [1, 2, 3, 4], set(), {})
        assert 1 not in per_msg  # empty cu skipped
        assert per_msg[2]["15550002222"]["name"] == "FolderContact"
        assert per_msg[3]["103000000000003"]["name"] == "+15550004444"
        assert per_msg[4]["103000000000005"]["name"] == "103000000000005 (Unknown)"

        # Legacy tokens > 400 chunking
        leg_tokens = {f"1555000{i:04d}" for i in range(450)}
        _, leg_res = hydration._resolve_android_mention_tokens(conn, [], leg_tokens, {})
        assert isinstance(leg_res, dict)
        conn.close()

    def test_resolve_ios_mention_jids_additional_paths(self) -> None:
        # wa_conn is None guard
        assert hydration._resolve_ios_mention_jids(None, set(), set(), {}, {}) == ({}, {})

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            ATTACH DATABASE ':memory:' AS ext;
            ATTACH DATABASE ':memory:' AS arch;
            CREATE TABLE ext.contact_metadata (contact_lid TEXT, contact_push_name TEXT, contact_pn TEXT);
            CREATE TABLE arch.contacts (number TEXT, display_name TEXT);
            CREATE TEMP TABLE _ios_contacts (jid TEXT PRIMARY KEY, full_name TEXT, phone_number TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);

            INSERT INTO ext.contact_metadata VALUES ('103000000000010', 'ExtPush', '15550001010@s.whatsapp.net');
            INSERT INTO arch.contacts VALUES ('15550002020', 'ArchContact');
            INSERT INTO ZWAPROFILEPUSHNAME VALUES (1, '15550002020@s.whatsapp.net', 'ArchContact');
            INSERT INTO _ios_contacts VALUES ('15550003030@s.whatsapp.net', 'You', '15550003030');
            INSERT INTO _ios_contacts VALUES ('103000000000040@lid', '+15550004040', '15550004040');
            INSERT INTO _ios_contacts VALUES ('15550004040@s.whatsapp.net', 'ResolvedPN', '15550004040');
        """)

        jids = {
            "103000000000010@lid",
            "15550002020@s.whatsapp.net",
            "15550003030@s.whatsapp.net",
            "103000000000040@lid",
            "103000000000099@lid",  # Unknown
        }
        legacy_tokens = {"15550002020"}

        jid_res, leg_res = hydration._resolve_ios_mention_jids(conn, jids, legacy_tokens, {}, {})
        assert jid_res["15550003030@s.whatsapp.net"]["name"] == "You"
        assert jid_res["15550003030@s.whatsapp.net"]["is_self"] is True
        assert jid_res["15550002020@s.whatsapp.net"]["name"] == "ArchContact"
        assert jid_res["103000000000040@lid"]["name"] == "ResolvedPN"
        assert jid_res["103000000000099@lid"]["name"] == "103000000000099 (Unknown)"
        assert "15550002020" in leg_res
        conn.close()

    def test_hydrate_mentions_refresh_and_ios_live_and_preserved(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TEMP TABLE _ios_contacts (jid TEXT PRIMARY KEY, full_name TEXT, phone_number TEXT);
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZCONTACTIDENTIFIER TEXT, ZPARTNERNAME TEXT, ZHIDDEN INTEGER DEFAULT 0, ZSESSIONTYPE INTEGER DEFAULT 0);
            INSERT INTO _ios_contacts VALUES ('15550001111@s.whatsapp.net', 'Alice', '15550001111');
            INSERT INTO ZWACHATSESSION VALUES (10, '15550001111@s.whatsapp.net', NULL, 'Alice', 0, 0);
        """)

        # iOS raw mention in _raw_metadata (field 8 = mention JID)
        meta_with_mention = build_field(8, 2, b"15550001111@s.whatsapp.net")

        rows = [
            # 1. Existing dict mentions refresh
            {
                "msg_id": 1,
                "mentions": {
                    "15550001111": {"name": "Alice", "is_self": False},
                    "self_tok": {"name": "You", "is_self": True},
                },
            },
            # 2. iOS Live raw mentions
            {
                "msg_id": 2,
                "_raw_metadata": meta_with_mention,
                "media_type": "text",
            },
            # 3. iOS Preserved mentions
            {
                "msg_id": 3,
                "is_preserved": 1,
                "text_body": "Hello @15550001111",
            },
        ]

        hydration._hydrate_mentions(rows, "ios", conn, None)

        assert rows[0]["mentions"]["15550001111"]["chat_id"] == "10"
        assert rows[0]["mentions"]["self_tok"]["chat_id"] is None
        assert "15550001111" in rows[1]["mentions"]
        assert rows[1]["mentions"]["15550001111"]["name"] == "Alice"
        assert "15550001111" in rows[2]["mentions"]
        assert rows[2]["mentions"]["15550001111"]["name"] == "Alice"
        conn.close()

    def test_hydrate_link_preview_rows_jpeg_thumbnail(self) -> None:
        # Guard: empty rows
        hydration._hydrate_link_preview_rows([], "ios")

        # JPEG SOI marker in raw_meta sets has_thumbnail = 1
        raw_meta = b"prefix\xff\xd8\xffsuffix"
        rows = [{"media_type": "link", "_raw_metadata": raw_meta}]
        hydration._hydrate_link_preview_rows(rows, "ios")
        assert rows[0].get("has_thumbnail") == 1

    def test_hydrate_live_photos_guards(self) -> None:
        # Guard: empty rows
        hydration._hydrate_live_photos([], "android", None)
        hydration._hydrate_live_photos([], "ios", None)

        conn = sqlite3.connect(":memory:")
        # Guard: empty mids / stanzas
        hydration._hydrate_live_photos([{"media_type": "text"}], "android", conn)
        hydration._hydrate_live_photos([{"media_type": "text"}], "ios", conn)
        conn.close()

    def test_backfill_recent_messages_all_guards(self, tmp_path: Path) -> None:
        # Guard: empty rows or None target
        hydration._backfill_recent_messages(None, [])
        hydration._backfill_recent_messages(tmp_path / "x.db", [])

        # Filtered rows empty (synthetic/call/negative msg_id)
        hydration._backfill_recent_messages(tmp_path / "x.db", [{"is_preserved": 1, "msg_id": 1}])
        hydration._backfill_recent_messages(
            tmp_path / "x.db", [{"media_type": "call", "msg_id": 2}]
        )
        hydration._backfill_recent_messages(tmp_path / "x.db", [{"msg_id": -1}])

        # DB exists but no recent_messages table
        empty_db = tmp_path / "empty.db"
        conn = sqlite3.connect(str(empty_db))
        conn.execute("CREATE TABLE foo (x INTEGER)")
        conn.close()
        hydration._backfill_recent_messages(
            empty_db,
            [
                {
                    "msg_id": 10,
                    "chat_id": "1",
                    "chat_type": "contact",
                    "media_type": "text",
                    "timestamp_ms": 1000,
                    "sender": "A",
                    "from_me": 0,
                    "text_body": "",
                }
            ],
        )

    def test_parse_ios_receipt_blob_compact_and_arch_contacts(self) -> None:
        # Compact receipt with field 9 fallback deltas (no base_ts, anchor from msg_ts_s)
        lid_bytes = lid_field1_bytes("103000000000088")
        event9 = build_field(1, 0, 12) + build_field(2, 0, 3)
        entry = build_field(1, 2, lid_bytes) + build_field(9, 2, event9)
        blob = build_field(2, 2, entry)

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            ATTACH DATABASE ':memory:' AS arch;
            CREATE TEMP TABLE _ios_contacts (jid TEXT PRIMARY KEY, full_name TEXT, phone_number TEXT);
            CREATE TABLE arch.contacts (number TEXT PRIMARY KEY, display_name TEXT);

            INSERT INTO _ios_contacts VALUES ('103000000000088@lid', NULL, '15550008888');
            INSERT INTO arch.contacts VALUES ('15550008888', 'Arch Display User');
        """)

        res = hydration._parse_ios_receipt_blob(blob, conn, is_group=True, msg_ts_s=1700000000)
        assert len(res) == 1
        assert res[0]["name"] == "Arch Display User"
        assert res[0]["delivered_ts"] == (1700000000 + 12) * 1000
        assert res[0]["read_ts"] is None

        # Empty blob guard
        assert hydration._parse_ios_receipt_blob(b"", conn) == []
        conn.close()

    def test_parse_ios_receipt_device_rows_empty_and_contact_fallbacks(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            ATTACH DATABASE ':memory:' AS arch;
            CREATE TEMP TABLE _ios_contacts (jid TEXT PRIMARY KEY, full_name TEXT, phone_number TEXT);
            CREATE TABLE arch.contacts (number TEXT PRIMARY KEY, display_name TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);

            INSERT INTO _ios_contacts VALUES ('15550001000@s.whatsapp.net', NULL, '15550001000');
            INSERT INTO arch.contacts VALUES ('15550001000', 'Arch Phone User');
        """)

        infra_rows = [
            # Missing user_jid
            {
                "user_jid": None,
                "delivered_timestamp": 1000,
                "read_timestamp": None,
                "played_timestamp": None,
            },
            # User resolving to arch.contacts
            {
                "user_jid": "15550001000@s.whatsapp.net",
                "delivered_timestamp": 1700000000,
                "read_timestamp": 1700000050,
                "played_timestamp": 1700000060,
            },
        ]

        members = hydration._parse_ios_receipt_device_rows(infra_rows, conn, is_group=True)
        assert len(members) == 1
        assert members[0]["name"] == "Arch Phone User"

        # 1-to-1 chat with no timestamps returns []
        empty_rows = [
            {
                "user_jid": "15550001000@s.whatsapp.net",
                "delivered_timestamp": None,
                "read_timestamp": None,
                "played_timestamp": None,
            }
        ]
        assert hydration._parse_ios_receipt_device_rows(empty_rows, conn, is_group=False) == []
        conn.close()

    def test_ios_reactions_raw_empty_and_no_reactors(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER, ZMESSAGEINFO INTEGER, ZGROUPMEMBER INTEGER, ZFROMJID TEXT);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT);
            CREATE TABLE ZWAMESSAGEINFO (Z_PK INTEGER PRIMARY KEY, ZRECEIPTINFO BLOB);
            INSERT INTO ZWAMESSAGE VALUES (1, 10, 1, 100, NULL, '15550001111@s.whatsapp.net');
            INSERT INTO ZWAMESSAGE VALUES (2, 10, 1, 200, NULL, '15550001111@s.whatsapp.net');
            INSERT INTO ZWAMESSAGEINFO VALUES (100, X''); -- empty raw
            INSERT INTO ZWAMESSAGEINFO VALUES (200, X'0801'); -- valid protobuf but no reactors
        """)
        res = hydration._ios_reactions(conn, "10", [1, 2])
        assert res == {}
        conn.close()

    def test_load_android_contact_chats_j2_clean(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER DEFAULT 0);
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TEMP TABLE _jid_map_resolved (lid_row_id INTEGER PRIMARY KEY, jid_row_id INTEGER);
            INSERT INTO jid VALUES (1, '103000000000001', 'lid', '103000000000001@lid');
            INSERT INTO jid VALUES (2, '15550002222:1', 's.whatsapp.net', '15550002222:1@s.whatsapp.net');
            INSERT INTO _jid_map_resolved VALUES (1, 2);
            INSERT INTO chat VALUES (10, 1, NULL, 0);
        """)
        tok_to_chat, _ = hydration._load_android_contact_chats(conn, None)
        assert tok_to_chat["15550002222"] == "15550002222"
        conn.close()

    def test_parse_ios_receipt_blob_all_wires_and_fallbacks(self) -> None:
        # Outer loop wire 1 (8 bytes), wire 5 (4 bytes), and unsupported wire break
        outer_blob = (
            build_field(3, 0, 1700000000)
            + build_field(8, 1, b"12345678")  # outer wire 1
            + build_field(9, 5, b"1234")  # outer wire 5
        )
        lid_bytes = lid_field1_bytes("103000000000077")
        # Event with wire 2 and wire 5 noise
        unsupported_tag = encode_varint((5 << 3) | 7)
        event = (
            build_field(1, 0, 10)
            + build_field(3, 2, b"nested_noise")
            + build_field(4, 5, b"1234")
            + unsupported_tag
        )
        entry = (
            build_field(1, 2, lid_bytes)
            + build_field(10, 2, event)
            + build_field(5, 0, 20)  # read delta
            + build_field(15, 5, b"1234")  # member wire 5
            + unsupported_tag
        )
        outer_blob += build_field(2, 2, entry)
        outer_blob += unsupported_tag

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            ATTACH DATABASE ':memory:' AS arch;
            CREATE TEMP TABLE _ios_contacts (jid TEXT PRIMARY KEY, full_name TEXT, phone_number TEXT);
            CREATE TABLE arch.contacts (number TEXT PRIMARY KEY, display_name TEXT);
            INSERT INTO _ios_contacts VALUES ('103000000000077@lid', NULL, '15550007777');
            -- arch.contacts display_name is NULL, should fall back to +15550007777
            INSERT INTO arch.contacts VALUES ('15550007777', NULL);
        """)

        res = hydration._parse_ios_receipt_blob(outer_blob, conn, is_group=True)
        assert len(res) == 1
        assert res[0]["name"] == "+15550007777"
        assert res[0]["delivered_ts"] == (1700000000 + 10) * 1000
        assert res[0]["read_ts"] == (1700000000 + 20) * 1000
        conn.close()

    def test_parse_ios_receipt_device_rows_contact_cascade_all_branches(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            ATTACH DATABASE ':memory:' AS arch;
            CREATE TEMP TABLE _ios_contacts (jid TEXT PRIMARY KEY, full_name TEXT, phone_number TEXT);
            CREATE TABLE arch.contacts (number TEXT PRIMARY KEY, display_name TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);

            -- Case 1: _ios_contacts has phone_number, arch.contacts has NULL display_name -> +number
            INSERT INTO _ios_contacts VALUES ('15550001001@s.whatsapp.net', NULL, '15550001001');
            INSERT INTO arch.contacts VALUES ('15550001001', NULL);

            -- Case 2: prefix in _ios_contacts has full_name
            INSERT INTO _ios_contacts VALUES ('15550001002', 'Prefix Contact', NULL);

            -- Case 3: prefix in _ios_contacts has phone_number -> arch.contacts display_name
            INSERT INTO _ios_contacts VALUES ('15550001003', NULL, '15550001003');
            INSERT INTO arch.contacts VALUES ('15550001003', 'Arch Prefix Name');

            -- Case 4: prefix in _ios_contacts has phone_number -> arch.contacts NULL -> +number
            INSERT INTO _ios_contacts VALUES ('15550001004', NULL, '15550001004');
            INSERT INTO arch.contacts VALUES ('15550001004', NULL);

            -- Case 5: prefix in ZWAPROFILEPUSHNAME
            INSERT INTO ZWAPROFILEPUSHNAME VALUES (1, '15550001005@s.whatsapp.net', 'Push Name');

            -- Case 6: pure fallback to prefix
            -- 15550001006 has no entries anywhere
        """)

        infra_rows = [
            {
                "user_jid": "15550001001@s.whatsapp.net",
                "delivered_timestamp": 1000,
                "read_timestamp": None,
                "played_timestamp": None,
            },
            {
                "user_jid": "15550001002@s.whatsapp.net",
                "delivered_timestamp": 1000,
                "read_timestamp": None,
                "played_timestamp": None,
            },
            {
                "user_jid": "15550001003@s.whatsapp.net",
                "delivered_timestamp": 1000,
                "read_timestamp": None,
                "played_timestamp": None,
            },
            {
                "user_jid": "15550001004@s.whatsapp.net",
                "delivered_timestamp": 1000,
                "read_timestamp": None,
                "played_timestamp": None,
            },
            {
                "user_jid": "15550001005@s.whatsapp.net",
                "delivered_timestamp": 1000,
                "read_timestamp": None,
                "played_timestamp": None,
            },
            {
                "user_jid": "15550001006@s.whatsapp.net",
                "delivered_timestamp": 1000,
                "read_timestamp": None,
                "played_timestamp": None,
            },
        ]

        members = hydration._parse_ios_receipt_device_rows(infra_rows, conn, is_group=True)
        by_jid = {m["jid"]: m["name"] for m in members}
        assert by_jid["15550001001"] == "+15550001001"
        assert by_jid["15550001002"] == "Prefix Contact"
        assert by_jid["15550001003"] == "Arch Prefix Name"
        assert by_jid["15550001004"] == "+15550001004"
        assert by_jid["15550001005"] == "Push Name"
        assert by_jid["15550001006"] == "15550001006"
        conn.close()
