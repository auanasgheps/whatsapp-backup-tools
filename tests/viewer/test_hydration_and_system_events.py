"""Unit tests for Message Hydration, System Events, and Media Resolvers.

Tests wab_viewer.hydration and wab_viewer.system_events with 100% synthetic data.
"""

from __future__ import annotations

import json
import sqlite3

from tests.fixtures.protobuf_factory import (
    build_field,
    build_ios_poll_blob,
    make_zreceipt_blob_with_reactions,
)
from wab_viewer import hydration
from wab_viewer.system_events import (
    IOSEventType,
    SystemActionType,
    format_android_system_event,
    format_ios_system_event,
    format_participant_names,
)

# ---------------------------------------------------------------------------
# 1. Tests for System Events Formatting (system_events.py)
# ---------------------------------------------------------------------------


class TestSystemEventsFormatting:
    """Tests for format_participant_names, format_android_system_event, format_ios_system_event."""

    def test_format_participant_names_all_ranges(self) -> None:
        assert format_participant_names([]) == "Someone"
        assert format_participant_names(["Alice"]) == "Alice"
        assert format_participant_names(["Alice", "Bob"]) == "Alice and Bob"
        assert format_participant_names(["Alice", "Bob", "Charlie"]) == "Alice, Bob, and Charlie"
        assert (
            format_participant_names(["Alice", "Bob", "Charlie", "David"])
            == "Alice, Bob, Charlie, and 1 other"
        )
        assert (
            format_participant_names(["Alice", "Bob", "Charlie", "David", "Eve", "Frank"])
            == "Alice, Bob, Charlie, and 3 others"
        )

    def test_format_android_system_events_comprehensive(self) -> None:
        # 1. Announcement mode
        msg_ann_true = format_android_system_event(
            SystemActionType.ANNOUNCEMENT_MODE, "Alice", [], "true", {}
        )
        assert msg_ann_true == "Only admins can send messages in this group"

        msg_ann_false = format_android_system_event(
            SystemActionType.ANNOUNCEMENT_MODE, "Alice", [], "false", {}
        )
        assert msg_ann_false == "All participants can send messages in this group"

        # 2. Business account
        msg_biz = format_android_system_event(
            SystemActionType.BIZ_ACCOUNT_STATE, "Alice", [], "", {}
        )
        assert "WhatsApp Business account" in msg_biz

        # 3. Community links
        msg_comm_link = format_android_system_event(
            SystemActionType.COMMUNITY_LINK_CHANGED, "Alice", [], "", {}
        )
        assert msg_comm_link == "Alice added this group to the community"

        msg_comm_with_name = format_android_system_event(
            SystemActionType.COMMUNITY_LINK_CHANGED,
            "Alice",
            [],
            "",
            {"community_name": "Antigravity Devs"},
        )
        assert msg_comm_with_name == 'Alice added this group to community "Antigravity Devs"'

        # 4. Membership approvals
        msg_app_req = format_android_system_event(
            SystemActionType.MEMBERSHIP_APPROVAL_REQUEST, "Alice", ["Bob"], "", {}
        )
        assert msg_app_req == "Alice approved Bob's request to join"

        msg_app_mode_true = format_android_system_event(
            SystemActionType.MEMBERSHIP_APPROVAL_MODE, "Alice", [], "true", {}
        )
        assert msg_app_mode_true == "Admin approval is required to join this group"

        msg_app_mode_false = format_android_system_event(
            SystemActionType.MEMBERSHIP_APPROVAL_MODE, "Alice", [], "false", {}
        )
        assert msg_app_mode_false == "Admin approval is no longer required to join this group"

        # 5. Disappearing messages
        msg_ephem_days = format_android_system_event(
            SystemActionType.EPHEMERAL_SETTING, "Alice", [], "604800", {}
        )
        assert msg_ephem_days == "Disappearing messages set to 7 days"

        msg_ephem_hours = format_android_system_event(
            SystemActionType.EPHEMERAL_SETTING, "Alice", [], "7200", {}
        )
        assert msg_ephem_hours == "Disappearing messages set to 2 hours"

        msg_ephem_off = format_android_system_event(
            SystemActionType.EPHEMERAL_SETTING, "Alice", [], "0", {}
        )
        assert msg_ephem_off == "Disappearing messages turned off"

        # 6. Action 67 with readable text vs numeric fallback
        msg_67_text = format_android_system_event(
            SystemActionType.INITIAL_PRIVACY_PROVIDER,
            "Alice",
            [],
            "Custom system notice message",
            {},
        )
        assert "end-to-end encrypted" in msg_67_text

    def test_format_ios_system_events_comprehensive(self) -> None:
        # 1. Number changed
        assert (
            format_ios_system_event(IOSEventType.NUMBER_CHANGED, "Alice", [], "", False)
            == "Phone number changed"
        )
        assert (
            format_ios_system_event(IOSEventType.NUMBER_CHANGED, "Alice", [], "+15551234", False)
            == "Alice changed their phone number to +15551234."
        )

        # 2. Security code
        assert (
            format_ios_system_event(IOSEventType.SECURITY_CODE_CHANGED, "Alice", [], "", False)
            == "Your security code with Alice changed."
        )

        # 3. Group created
        assert (
            format_ios_system_event(IOSEventType.GROUP_CREATED, "You", [], "", False)
            == "You created this group"
        )
        assert (
            format_ios_system_event(IOSEventType.GROUP_CREATED, "Alice", [], "Project Team", False)
            == 'Alice created group "Project Team"'
        )

        # 4. Subject changed
        assert (
            format_ios_system_event(
                IOSEventType.SUBJECT_CHANGED, "Alice", [], "Project Team", False
            )
            == 'Alice changed the subject to "Project Team"'
        )
        assert (
            format_ios_system_event(IOSEventType.SUBJECT_CHANGED, "Alice", [], "", False)
            == "Alice changed the group subject"
        )

        # 5. Icon changed / removed
        assert (
            format_ios_system_event(IOSEventType.ICON_CHANGED, "Alice", [], "", False)
            == "Alice changed this group's icon"
        )
        assert (
            format_ios_system_event(IOSEventType.ICON_CHANGED, "Alice", [], "deleted", False)
            == "Alice deleted this group's icon"
        )

        # 6. Participant left
        assert (
            format_ios_system_event(IOSEventType.PARTICIPANT_LEFT, "Alice", ["Alice"], "", False)
            == "Alice left"
        )
        assert (
            format_ios_system_event(IOSEventType.PARTICIPANT_LEFT, "Alice", ["You"], "", True)
            == "You left"
        )

        # 7. Participant joined / added / removed
        assert (
            format_ios_system_event(
                IOSEventType.JOINED_VIA_INVITE_LINK, "Alice", ["Bob"], "", False
            )
            == "Bob joined using this group's invite link"
        )
        assert (
            format_ios_system_event(
                IOSEventType.PARTICIPANT_JOINED_OR_ADDED, "Alice", ["Bob"], "", False
            )
            == "Alice added Bob"
        )
        assert (
            format_ios_system_event(IOSEventType.PARTICIPANT_REMOVED, "Alice", ["Bob"], "", False)
            == "Alice removed Bob"
        )

        # 8. Admin promote / demote
        assert (
            format_ios_system_event(IOSEventType.ADMIN_PROMOTED, "Someone", ["You"], "", False)
            == "You're now an admin"
        )
        assert (
            format_ios_system_event(IOSEventType.ADMIN_PROMOTED, "Someone", ["Bob"], "", False)
            == "Bob is now an admin"
        )
        assert (
            format_ios_system_event(IOSEventType.ADMIN_DEMOTED, "Someone", ["You"], "", False)
            == "You're no longer an admin"
        )
        assert (
            format_ios_system_event(IOSEventType.ADMIN_DEMOTED, "Someone", ["Bob"], "", False)
            == "Bob is no longer an admin"
        )

        # 9. Disappearing messages
        assert (
            format_ios_system_event(
                IOSEventType.DISAPPEARING_MESSAGES, "Alice", [], "604800", False
            )
            == "Disappearing messages set to 7 days"
        )
        assert (
            format_ios_system_event(IOSEventType.DISAPPEARING_MESSAGES, "Alice", [], "7200", False)
            == "Disappearing messages set to 2 hours"
        )
        assert (
            format_ios_system_event(IOSEventType.DISAPPEARING_MESSAGES, "Alice", [], "0", False)
            == "Disappearing messages turned off"
        )

        # 10. Settings & Approval changes
        assert (
            format_ios_system_event(IOSEventType.SETTINGS_CHANGED_36, "Alice", [], "", False)
            == "Group settings changed"
        )
        assert (
            format_ios_system_event(IOSEventType.APPROVAL_CHANGE, "Alice", [], "", False)
            == "Group membership approval settings changed"
        )
        assert (
            format_ios_system_event(IOSEventType.ANNOUNCEMENT_MODE, "Alice", [], "false", False)
            == "All participants can send messages in this group"
        )
        assert (
            format_ios_system_event(IOSEventType.ANNOUNCEMENT_MODE, "Alice", [], "true", False)
            == "Only admins can send messages in this group"
        )

        # 11. Generic readable fallback vs default
        assert (
            format_ios_system_event(
                99999,
                "Alice",
                [],
                "Group description updated",
                False,  # type: ignore
            )
            == "Group description updated"
        )
        assert (
            format_ios_system_event(99999, "Alice", [], "12345", False)  # type: ignore
            == "Group event"
        )


# ---------------------------------------------------------------------------
# 2. Tests for Media Type From Path and Helpers (hydration.py)
# ---------------------------------------------------------------------------


class TestMediaTypeAndHelpers:
    """Tests for _media_type_from_path, _strip_none_reactions, _split_jid_parts, _is_masked_lid_name, _normalize_message_rows, _table_exists."""

    def test_media_type_from_path_all_extensions(self) -> None:
        assert hydration._media_type_from_path(None) == "text"  # type: ignore
        assert hydration._media_type_from_path("contact.vcf") == "vcard"
        assert hydration._media_type_from_path("sticker.webp") == "sticker"
        assert hydration._media_type_from_path("stk-12345.was") == "sticker"
        assert hydration._media_type_from_path("WhatsApp Stickers/item.webp") == "sticker"
        assert hydration._media_type_from_path("clip.gif") == "gif"
        assert hydration._media_type_from_path("WhatsApp Animated Gifs/anim.mp4") == "gif"
        assert hydration._media_type_from_path("photo.jpg") == "image"
        assert hydration._media_type_from_path("picture.heic") == "image"
        assert hydration._media_type_from_path("video.mp4") == "video"
        assert hydration._media_type_from_path("movie.mov") == "video"
        assert hydration._media_type_from_path("track.mp3") == "audio"
        assert hydration._media_type_from_path("voice.opus") == "audio"
        assert hydration._media_type_from_path("manual.pdf") == "document"
        assert hydration._media_type_from_path("archive.zip") == "document"

    def test_strip_none_reactions(self) -> None:
        rows = [
            {"id": 1, "reactions": None},
            {"id": 2, "reactions": "👍"},
            {"id": 3, "reactions": "None"},
        ]
        res = hydration._strip_none_reactions(rows)
        assert "reactions" not in res[0]
        assert res[1]["reactions"] == "👍"
        assert res[2]["reactions"] == "None"

    def test_split_jid_parts_and_masked_lids(self) -> None:
        u, s, canon = hydration._split_jid_parts("15550010:1@s.whatsapp.net")
        assert u == "15550010"
        assert s == "s.whatsapp.net"
        assert canon == "15550010@s.whatsapp.net"

        u2, s2, canon2 = hydration._split_jid_parts("simple_user")
        assert u2 == "simple_user"
        assert s2 == ""
        assert canon2 == "simple_user"

        assert hydration._is_masked_lid_name(None) is True
        assert hydration._is_masked_lid_name("") is True
        assert hydration._is_masked_lid_name("   ") is True
        assert hydration._is_masked_lid_name("+1 555 ∙∙∙ 1234") is True
        assert hydration._is_masked_lid_name("+1 555 ••• 1234") is True
        assert hydration._is_masked_lid_name("Normal Contact Name") is False

    def test_normalize_message_rows_comprehensive(self) -> None:
        raw_rows = [
            {
                "msg_id": 1,
                "text_body": "Voice 1",
                "audio_waveform": b"\x01\x02\x03\x04",
                "_raw_metadata": b"internal_meta",
            },
            {
                "msg_id": 2,
                "text_body": "Voice 2",
                "waveform": bytearray([10, 20, 30]),
            },
            {
                "msg_id": 3,
                "text_body": "Voice 3",
                "waveform": [5, 15, 25],
            },
            {
                "msg_id": 4,
                "text_body": "Text message",
            },
        ]
        norm = hydration._normalize_message_rows(raw_rows)
        assert len(norm) == 4
        # First row: audio_waveform bytes unpacked to list, _raw_metadata popped
        assert norm[0]["waveform"] == [1, 2, 3, 4]
        assert "_raw_metadata" not in norm[0]
        # Second row: waveform bytearray unpacked to list
        assert norm[1]["waveform"] == [10, 20, 30]
        # Third row: waveform list kept as list
        assert norm[2]["waveform"] == [5, 15, 25]
        # Fourth row: no waveform -> None
        assert norm[3]["waveform"] is None

    def test_table_exists(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE main_tbl (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TEMP TABLE temp_tbl (id INTEGER PRIMARY KEY)")

        assert hydration._table_exists(conn, "main_tbl", "main") is True
        assert hydration._table_exists(conn, "temp_tbl", "temp") is True
        assert hydration._table_exists(conn, "non_existent_tbl", "main") is False
        conn.close()


# ---------------------------------------------------------------------------
# 3. Tests for JID & Service Row Resolution (hydration.py)
# ---------------------------------------------------------------------------


class TestJidAndServiceRowResolution:
    """Tests for _resolve_ios_jid, _format_ios_service_row, _format_android_service_row, _hydrate_android_service_participants."""

    def test_resolve_ios_jid_permutations(self) -> None:
        contacts = {
            "15550020": ("Alice Smith", "15550020"),
            "999888@lid": ("Bob Jones", None),
        }
        assert hydration._resolve_ios_jid("15550020@s.whatsapp.net", contacts) == "Alice Smith"
        assert hydration._resolve_ios_jid("15550020", contacts) == "Alice Smith"
        assert hydration._resolve_ios_jid("999888@lid", contacts) == "Bob Jones"
        assert hydration._resolve_ios_jid("0@s.whatsapp.net", contacts) == "WhatsApp"
        assert hydration._resolve_ios_jid("15550099@s.whatsapp.net", contacts) == "+15550099"
        assert hydration._resolve_ios_jid("non_digit_user", contacts) == "non_digit_user"

    def test_format_ios_service_row_creation_and_encryption(self) -> None:
        contacts = {"15550030": ("Charlie", None)}
        row_created = {
            "group_event_type": 12,
            "member_jid": "15550030@s.whatsapp.net",
            "text_body": "New Project",
            "chat_type": "group",
            "raw_msg_type": 10,
        }
        assert (
            hydration._format_ios_service_row(row_created, contacts)
            == 'Charlie created group "New Project"'
        )

        row_contact = {
            "group_event_type": 1,
            "chat_type": "contact",
            "raw_msg_type": 10,
        }
        assert "end-to-end encrypted" in hydration._format_ios_service_row(row_contact, contacts)

    def test_format_android_service_row_encrypted_and_created(self) -> None:
        row_enc = {
            "action_type": SystemActionType.GROUP_CREATED,
            "chat_type": "contact",
            "text_body": None,
        }
        assert "end-to-end encrypted" in hydration._format_android_service_row(row_enc)

        row_created = {
            "action_type": SystemActionType.GROUP_CREATED,
            "chat_type": "group",
            "sender": "Dave",
            "participant_names": [],
            "action_param": "",
        }
        assert hydration._format_android_service_row(row_created) == "Dave created this group"

    def test_hydrate_android_service_participants_comprehensive(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            ATTACH ':memory:' AS arch;
            CREATE TABLE arch.contacts (number TEXT PRIMARY KEY, display_name TEXT);
            INSERT INTO arch.contacts VALUES ('555001', 'Archived Alice');

            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT, server TEXT);
            CREATE TABLE message_system_chat_participant (message_row_id INTEGER, user_jid_row_id INTEGER);
            CREATE TEMP TABLE _jid_map_resolved (lid_row_id INTEGER PRIMARY KEY, jid_row_id INTEGER);
            CREATE TABLE lid_display_name (lid_row_id INTEGER PRIMARY KEY, display_name TEXT);

            -- 1. 'me' raw user -> 'You'
            INSERT INTO jid VALUES (1, 'me', 'me@s.whatsapp.net', 's.whatsapp.net');
            INSERT INTO message_system_chat_participant VALUES (101, 1);

            -- 2. Matched contact in arch.contacts -> 'Archived Alice'
            INSERT INTO jid VALUES (2, '555001', '555001@s.whatsapp.net', 's.whatsapp.net');
            INSERT INTO message_system_chat_participant VALUES (101, 2);

            -- 3. WhatsApp system user '0' -> 'WhatsApp'
            INSERT INTO jid VALUES (3, '0', '0@s.whatsapp.net', 's.whatsapp.net');
            INSERT INTO message_system_chat_participant VALUES (102, 3);

            -- 4. LID mapped to real user '555002' -> '+555002'
            INSERT INTO jid VALUES (4, 'lid_token_4', 'lid_token_4@lid', 'lid');
            INSERT INTO jid VALUES (5, '555002', '555002@s.whatsapp.net', 's.whatsapp.net');
            INSERT INTO _jid_map_resolved VALUES (4, 5);
            INSERT INTO message_system_chat_participant VALUES (102, 4);

            -- 5. LID unmapped but with lid_display_name -> 'LID Display Name'
            INSERT INTO jid VALUES (6, 'lid_token_6', 'lid_token_6@lid', 'lid');
            INSERT INTO lid_display_name VALUES (6, 'LID Display Name');
            INSERT INTO message_system_chat_participant VALUES (103, 6);

            -- 6. Unmapped normal phone user -> '+555003'
            INSERT INTO jid VALUES (7, '555003', '555003@s.whatsapp.net', 's.whatsapp.net');
            INSERT INTO message_system_chat_participant VALUES (103, 7);
        """)

        p_map = hydration._hydrate_android_service_participants(
            conn, [101, 102, 103], True, True, True, True
        )
        conn.close()

        assert 101 in p_map
        assert "You" in p_map[101]
        assert "Archived Alice" in p_map[101]

        assert 102 in p_map
        assert "WhatsApp" in p_map[102]
        assert "+555002" in p_map[102]

        assert 103 in p_map
        assert "LID Display Name" in p_map[103]
        assert "+555003" in p_map[103]


# ---------------------------------------------------------------------------
# 4. Tests for Link Previews and Live Photos (hydration.py)
# ---------------------------------------------------------------------------


class TestLinkPreviewsAndLivePhotos:
    """Tests for _hydrate_link_preview_rows, _hydrate_live_photos on Android and iOS."""

    def test_hydrate_link_preview_url_regex_and_thumbnail(self) -> None:
        rows = [
            {
                "msg_id": 1,
                "media_type": "link",
                "text_body": "Check out https://antigravity.dev for details",
                "link_url": None,
                "link_title": "Antigravity",
                "_raw_metadata": b"preview_info\xff\xd8\xffimage_data",
                "has_thumbnail": 0,
            },
            {
                "msg_id": 2,
                "media_type": "text",
                "text_body": "Plain text without links",
                "link_url": None,
            },
        ]

        hydration._hydrate_link_preview_rows(rows, "ios")

        assert rows[0]["link_url"] == "https://antigravity.dev"
        assert rows[0]["has_thumbnail"] == 1
        assert rows[1]["link_url"] is None

    def test_hydrate_live_photos_android(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE message_association (
                parent_message_row_id INTEGER,
                child_message_row_id INTEGER,
                association_type INTEGER
            );
            CREATE TABLE message_media (
                message_row_id INTEGER PRIMARY KEY,
                file_path TEXT
            );
            -- Motion photo pairing: association_type = 11
            INSERT INTO message_association VALUES (10, 20, 11);
            INSERT INTO message_media VALUES (20, 'Media/WhatsApp Video/VID_MOTION.mp4');
        """)

        rows = [
            {
                "msg_id": 10,
                "media_type": "image",
                "archive_path": "Media/WhatsApp Images/IMG_MOTION.jpg",
            },
            {
                "msg_id": 99,
                "media_type": "image",
                "archive_path": "Media/WhatsApp Images/IMG_NORMAL.jpg",
            },
        ]

        hydration._hydrate_live_photos(rows, "android", conn)
        conn.close()

        assert rows[0]["is_live_photo"] == 1
        assert rows[0]["live_photo_path"] == "Media/WhatsApp Video/VID_MOTION.mp4"
        assert rows[1].get("is_live_photo") is None

    def test_hydrate_live_photos_ios(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            ATTACH ':memory:' AS ext;
            CREATE TABLE ext.message_parent_association (
                parent_stanza_id TEXT,
                stanza_id TEXT,
                type INTEGER
            );
            CREATE TABLE ZWAMESSAGE (
                ZSTANZAID TEXT PRIMARY KEY,
                ZMEDIAITEM INTEGER
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT
            );
            -- iOS live photo association type = 12
            INSERT INTO ext.message_parent_association VALUES ('STANZA_PHOTO_1', 'STANZA_VIDEO_1', 12);
            INSERT INTO ZWAMESSAGE VALUES ('STANZA_VIDEO_1', 1);
            INSERT INTO ZWAMEDIAITEM VALUES (1, 'Media/live_clip.mov');
        """)

        rows = [
            {
                "msg_id": 100,
                "stanza_id": "STANZA_PHOTO_1",
                "media_type": "image",
                "archive_path": "Message/Media/live_photo.heic",
            }
        ]

        hydration._hydrate_live_photos(rows, "ios", conn)
        conn.close()

        assert rows[0]["is_live_photo"] == 1
        assert rows[0]["live_photo_path"] == "Message/Media/live_clip.mov"


# ---------------------------------------------------------------------------
# 5. Tests for iOS Receipts & Blob Decoding (hydration.py)
# ---------------------------------------------------------------------------


class TestIOSReceiptBlobDecoding:
    """Tests for _parse_ios_receipt_blob, _ios_read_without_timestamp, and _parse_ios_receipt_device_rows."""

    def test_parse_ios_receipt_blob_full_resolution(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE _ios_contacts (jid TEXT PRIMARY KEY, full_name TEXT, phone_number TEXT);
            ATTACH ':memory:' AS arch;
            CREATE TABLE arch.contacts (number TEXT PRIMARY KEY, display_name TEXT);

            INSERT INTO _ios_contacts VALUES ('15550010@lid', 'Resolved Alice', '15550010');
            INSERT INTO arch.contacts VALUES ('15550011', 'Archived Bob');
        """)

        base_ts = 1729000000

        # Build synthetic ZRECEIPTINFO protobuf:
        # Top field 3 = base_ts (varint)
        # Top field 2 = repeated member entries (length-delimited)
        # Member entry:
        # field 1: LID bytes (value[1:].hex() -> lid)
        # field 5: read delta
        # field 10: delivered event subfield 1
        entry1 = (
            build_field(1, 2, b"\x00" + bytes.fromhex("15550010"))
            + build_field(5, 0, 20)
            + build_field(10, 2, build_field(1, 0, 10))
        )
        entry2 = (
            build_field(1, 2, b"\x00" + bytes.fromhex("15550011"))
            + build_field(5, 0, 25)
            + build_field(10, 2, build_field(1, 0, 15))
        )

        blob = build_field(3, 0, base_ts) + build_field(2, 2, entry1) + build_field(2, 2, entry2)

        receipts = hydration._parse_ios_receipt_blob(blob, conn, is_group=True, msg_ts_s=base_ts)
        conn.close()

        assert len(receipts) == 2

        # 1. Resolved via _ios_contacts full_name
        r0 = receipts[0]
        assert r0["name"] == "Resolved Alice"
        assert r0["delivered_ts"] == (base_ts + 10) * 1000
        assert r0["read_ts"] == (base_ts + 20) * 1000

        # 2. Resolved via arch.contacts display_name
        r1 = receipts[1]
        assert r1["name"] == "Archived Bob"
        assert r1["delivered_ts"] == (base_ts + 15) * 1000
        assert r1["read_ts"] == (base_ts + 25) * 1000

    def test_ios_read_without_timestamp(self) -> None:
        assert hydration._ios_read_without_timestamp(8, None) is True
        assert hydration._ios_read_without_timestamp(8, 1729000000) is False
        assert hydration._ios_read_without_timestamp(5, None) is False

    def test_parse_ios_receipt_device_rows_comprehensive(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE _ios_contacts (jid TEXT PRIMARY KEY, full_name TEXT, phone_number TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (ZJID TEXT PRIMARY KEY, ZPUSHNAME TEXT);
            ATTACH ':memory:' AS arch;
            CREATE TABLE arch.contacts (number TEXT PRIMARY KEY, display_name TEXT);

            INSERT INTO _ios_contacts VALUES ('user1@s.whatsapp.net', 'User One', '1555001');
        """)

        infra_rows = [
            {
                "user_jid": "user1@s.whatsapp.net",
                "delivered_timestamp": 1000,
                "read_timestamp": 1020,
                "played_timestamp": None,
            },
            {
                "user_jid": "user1@s.whatsapp.net",
                "delivered_timestamp": 990,
                "read_timestamp": 1010,
                "played_timestamp": None,
            },
        ]

        # 1. 1-to-1 mode (is_group = False) aggregates to min timestamps
        direct_res = hydration._parse_ios_receipt_device_rows(infra_rows, conn, is_group=False)
        assert len(direct_res) == 1
        assert direct_res[0]["delivered_ts"] == 990 * 1000
        assert direct_res[0]["read_ts"] == 1010 * 1000
        assert direct_res[0]["read_known"] is True

        # 2. Group mode (is_group = True) resolves participant name
        group_res = hydration._parse_ios_receipt_device_rows(infra_rows, conn, is_group=True)
        assert len(group_res) == 1
        assert group_res[0]["name"] == "User One"
        assert group_res[0]["delivered_ts"] == 990 * 1000
        assert group_res[0]["read_ts"] == 1010 * 1000

        conn.close()


# ---------------------------------------------------------------------------
# 6. Tests for Service Rows Formatting Integration (hydration.py)
# ---------------------------------------------------------------------------


class TestFormatServiceRows:
    """Tests for _format_service_rows on Android and iOS."""

    def test_format_service_rows_android_deletion_and_group_events(
        self,
    ) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE message_system_chat_participant (message_row_id INTEGER, user_jid_row_id INTEGER);
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT, server TEXT);
        """)

        rows = [
            # 1-to-1 message deletion (action_type 1)
            {
                "msg_id": 1,
                "media_type": "service",
                "message_type": 7,
                "action_type": 1,
                "chat_type": "contact",
                "from_me": 1,
                "text_body": None,
            },
            # Group event (subject change)
            {
                "msg_id": 2,
                "media_type": "service",
                "message_type": 7,
                "action_type": SystemActionType.SUBJECT_CHANGE,
                "chat_type": "group",
                "sender": "Alice",
                "text_body": "New Title",
            },
        ]

        hydration._format_service_rows(rows, "android", conn, False, False, False, False)
        conn.close()

        # Action type 1 converted to deleted media_type
        assert rows[0]["media_type"] == "deleted"
        assert rows[0]["text_body"] == "You deleted this message"

        # Action type 1 formatted into group subject change string
        assert rows[1]["text_body"] == 'Alice changed the subject to "New Title"'

    def test_format_service_rows_ios_creation_and_member_jids(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE _ios_contacts (jid TEXT PRIMARY KEY, full_name TEXT, phone_number TEXT);
            INSERT INTO _ios_contacts VALUES ('15550090@s.whatsapp.net', 'Evelyn', '15550090');
        """)

        rows = [
            {
                "msg_id": 10,
                "media_type": "service",
                "raw_msg_type": 10,
                "group_event_type": IOSEventType.PARTICIPANT_JOINED_OR_ADDED,
                "member_jid": "15550090@s.whatsapp.net",
                "chat_type": "group",
                "text_body": "15550090@s.whatsapp.net",
            }
        ]

        hydration._format_service_rows(rows, "ios", conn, False, False, False, False)
        conn.close()

        assert rows[0]["text_body"] == "Evelyn joined"


# ---------------------------------------------------------------------------
# 7. Tests for Reactions and Polls Hydration (hydration.py)
# ---------------------------------------------------------------------------


class TestReactionsAndPollsHydration:
    """Tests for _ios_reactions, _resolve_and_cache_reactions, and _hydrate_poll_rows."""

    def test_ios_reactions_from_me_annotation(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZISFROMME INTEGER,
                ZFROMJID TEXT,
                ZGROUPMEMBER INTEGER,
                ZMESSAGEINFO INTEGER
            );
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT);
            CREATE TABLE ZWAMESSAGEINFO (Z_PK INTEGER PRIMARY KEY, ZRECEIPTINFO BLOB);

            -- Sent message by me (phone 15550040)
            INSERT INTO ZWAMESSAGE VALUES (1, 10, 1, '15550040@s.whatsapp.net', NULL, NULL);

            -- Message 50 with reaction blob
        """)

        # Build reaction blob using protobuf factory
        blob = make_zreceipt_blob_with_reactions(
            [
                ("15550040", "❤️".encode()),
                ("15550041", "👍".encode()),
            ]
        )

        conn.execute("INSERT INTO ZWAMESSAGEINFO VALUES (100, ?)", (blob,))
        conn.execute(
            "INSERT INTO ZWAMESSAGE VALUES (50, 10, 0, '15550041@s.whatsapp.net', NULL, 100)"
        )
        conn.commit()

        reactions = hydration._ios_reactions(conn, "10", [50])
        conn.close()

        assert 50 in reactions
        reactors = reactions[50]
        assert len(reactors) == 2
        # First reactor matches my_phone (15550040), so from_me = 1
        assert reactors[0] == ("15550040", "❤️", 1)
        # Second reactor is other, so from_me = 0
        assert reactors[1] == ("15550041", "👍", 0)

    def test_resolve_and_cache_reactions_android(self) -> None:
        wa_conn = sqlite3.connect(":memory:")
        wa_conn.row_factory = sqlite3.Row
        wa_conn.executescript("""
            CREATE TABLE message_add_on (
                _id INTEGER PRIMARY KEY,
                parent_message_row_id INTEGER
            );
            CREATE TABLE message_add_on_reaction (
                message_add_on_row_id INTEGER,
                reaction TEXT
            );

            INSERT INTO message_add_on VALUES (1, 100);
            INSERT INTO message_add_on VALUES (2, 100);
            INSERT INTO message_add_on_reaction VALUES (1, '🎉');
            INSERT INTO message_add_on_reaction VALUES (2, '🔥');
        """)

        arc_conn = sqlite3.connect(":memory:")
        arc_conn.executescript("""
            CREATE TABLE reactions_cache (
                chat_id TEXT,
                msg_id INTEGER,
                reactions TEXT,
                PRIMARY KEY (chat_id, msg_id)
            );
        """)

        rows = [{"chat_id": "c1", "msg_id": 100, "reactions": None}]
        hydration._resolve_and_cache_reactions("android", wa_conn, arc_conn, rows)

        assert rows[0]["reactions"] == "🎉,🔥"
        cached = arc_conn.execute(
            "SELECT reactions FROM reactions_cache WHERE chat_id='c1' AND msg_id=100"
        ).fetchone()
        assert cached[0] == "🎉,🔥"

        wa_conn.close()
        arc_conn.close()

    def test_hydrate_poll_rows_android(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE message_poll (
                message_row_id INTEGER PRIMARY KEY,
                selectable_options_count INTEGER
            );
            CREATE TABLE message_poll_option (
                _id INTEGER PRIMARY KEY,
                message_row_id INTEGER,
                option_name TEXT,
                vote_total INTEGER
            );
            CREATE TABLE message_add_on (
                _id INTEGER PRIMARY KEY,
                parent_message_row_id INTEGER,
                from_me INTEGER,
                message_add_on_type INTEGER
            );
            CREATE TABLE message_add_on_poll_vote_selected_option (
                message_add_on_row_id INTEGER,
                message_poll_option_id INTEGER
            );

            -- Poll message 10 (Single choice: selectable_options_count = 1)
            INSERT INTO message_poll VALUES (10, 1);
            INSERT INTO message_poll_option VALUES (1, 10, 'Option A', 2);
            INSERT INTO message_poll_option VALUES (2, 10, 'Option B', 1);

            -- Voter records: user voted Option A (from_me = 1)
            INSERT INTO message_add_on VALUES (101, 10, 1, 67);
            INSERT INTO message_add_on_poll_vote_selected_option VALUES (101, 1);
        """)

        rows = [
            {
                "msg_id": 10,
                "media_type": "poll",
                "text_body": "What is your preference?",
            }
        ]

        hydration._hydrate_poll_rows(rows, "android", conn)
        conn.close()

        poll = rows[0]["poll"]
        assert poll["question"] == "What is your preference?"
        assert poll["multiple_answers"] is False
        assert poll["total_votes"] == 3
        assert poll["options"][0]["name"] == "Option A"
        assert poll["options"][0]["vote_count"] == 2
        assert poll["options"][0]["user_selected"] is True
        assert poll["options"][1]["name"] == "Option B"
        assert poll["options"][1]["user_selected"] is False

    def test_hydrate_poll_rows_ios(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE ZWAMESSAGEINFO (
                ZMESSAGE INTEGER PRIMARY KEY,
                ZRECEIPTINFO BLOB
            );
        """)

        poll_blob = build_ios_poll_blob(
            question="Lunch plan?",
            options=["Pizza", "Sushi"],
            selectable_count=1,
            votes=[{"voter_jid": "You", "option_indices": [0]}],
        )

        conn.execute("INSERT INTO ZWAMESSAGEINFO VALUES (50, ?)", (poll_blob,))
        conn.commit()

        rows = [{"msg_id": 50, "media_type": "poll", "text_body": "Lunch plan?"}]
        hydration._hydrate_poll_rows(rows, "ios", conn)
        conn.close()

        poll = rows[0]["poll"]
        assert poll["question"] == "Lunch plan?"
        assert poll["multiple_answers"] is False
        assert poll["total_votes"] == 1
        assert poll["options"][0]["name"] == "Pizza"
        assert poll["options"][0]["user_selected"] is True
        assert poll["options"][1]["name"] == "Sushi"
        assert poll["options"][1]["user_selected"] is False


# ---------------------------------------------------------------------------
# 8. Tests for Mentions, Quoted Info, and Recent Messages (hydration.py)
# ---------------------------------------------------------------------------


class TestMentionsAndRecentMessages:
    """Tests for mentions resolution, contact chats loading, quoted hydration, and recent messages backfilling."""

    def test_resolve_android_mention_tokens(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT, server TEXT);
            CREATE TABLE message_mentions (message_row_id INTEGER, jid_row_id INTEGER);
            CREATE TEMP TABLE _jid_map_resolved (lid_row_id INTEGER PRIMARY KEY, jid_row_id INTEGER);
            CREATE TEMP TABLE _contacts_resolved (clean_number TEXT PRIMARY KEY, display_name TEXT, folder TEXT);

            INSERT INTO jid VALUES (1, '5550050', '5550050@s.whatsapp.net', 's.whatsapp.net');
            INSERT INTO jid VALUES (2, 'me', 'lid_me', 'lid');
            INSERT INTO message_mentions VALUES (100, 1);
            INSERT INTO message_mentions VALUES (100, 2);
            INSERT INTO _contacts_resolved VALUES ('5550050', 'Mentioned Bob', 'Contacts/Bob');
        """)

        per_msg, _ = hydration._resolve_android_mention_tokens(conn, [100], set(), {})
        conn.close()

        assert 100 in per_msg
        mentions = per_msg[100]
        assert "5550050" in mentions
        assert mentions["5550050"]["name"] == "Mentioned Bob"
        assert "me" in mentions
        assert mentions["me"]["is_self"] is True
        assert mentions["me"]["name"] == "You"

    def test_load_contact_chats_android_and_ios(self) -> None:
        # 1. Android chats: maps user to user/phone clean string
        wa_android = sqlite3.connect(":memory:")
        wa_android.executescript("""
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER);
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT);

            INSERT INTO jid VALUES (1, '5550060', 's.whatsapp.net');
            INSERT INTO chat VALUES (10, 1, NULL, 0);
        """)

        tok_to_c, _ = hydration._load_android_contact_chats(wa_android, None)
        assert tok_to_c.get("5550060") == "5550060"
        wa_android.close()

        # 2. iOS chats: maps user and jid to ZWACHATSESSION Z_PK string
        wa_ios = sqlite3.connect(":memory:")
        wa_ios.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZHIDDEN INTEGER,
                ZSESSIONTYPE INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (20, '15550070@s.whatsapp.net', 0, 0);
        """)

        tok_ios, jid_ios = hydration._load_ios_contact_chats(wa_ios, None)
        assert tok_ios.get("15550070") == "20"
        assert jid_ios.get("15550070@s.whatsapp.net") == "20"
        wa_ios.close()

    def test_resolve_ios_mention_jids_comprehensive(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TEMP TABLE _ios_contacts (jid TEXT PRIMARY KEY, full_name TEXT, phone_number TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (ZJID TEXT PRIMARY KEY, ZPUSHNAME TEXT);
            CREATE TABLE ZWACHATSESSION (ZCONTACTJID TEXT PRIMARY KEY, ZPARTNERNAME TEXT);
            ATTACH ':memory:' AS arch;
            CREATE TABLE arch.contacts (number TEXT PRIMARY KEY, display_name TEXT);
            ATTACH ':memory:' AS ext;
            CREATE TABLE ext.contact_metadata (contact_lid TEXT PRIMARY KEY, contact_push_name TEXT, contact_pn TEXT);

            INSERT INTO _ios_contacts VALUES ('15550080@s.whatsapp.net', 'Contact Frank', '15550080');
            INSERT INTO arch.contacts VALUES ('15550081', 'Archived Grace');
            INSERT INTO ZWAPROFILEPUSHNAME VALUES ('15550082@s.whatsapp.net', 'Push Helen');
            INSERT INTO ext.contact_metadata VALUES ('999001@lid', 'Metadata Ian', '15550083@s.whatsapp.net');
        """)

        jids = {
            "15550080@s.whatsapp.net",
            "15550081@s.whatsapp.net",
            "15550082@s.whatsapp.net",
            "999001@lid",
        }
        tok_to_chat = {"15550080": "c_frank"}
        jid_to_chat = {"15550080@s.whatsapp.net": "c_frank"}

        resolved_jids, _ = hydration._resolve_ios_mention_jids(
            conn, jids, set(), tok_to_chat, jid_to_chat
        )
        conn.close()

        assert "15550080@s.whatsapp.net" in resolved_jids
        assert resolved_jids["15550080@s.whatsapp.net"]["name"] == "Contact Frank"
        assert resolved_jids["15550080@s.whatsapp.net"]["chat_id"] == "c_frank"

        assert "15550081@s.whatsapp.net" in resolved_jids
        assert resolved_jids["15550081@s.whatsapp.net"]["name"] == "Archived Grace"

        assert "15550082@s.whatsapp.net" in resolved_jids
        assert resolved_jids["15550082@s.whatsapp.net"]["name"] == "Push Helen"

        assert "999001@lid" in resolved_jids
        assert resolved_jids["999001@lid"]["name"] == "Metadata Ian"

    def test_hydrate_mentions_integration(self) -> None:
        rows = [
            {
                "msg_id": 1,
                "mentions": json.dumps(
                    {
                        "5550090": {"name": "George", "is_self": False},
                        "me": {"name": "You", "is_self": True},
                    }
                ),
            }
        ]
        # Hydrate with mock context/caches
        hydration._hydrate_mentions(rows, "android", None, None)

        m = rows[0]["mentions"]
        assert isinstance(m, dict)
        assert m["5550090"]["name"] == "George"
        assert m["me"]["is_self"] is True
        assert m["me"]["chat_id"] is None

    def test_hydrate_quoted_rows_android_and_ios(self) -> None:
        # 1. Android quoted row with emoji media indicator
        android_rows = [
            {"msg_id": 1, "quoted_text": "📷 Photo caption"},
            {"msg_id": 2, "quoted_text": "Normal text"},
        ]
        hydration._hydrate_quoted_rows(android_rows, "android", None)
        assert android_rows[0]["quoted_has_thumb"] == 1
        assert "quoted_has_thumb" not in android_rows[1]

        # 2. iOS quoted row with _raw_metadata protobuf
        wa_ios = sqlite3.connect(":memory:")
        wa_ios.row_factory = sqlite3.Row
        wa_ios.executescript("""
            CREATE TABLE _ios_contacts (jid TEXT PRIMARY KEY, full_name TEXT, phone_number TEXT);
            INSERT INTO _ios_contacts VALUES ('15550099@s.whatsapp.net', 'Quoted Sender Name', '15550099');
        """)

        # Build raw metadata: field 5 stanza_id, field 6 sender jid, field 19 subfield 1 text
        meta = (
            build_field(5, 2, b"STANZA_Q_123")
            + build_field(6, 2, b"15550099@s.whatsapp.net")
            + build_field(19, 2, build_field(1, 2, b"Quoted message body"))
        )

        ios_rows = [{"msg_id": 10, "_raw_metadata": meta}]
        hydration._hydrate_quoted_rows(ios_rows, "ios", wa_ios)
        wa_ios.close()

        assert ios_rows[0]["quoted_stanza_id"] == "STANZA_Q_123"
        assert ios_rows[0]["quoted_text"] == "Quoted message body"
        assert ios_rows[0]["quoted_sender"] == "Quoted Sender Name"

    def test_backfill_recent_messages_insert_and_trim(self, tmp_path) -> None:
        db_path = tmp_path / "arc_backfill.db"
        with sqlite3.connect(db_path) as conn:
            conn.executescript("""
                CREATE TABLE recent_messages (
                    chat_id        TEXT NOT NULL,
                    chat_type      TEXT NOT NULL,
                    msg_id         INTEGER NOT NULL,
                    stanza_id      TEXT,
                    timestamp_ms   INTEGER NOT NULL,
                    sender         TEXT NOT NULL,
                    from_me        INTEGER NOT NULL,
                    archive_path   TEXT,
                    media_type     TEXT NOT NULL DEFAULT 'text',
                    media_name     TEXT,
                    text_body      TEXT NOT NULL,
                    quoted_text    TEXT,
                    quoted_sender  TEXT,
                    quoted_ts      INTEGER,
                    reactions      TEXT,
                    is_edited      INTEGER NOT NULL DEFAULT 0,
                    edited_ts      INTEGER,
                    is_view_once   INTEGER,
                    PRIMARY KEY (chat_id, chat_type, msg_id)
                );
            """)

        rows = [
            {
                "chat_id": "c1",
                "chat_type": "contact",
                "msg_id": 1,
                "timestamp_ms": 1000,
                "text_body": "Message 1",
                "media_type": "text",
                "sender": "Alice",
                "from_me": 0,
            },
            {
                "chat_id": "c1",
                "chat_type": "contact",
                "msg_id": 2,
                "timestamp_ms": 2000,
                "text_body": "Message 2",
                "media_type": "text",
                "sender": "Alice",
                "from_me": 0,
            },
        ]

        hydration._backfill_recent_messages(db_path, rows)

        with sqlite3.connect(db_path) as conn:
            saved = conn.execute(
                "SELECT msg_id, text_body FROM recent_messages ORDER BY msg_id"
            ).fetchall()
            assert len(saved) == 2
            assert saved[0][0] == 1
            assert saved[1][0] == 2
