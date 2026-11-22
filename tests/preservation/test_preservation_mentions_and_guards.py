"""Unit tests for mention hydration, account guards, and health checks in wab_archiver.preservation.

Tests hydrate_android_mentions_for_preservation, verify_account_match for iOS,
check_preserved_db_health, and masked LID filtering with 100% synthetic data.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from wab_archiver import preservation

logger = logging.getLogger("test_preservation")


class TestAndroidMentionHydration:
    """Tests for hydrate_android_mentions_for_preservation across self, mapped contacts, and LIDs."""

    def test_hydrate_android_mentions(self, tmp_path) -> None:
        db_path = tmp_path / "msgstore_mentions.db"
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE jid (
                _id INTEGER PRIMARY KEY,
                user TEXT,
                server TEXT,
                raw_string TEXT
            );
            CREATE TABLE jid_map (
                _id INTEGER PRIMARY KEY,
                lid_row_id INTEGER,
                jid_row_id INTEGER
            );
            CREATE TABLE lid_display_name (
                lid_row_id INTEGER PRIMARY KEY,
                display_name TEXT
            );
            CREATE TABLE message_mentions (
                _id INTEGER PRIMARY KEY,
                message_row_id INTEGER,
                jid_row_id INTEGER
            );
        """)

        # JID 1: Self (me)
        conn.execute("INSERT INTO jid VALUES (1, 'me', 's.whatsapp.net', 'lid_me')")
        # JID 2: Mapped contact
        conn.execute(
            "INSERT INTO jid VALUES (2, '5550100', 's.whatsapp.net', '5550100@s.whatsapp.net')"
        )
        # JID 3: LID with unmasked display name
        conn.execute("INSERT INTO jid VALUES (3, '9990100', 'lid', '9990100@lid')")
        conn.execute("INSERT INTO lid_display_name VALUES (3, 'Unmasked Name')")
        # JID 4: LID with masked display name
        conn.execute("INSERT INTO jid VALUES (4, '9990200', 'lid', '9990200@lid')")
        conn.execute("INSERT INTO lid_display_name VALUES (4, '+39••••••••04')")
        # JID 5: LID mapped to phone JID 6 via jid_map
        conn.execute("INSERT INTO jid VALUES (5, '9990300', 'lid', '9990300@lid')")
        conn.execute(
            "INSERT INTO jid VALUES (6, '5550300', 's.whatsapp.net', '5550300@s.whatsapp.net')"
        )
        conn.execute("INSERT INTO jid_map VALUES (1, 5, 6)")

        # Message 1 mentions JID 1 (self), JID 2 (mapped contact), JID 3 (unmasked LID), JID 4 (masked LID), JID 5 (mapped to phone)
        for jid_id in [1, 2, 3, 4, 5]:
            conn.execute(
                f"INSERT INTO message_mentions (message_row_id, jid_row_id) VALUES (100, {jid_id})"
            )
        conn.commit()

        contacts_map = {"5550100": "Alice Contact"}
        extracted_rows = [{"msg_id": 100, "text_body": "Hey @Alice @Unmasked check this"}]

        preservation.hydrate_android_mentions_for_preservation(
            conn, extracted_rows, "", contacts_map
        )
        conn.close()

        assert "mentions" in extracted_rows[0]
        mentions_dict = json.loads(extracted_rows[0]["mentions"])

        # JID 1 (self) resolves to "You"
        assert mentions_dict["me"]["name"] == "You"
        assert mentions_dict["me"]["is_self"] is True

        # JID 2 resolves to "Alice Contact" from contacts_map
        assert mentions_dict["5550100"]["name"] == "Alice Contact"

        # JID 3 resolves to "Unmasked Name" from lid_display_name
        assert mentions_dict["9990100"]["name"] == "Unmasked Name"

        # JID 4 has masked display name -> falls back to Unknown
        assert "(Unknown)" in mentions_dict["9990200"]["name"]

        # JID 5 mapped to phone 5550300 -> resolves to +5550300
        assert mentions_dict["9990300"]["name"] == "+5550300"


class TestMaskedLidDisplay:
    """Tests for _is_masked_lid_display."""

    def test_masked_lid_display_detection(self) -> None:
        assert preservation._is_masked_lid_display(None) is True
        assert preservation._is_masked_lid_display("") is True
        assert preservation._is_masked_lid_display("   ") is True

        # Bullet characters
        assert preservation._is_masked_lid_display("+39••••••••04") is True
        assert preservation._is_masked_lid_display("+1 555 ∙∙∙∙ 12") is True
        assert preservation._is_masked_lid_display("+44\u2219\u2219\u221999") is True

        # Clean unmasked names
        assert preservation._is_masked_lid_display("John Doe") is False
        assert preservation._is_masked_lid_display("+15551234567") is False


class TestVerifyAccountMatchIOS:
    """Tests for verify_account_match on iOS."""

    def test_ios_verify_account_match(self, tmp_path) -> None:
        old_db = tmp_path / "old_ios.sqlite"
        new_db = tmp_path / "new_ios.sqlite"

        # Case 1: Missing ZWACHATSESSION table -> returns True safely
        with sqlite3.connect(old_db) as conn:
            conn.execute("CREATE TABLE dummy (id INT)")
        with sqlite3.connect(new_db) as conn:
            conn.execute("CREATE TABLE dummy (id INT)")
        assert preservation.verify_account_match(str(old_db), str(new_db), "ios", logger) is True

        # Case 2: Fewer than 5 chats -> returns True
        with sqlite3.connect(old_db) as conn:
            conn.execute("CREATE TABLE ZWACHATSESSION (ZCONTACTJID TEXT)")
            conn.executemany(
                "INSERT INTO ZWACHATSESSION VALUES (?)",
                [("111@s.whatsapp.net",), ("222@s.whatsapp.net",)],
            )
        with sqlite3.connect(new_db) as conn:
            conn.execute("CREATE TABLE ZWACHATSESSION (ZCONTACTJID TEXT)")
            conn.executemany(
                "INSERT INTO ZWACHATSESSION VALUES (?)",
                [("333@s.whatsapp.net",), ("444@s.whatsapp.net",)],
            )
        assert preservation.verify_account_match(str(old_db), str(new_db), "ios", logger) is True

        # Case 3: >= 5 chats with overlap -> returns True
        with sqlite3.connect(old_db) as conn:
            conn.executemany(
                "INSERT INTO ZWACHATSESSION VALUES (?)",
                [("555@s.whatsapp.net",), ("666@s.whatsapp.net",), ("777@s.whatsapp.net",)],
            )
        with sqlite3.connect(new_db) as conn:
            conn.executemany(
                "INSERT INTO ZWACHATSESSION VALUES (?)",
                [("555@s.whatsapp.net",), ("888@s.whatsapp.net",), ("999@s.whatsapp.net",)],
            )
        assert preservation.verify_account_match(str(old_db), str(new_db), "ios", logger) is True

        # Case 4: >= 5 chats with 0 overlap -> returns False
        unrelated_db = tmp_path / "unrelated_ios.sqlite"
        with sqlite3.connect(unrelated_db) as conn:
            conn.execute("CREATE TABLE ZWACHATSESSION (ZCONTACTJID TEXT)")
            conn.executemany(
                "INSERT INTO ZWACHATSESSION VALUES (?)",
                [
                    ("a1@s.whatsapp.net",),
                    ("a2@s.whatsapp.net",),
                    ("a3@s.whatsapp.net",),
                    ("a4@s.whatsapp.net",),
                    ("a5@s.whatsapp.net",),
                ],
            )
        assert (
            preservation.verify_account_match(str(old_db), str(unrelated_db), "ios", logger)
            is False
        )


class TestPreservedDbHealth:
    """Tests for check_preserved_db_health."""

    def test_healthy_and_corrupt_db_health(self, tmp_path) -> None:
        healthy_dir = tmp_path / "healthy"
        healthy_dir.mkdir()
        with preservation.open_preservation_db(str(healthy_dir)) as conn:
            # Healthy DB completes without raising any exception
            preservation.check_preserved_db_health(conn, logger)

        # Foreign key violation raises IntegrityError
        fk_conn = sqlite3.connect(":memory:")
        fk_conn.execute("PRAGMA foreign_keys = ON")
        fk_conn.execute("CREATE TABLE parent (id INT PRIMARY KEY)")
        fk_conn.execute("CREATE TABLE child (id INT, p_id INT REFERENCES parent(id))")
        fk_conn.execute("PRAGMA foreign_keys = OFF")
        fk_conn.execute("INSERT INTO child VALUES (1, 999)")
        fk_conn.execute("PRAGMA foreign_keys = ON")
        import pytest

        with pytest.raises(sqlite3.IntegrityError, match="foreign_key_check failed"):
            preservation.check_preserved_db_health(fk_conn, logger)


def _make_proto_field(field_num: int, wire_type: int, payload: bytes) -> bytes:
    tag = (field_num << 3) | wire_type
    out = bytearray()
    while tag > 0x7F:
        out.append((tag & 0x7F) | 0x80)
        tag >>= 7
    out.append(tag)
    if wire_type == 2:
        length = len(payload)
        while length > 0x7F:
            out.append((length & 0x7F) | 0x80)
            length >>= 7
        out.append(length)
        out.extend(payload)
    else:
        out.extend(payload)
    return bytes(out)


class TestExtractZmetadataMentions:
    """Tests for _extract_zmetadata_mentions protobuf wire parsing."""

    def test_empty_or_invalid_inputs(self) -> None:
        assert preservation._extract_zmetadata_mentions(None) == []
        assert preservation._extract_zmetadata_mentions(b"") == []
        assert preservation._extract_zmetadata_mentions("not_bytes") == []  # type: ignore

    def test_extract_tag8_and_tag15_jids(self) -> None:
        blob = bytearray()
        # Wire type 0 (varint)
        blob.extend(_make_proto_field(1, 0, b"\x05"))
        # Wire type 1 (fixed64)
        blob.extend(_make_proto_field(2, 1, b"\x01\x02\x03\x04\x05\x06\x07\x08"))
        # Wire type 5 (fixed32)
        blob.extend(_make_proto_field(3, 5, b"\x0a\x0b\x0c\x0d"))
        # Tag 8 JID (wire type 2)
        blob.extend(_make_proto_field(8, 2, b"15551234567@s.whatsapp.net"))
        # Tag 15 JID (wire type 2)
        blob.extend(_make_proto_field(15, 2, b"15559876543@s.whatsapp.net"))
        # Tag 15 Duplicate JID (should deduplicate)
        blob.extend(_make_proto_field(15, 2, b"15559876543@s.whatsapp.net"))
        # Tag without @ (ignored)
        blob.extend(_make_proto_field(8, 2, b"not_a_jid"))

        jids = preservation._extract_zmetadata_mentions(bytes(blob))
        assert jids == ["15551234567@s.whatsapp.net", "15559876543@s.whatsapp.net"]

    def test_truncated_bytes_gracefully_halt(self) -> None:
        # Field 8 length claims 20 bytes, but only 5 provided
        truncated = bytes([0x42, 20, 0x31, 0x32, 0x33, 0x34, 0x35])
        assert preservation._extract_zmetadata_mentions(truncated) == []


class TestIOSMentionHydration:
    """Tests for hydrate_ios_mentions_for_preservation."""

    def test_empty_rows_or_missing_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        rows = [{"msg_id": 1}]
        # Missing ZWAMEDIAITEM
        preservation.hydrate_ios_mentions_for_preservation(
            conn, rows, {"ZWAMESSAGE"}, {"ZMETADATA"}, {}
        )
        assert "mentions" not in rows[0]
        # Empty rows
        preservation.hydrate_ios_mentions_for_preservation(
            conn, [], {"ZWAMEDIAITEM"}, {"ZMETADATA"}, {}
        )
        conn.close()

    def test_hydrate_ios_mentions_full_flow(self, tmp_path) -> None:
        old_db_path = tmp_path / "old_ios_mentions.sqlite"
        with sqlite3.connect(old_db_path) as oconn:
            oconn.executescript("""
                CREATE TABLE ZWAMESSAGE (
                    Z_PK INTEGER PRIMARY KEY,
                    ZMEDIAITEM INTEGER
                );
                CREATE TABLE ZWAMEDIAITEM (
                    Z_PK INTEGER PRIMARY KEY,
                    ZMETADATA BLOB
                );
                CREATE TABLE ZWAPROFILEPUSHNAME (
                    ZJID TEXT,
                    ZPUSHNAME TEXT
                );
                CREATE TABLE ZWACHATSESSION (
                    ZCONTACTJID TEXT,
                    ZPARTNERNAME TEXT
                );
            """)

            # JID 1: Has push name
            oconn.execute(
                "INSERT INTO ZWAPROFILEPUSHNAME VALUES ('5550001@s.whatsapp.net', 'Alice Push')"
            )
            # JID 2: Has partner name in chat session
            oconn.execute(
                "INSERT INTO ZWACHATSESSION VALUES ('5550002@s.whatsapp.net', 'Bob Partner')"
            )

            # Metadata blob with JID 1 (push), JID 2 (partner), JID 3 (phone fallback), JID 4 (LID unknown)
            blob = bytearray()
            blob.extend(_make_proto_field(8, 2, b"5550001@s.whatsapp.net"))
            blob.extend(_make_proto_field(15, 2, b"5550002@s.whatsapp.net"))
            blob.extend(_make_proto_field(15, 2, b"5550003@s.whatsapp.net"))
            blob.extend(_make_proto_field(15, 2, b"9990001@lid"))

            oconn.execute("INSERT INTO ZWAMEDIAITEM VALUES (10, ?)", (bytes(blob),))
            oconn.execute("INSERT INTO ZWAMESSAGE VALUES (100, 10)")
            oconn.commit()

        conn = sqlite3.connect(":memory:")
        conn.execute(f"ATTACH DATABASE '{old_db_path}' AS old_db")

        extracted_rows = [{"msg_id": 100, "text_body": "Hello everyone"}]
        old_tables = {"ZWAMESSAGE", "ZWAMEDIAITEM", "ZWAPROFILEPUSHNAME", "ZWACHATSESSION"}
        mi_cols = {"ZMETADATA"}
        contacts_map = {"5550003": "Charlie Contact"}

        preservation.hydrate_ios_mentions_for_preservation(
            conn, extracted_rows, old_tables, mi_cols, contacts_map
        )
        conn.close()

        assert "mentions" in extracted_rows[0]
        mentions = json.loads(extracted_rows[0]["mentions"])

        assert mentions["5550001"]["name"] == "Alice Push"
        assert mentions["5550002"]["name"] == "Bob Partner"
        assert mentions["5550003"]["name"] == "Charlie Contact"
        assert mentions["9990001"]["name"] == "9990001 (Unknown)"


class TestEnsureIOSDiffIndexes:
    """Tests for _ensure_ios_diff_indexes."""

    def test_ensure_ios_diff_indexes_creates_required_indexes(self, tmp_path) -> None:
        old_db_file = tmp_path / "old_idx.sqlite"
        with sqlite3.connect(old_db_file) as oconn:
            oconn.execute("CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZSTANZAID TEXT)")

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZSTANZAID TEXT, ZCHATSESSION INTEGER, ZMESSAGEDATE REAL, ZISFROMME INTEGER, ZMESSAGETYPE INTEGER)"
        )
        conn.execute(f"ATTACH DATABASE '{old_db_file}' AS old_db")

        preservation._ensure_ios_diff_indexes(conn)

        indexes = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        assert "idx_pres_cur_stanza" in indexes
        assert "idx_pres_cur_fallback" in indexes
        assert "idx_pres_cur_revoked" in indexes
        conn.close()


class TestLoadArchiveContactsMap:
    """Tests for _load_archive_contacts_map."""

    def test_load_archive_contacts_map(self, tmp_path) -> None:
        assert preservation._load_archive_contacts_map(None) == {}
        assert preservation._load_archive_contacts_map(tmp_path / "missing.db") == {}

        arc_db = tmp_path / "test_contacts.db"
        with sqlite3.connect(arc_db) as aconn:
            aconn.execute("CREATE TABLE contacts (number TEXT, display_name TEXT, folder TEXT)")
            aconn.execute(
                "INSERT INTO contacts VALUES ('+1 (555) 0199', 'Alice Test', 'Contacts/Alice')"
            )
            aconn.execute("INSERT INTO contacts VALUES ('5550200', '', 'Contacts/BobFolder')")
            aconn.execute("INSERT INTO contacts VALUES (NULL, 'Empty', 'Contacts/Empty')")

        cmap = preservation._load_archive_contacts_map(arc_db)
        assert cmap["15550199"] == "Alice Test"
        assert cmap["5550200"] == "Contacts/BobFolder"


class TestHydrateAndroidLinkPreviews:
    """Tests for hydrate_android_link_previews."""

    def test_hydrate_android_link_previews(self, tmp_path) -> None:
        db_path = tmp_path / "link_preview.db"
        with sqlite3.connect(db_path) as conn:
            conn.executescript("""
                CREATE TABLE message_text (
                    message_row_id INTEGER PRIMARY KEY,
                    page_title TEXT,
                    description TEXT,
                    url TEXT,
                    preview_type INTEGER
                );
                CREATE TABLE message_thumbnail (
                    message_row_id INTEGER PRIMARY KEY,
                    thumbnail BLOB
                );
                INSERT INTO message_text VALUES (10, 'Title 1', 'Desc 1', 'https://example.com', 1);
                INSERT INTO message_thumbnail VALUES (10, X'DEADBEEF');

                -- Row 20 has text_body fallback for URL
                INSERT INTO message_text VALUES (20, 'Title 2', 'Desc 2', NULL, 0);
            """)

        conn = sqlite3.connect(db_path)
        extracted = [
            {"msg_id": 10, "media_type": "link", "service_metadata": None},
            {
                "msg_id": 20,
                "media_type": "text",
                "text_body": "Visit www.sample.org/path now",
                "service_metadata": '{"existing":1}',
            },
        ]

        preservation.hydrate_android_link_previews(
            conn, extracted, "", has_message_text=True, has_message_thumbnail=True
        )
        conn.close()

        # Row 10
        meta10 = json.loads(extracted[0]["service_metadata"])
        assert meta10["link_preview"]["title"] == "Title 1"
        assert meta10["link_preview"]["url"] == "https://example.com"
        assert "thumbnail_b64" in meta10["link_preview"]

        # Row 20
        meta20 = json.loads(extracted[1]["service_metadata"])
        assert meta20["existing"] == 1
        assert meta20["link_preview"]["title"] == "Title 2"
        assert meta20["link_preview"]["url"] == "https://www.sample.org/path"


class TestPreserveDeletedMessagesOrchestrator:
    """Tests for high-level preserve_deleted_messages orchestrator."""

    def test_missing_files_or_unsupported_platform(self, tmp_path) -> None:
        old_db = tmp_path / "old.db"
        new_db = tmp_path / "new.db"

        # Missing old_db
        res = preservation.preserve_deleted_messages(
            str(old_db), str(new_db), str(tmp_path), str(tmp_path / "arc.db"), "android", logger
        )
        assert res.messages == 0 and res.chats == 0

        # Missing new_db
        old_db.write_text("dummy")
        res = preservation.preserve_deleted_messages(
            str(old_db), str(new_db), str(tmp_path), str(tmp_path / "arc.db"), "android", logger
        )
        assert res.messages == 0 and res.chats == 0

        # Unsupported platform
        new_db.write_text("dummy")
        from unittest.mock import patch

        with patch.object(preservation, "verify_account_match", return_value=True):
            res = preservation.preserve_deleted_messages(
                str(old_db), str(new_db), str(tmp_path), str(tmp_path / "arc.db"), "windows", logger
            )
            assert res.messages == 0 and res.chats == 0

    def test_android_and_ios_orchestrator_success(self, tmp_path) -> None:
        old_db = tmp_path / "old.db"
        new_db = tmp_path / "new.db"
        old_db.write_text("dummy")
        new_db.write_text("dummy")
        arc_db = tmp_path / "arc.db"

        from unittest.mock import patch

        with (
            patch.object(preservation, "verify_account_match", return_value=True),
            patch(
                "wab_archiver.archive_db.open_archive_db", return_value=sqlite3.connect(":memory:")
            ),
            patch.object(
                preservation,
                "diff_and_extract_android",
                return_value=[
                    {
                        "chat_id": "c1",
                        "stanza_id": "s1",
                        "from_me": 0,
                        "media_type": "text",
                        "text_body": "msg",
                    }
                ],
            ),
            patch.object(
                preservation,
                "diff_and_extract_android_calls",
                return_value=[
                    {
                        "call_id": "call_1",
                        "timestamp_ms": 1000,
                        "caller_jid": "55501",
                        "call_result": 1,
                        "video_call": 0,
                        "duration": 30,
                    }
                ],
            ),
            patch.object(
                preservation,
                "record_preserved_messages",
                return_value=preservation.PreservationResult(1, 1),
            ) as mock_rec_msg,
            patch.object(preservation, "record_preserved_calls", return_value=1) as mock_rec_call,
        ):
            res = preservation.preserve_deleted_messages(
                str(old_db), str(new_db), str(tmp_path), str(arc_db), "android", logger
            )
            assert res.messages == 1
            assert res.chats == 1
            assert mock_rec_msg.called
            assert mock_rec_call.called

    def test_empty_diff_returns_zero(self, tmp_path) -> None:
        old_db = tmp_path / "old.db"
        new_db = tmp_path / "new.db"
        old_db.write_text("dummy")
        new_db.write_text("dummy")

        from unittest.mock import patch

        with (
            patch.object(preservation, "verify_account_match", return_value=True),
            patch(
                "wab_archiver.archive_db.open_archive_db", return_value=sqlite3.connect(":memory:")
            ),
            patch.object(preservation, "diff_and_extract_ios", return_value=[]),
            patch.object(preservation, "diff_and_extract_ios_calls", return_value=[]),
        ):
            res = preservation.preserve_deleted_messages(
                str(old_db), str(new_db), str(tmp_path), str(tmp_path / "arc.db"), "ios", logger
            )
            assert res.messages == 0 and res.chats == 0


class TestPreserveIncrementalDeletions:
    """Tests for preserve_incremental_deletions across schema variants."""

    def test_empty_verified_ids_returns_zero(self) -> None:
        conn = sqlite3.connect(":memory:")
        res = preservation.preserve_incremental_deletions(conn, [], "/tmp", logger)
        assert res == 0
        conn.close()
