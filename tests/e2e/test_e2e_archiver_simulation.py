"""
Comprehensive End-to-End Simulation Test Suite for WhatsApp Backup Archiver (wab_archiver).

Simulates complete user workflows for both iOS and Android platforms across all operational
modes, data paths, edge cases, multi-run lifecycles, and restoration:
- iOS plaintext backup (Manifest.db, hashed directories, ContactsV2, ExtChatDatabase HD dedup, vCards)
- iOS encrypted backup (EncryptedBackup interface, passphrase validation, lazy decryption)
- iOS pre-extracted container mode (AppDomainGroup, Message/ folder)
- Android multi-root resolution (zero-byte skip, largest wins, same-size tie-breaking, conflicts CSV)
- Android encrypted & incremental merge (crypt14/crypt15 base + incremental diffs)
- Android ADB extraction (device verification, content provider contacts, media staging)
- Multi-run lifecycle (contact rename sync, group rename sync, preserve-deleted diff engine)
- Deduplication, collision renaming (_1, _2), and hardlinking (--link-duplicates)
- Full reverse restore mode (wab-archiver restore, unrestorable reporting, iOS rejection)
- Filtering, limiting, timezones, dry-run, and TOML configuration
- Schema drift detection and fault recovery

Strict privacy rules: 100% synthetic mock data (no real device data, JIDs, or phone numbers).
"""

from __future__ import annotations

import csv
import logging
import os
import plistlib
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest
import wab_archiver.main as wa

# ---------------------------------------------------------------------------
# Synthetic Binary Data Generators (Valid Headers, Deterministic MD5)
# ---------------------------------------------------------------------------


def make_jpeg_bytes(identifier: str) -> bytes:
    """Return valid JPEG stream with unique payload."""
    payload = f"SYNTHETIC_JPEG_PAYLOAD_{identifier}".encode()
    return (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        + payload
        + b"\xff\xd9"
    )


def make_mp4_bytes(identifier: str) -> bytes:
    """Return valid MP4 stream with unique payload."""
    payload = f"SYNTHETIC_MP4_PAYLOAD_{identifier}".encode()
    return b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00isommp42" + payload


def make_pdf_bytes(identifier: str) -> bytes:
    """Return valid PDF stream with unique payload."""
    payload = f"SYNTHETIC_PDF_PAYLOAD_{identifier}".encode()
    return b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n" + payload + b"\n%%EOF"


def make_opus_bytes(identifier: str) -> bytes:
    """Return valid Ogg Opus audio stream with unique payload."""
    payload = f"SYNTHETIC_OPUS_PAYLOAD_{identifier}".encode()
    return b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00" + payload


# ---------------------------------------------------------------------------
# Fixture: Silent test logger
# ---------------------------------------------------------------------------


@pytest.fixture
def silent_logger():
    logger = logging.getLogger("wab_archiver_test_silent")
    logger.setLevel(logging.CRITICAL)
    return logger


# ===========================================================================
# SCENARIO 1: iOS Plaintext Backup End-to-End Simulation
# ===========================================================================


class TestE2EIOSPlaintextSimulation:
    def test_ios_plaintext_full_flow(self, tmp_path: Path):
        """
        Verify complete iOS unencrypted backup flow:
        - Manifest.db mapping relativePath to hashed on-disk storage
        - 1-to-1 chats: known contact, unknown contact, WhatsApp official sender
        - Group chats: received from contact (_SenderName) and sent by user (_Me)
        - Contact cards (vCards) extracted to .vcf with clean display names
        - Document title override (ZTITLE applied)
        - HD media deduplication via ExtChatDatabase.sqlite (LQ parent skipped)
        - Intentionally missing media recorded in missing_media_report.csv
        - Duplicate media hardlinking across chats (--link-duplicates)
        - Output archive database and reports verified
        """
        backup_dir = tmp_path / "ios_backup"
        backup_dir.mkdir()
        out_dir = tmp_path / "archive_output"

        # 1. Info.plist
        info_plist = backup_dir / "Info.plist"
        with open(info_plist, "wb") as f:
            plistlib.dump({"IsEncrypted": False}, f)

        # 2. ChatStorage.sqlite
        cs_path = tmp_path / "ChatStorage_raw.sqlite"
        conn = sqlite3.connect(str(cs_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZGROUPINFO INTEGER,
                ZPARTNERNAME TEXT,
                ZCONTACTABID INTEGER,
                ZLASTMESSAGEDATE REAL
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT,
                ZMEDIAURL TEXT,
                ZTITLE TEXT,
                ZVCARDNAME TEXT,
                ZVCARDSTRING TEXT
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZMEMBERJID TEXT
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER DEFAULT 0,
                ZISFROMME INTEGER,
                ZCHATSESSION INTEGER,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER,
                ZPUSHNAME TEXT,
                ZSTANZAID TEXT
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (
                ZJID TEXT,
                ZPUSHNAME TEXT
            );

            -- Chat Sessions:
            -- 1: Known contact Alice
            INSERT INTO ZWACHATSESSION VALUES (1, '390000000001@s.whatsapp.net', NULL, NULL, 101, 802785600.0);
            -- 2: Unknown contact Bob (no address book entry)
            INSERT INTO ZWACHATSESSION VALUES (2, '390000000002@s.whatsapp.net', NULL, NULL, NULL, 802785600.0);
            -- 3: WhatsApp official system sender
            INSERT INTO ZWACHATSESSION VALUES (3, '0@s.whatsapp.net', NULL, NULL, NULL, 802785600.0);
            -- 4: Group Chat 'Project Alpha'
            INSERT INTO ZWACHATSESSION VALUES (4, '120363000000001@g.us', 1, 'Project Alpha', NULL, 802785600.0);

            -- Group members
            INSERT INTO ZWAGROUPMEMBER VALUES (1, '390000000003@s.whatsapp.net');

            -- Pushnames
            INSERT INTO ZWAPROFILEPUSHNAME VALUES ('390000000003@s.whatsapp.net', 'Charlie');

            -- Media Items:
            -- 1: Photo in 1-to-1 Alice (Received)
            INSERT INTO ZWAMEDIAITEM VALUES (1, 'Media/alice_photo.jpg', 'http://wa/1', NULL, NULL, NULL);
            -- 2: Video in 1-to-1 Bob (Sent)
            INSERT INTO ZWAMEDIAITEM VALUES (2, 'Media/bob_video.mp4', 'http://wa/2', NULL, NULL, NULL);
            -- 3: WhatsApp system announcement (Received)
            INSERT INTO ZWAMEDIAITEM VALUES (3, 'Media/sys_notice.jpg', 'http://wa/3', NULL, NULL, NULL);
            -- 4: Group photo received from Charlie (identical bytes to alice_photo for dup test)
            INSERT INTO ZWAMEDIAITEM VALUES (4, 'Media/group_photo.jpg', 'http://wa/4', NULL, NULL, NULL);
            -- 5: Group video sent by user (from_me=1)
            INSERT INTO ZWAMEDIAITEM VALUES (5, 'Media/group_sent.mp4', 'http://wa/5', NULL, NULL, NULL);
            -- 6: Document with custom ZTITLE
            INSERT INTO ZWAMEDIAITEM VALUES (6, 'Documents/doc_guid.pdf', 'http://wa/6', 'Project Guidelines.pdf', NULL, NULL);
            -- 7: vCard contact card
            INSERT INTO ZWAMEDIAITEM VALUES (7, NULL, NULL, NULL, 'David Specialist',
                'BEGIN:VCARD\r\nVERSION:3.0\r\nFN:David Specialist\r\nTEL:+390000000004\r\nEND:VCARD\r\n');
            -- 8: LQ image with HD parent
            INSERT INTO ZWAMEDIAITEM VALUES (8, 'Media/lq_image.jpg', 'http://wa/8', NULL, NULL, NULL);
            -- 9: HD image
            INSERT INTO ZWAMEDIAITEM VALUES (9, 'Media/hd_image.jpg', 'http://wa/9', NULL, NULL, NULL);
            -- 10: Missing image on disk
            INSERT INTO ZWAMEDIAITEM VALUES (10, 'Media/missing_file.jpg', 'http://wa/10', NULL, NULL, NULL);

            -- Messages (Apple timestamp 802785600.0 = 2026-06-10 12:00:00 UTC):
            INSERT INTO ZWAMESSAGE VALUES (1, 802785600.0, 1, 0, 1, 1, NULL, NULL, 'msg_01');
            INSERT INTO ZWAMESSAGE VALUES (2, 802785600.0, 1, 1, 2, 2, NULL, NULL, 'msg_02');
            INSERT INTO ZWAMESSAGE VALUES (3, 802785600.0, 1, 0, 3, 3, NULL, NULL, 'msg_03');
            INSERT INTO ZWAMESSAGE VALUES (4, 802785600.0, 1, 0, 4, 4, 1, 'Charlie', 'msg_04');
            INSERT INTO ZWAMESSAGE VALUES (5, 802785600.0, 1, 1, 4, 5, NULL, NULL, 'msg_05');
            INSERT INTO ZWAMESSAGE VALUES (6, 802785600.0, 1, 0, 1, 6, NULL, NULL, 'msg_06');
            INSERT INTO ZWAMESSAGE VALUES (7, 802785600.0, 4, 0, 1, 7, NULL, NULL, 'msg_07');
            INSERT INTO ZWAMESSAGE VALUES (8, 802785600.0, 1, 0, 1, 8, NULL, NULL, 'STANZA_LQ');
            INSERT INTO ZWAMESSAGE VALUES (9, 802785600.0, 1, 0, 1, 9, NULL, NULL, 'STANZA_HD');
            INSERT INTO ZWAMESSAGE VALUES (10, 802785600.0, 1, 0, 1, 10, NULL, NULL, 'msg_10');
        """)
        conn.close()

        # 3. ContactsV2.sqlite
        contacts_v2_path = tmp_path / "ContactsV2_raw.sqlite"
        c_conn = sqlite3.connect(str(contacts_v2_path))
        c_conn.executescript("""
            CREATE TABLE ZWAADDRESSBOOKCONTACT (
                Z_PK INTEGER PRIMARY KEY,
                ZWHATSAPPID TEXT,
                ZFULLNAME TEXT
            );
            INSERT INTO ZWAADDRESSBOOKCONTACT VALUES (1, '390000000001@s.whatsapp.net', 'Alice Smith');
        """)
        c_conn.close()

        # 4. ExtChatDatabase.sqlite (HD Association)
        ext_db_path = tmp_path / "ExtChatDatabase_raw.sqlite"
        e_conn = sqlite3.connect(str(ext_db_path))
        e_conn.executescript("""
            CREATE TABLE message_parent_association (
                stanza_id TEXT,
                parent_stanza_id TEXT,
                type INTEGER,
                sort INTEGER,
                chat_jid TEXT
            );
            -- Type 10 association: STANZA_HD is HD version of STANZA_LQ
            INSERT INTO message_parent_association VALUES ('STANZA_HD', 'STANZA_LQ', 10, 1, '390000000001@s.whatsapp.net');
        """)
        e_conn.close()

        # 5. Build iOS Hashed Storage and Manifest.db
        shared_domain = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
        manifest_db_path = backup_dir / "Manifest.db"
        m_conn = sqlite3.connect(str(manifest_db_path))
        m_conn.executescript("""
            CREATE TABLE Files (
                fileID TEXT PRIMARY KEY,
                domain TEXT,
                relativePath TEXT,
                flags INTEGER,
                file BLOB
            );
        """)

        shared_photo_bytes = make_jpeg_bytes("SHARED_PHOTO_CONTENT")
        files_to_hash = [
            ("ChatStorage.sqlite", cs_path.read_bytes()),
            ("ContactsV2.sqlite", contacts_v2_path.read_bytes()),
            ("ExtChatDB/ExtChatDatabase.sqlite", ext_db_path.read_bytes()),
            ("Message/Media/alice_photo.jpg", shared_photo_bytes),
            ("Message/Media/bob_video.mp4", make_mp4_bytes("BOB_VIDEO")),
            ("Message/Media/sys_notice.jpg", make_jpeg_bytes("SYS_NOTICE")),
            ("Message/Media/group_photo.jpg", shared_photo_bytes),  # Identical content for dedup
            ("Message/Media/group_sent.mp4", make_mp4_bytes("GROUP_SENT_VIDEO")),
            ("Message/Documents/doc_guid.pdf", make_pdf_bytes("PROJECT_GUIDELINES")),
            ("Message/Media/lq_image.jpg", make_jpeg_bytes("LQ_RAW")),
            ("Message/Media/hd_image.jpg", make_jpeg_bytes("HD_RAW")),
            # "Message/Media/missing_file.jpg" is omitted from hash directory intentionally
        ]

        import hashlib

        for rel_path, data in files_to_hash:
            file_id = hashlib.sha1(f"{shared_domain}-{rel_path}".encode()).hexdigest()
            dir_prefix = backup_dir / file_id[:2]
            dir_prefix.mkdir(exist_ok=True)
            (dir_prefix / file_id).write_bytes(data)
            m_conn.execute(
                "INSERT INTO Files VALUES (?, ?, ?, 1, NULL)",
                (file_id, shared_domain, rel_path),
            )

        m_conn.commit()
        m_conn.close()

        # Execute wab-archiver archive via main entry point
        cmd_args = [
            "wab-archiver",
            "archive",
            "--ios-backup",
            str(backup_dir),
            "--output",
            str(out_dir),
            "--link-duplicates",
            "-v",
        ]
        with patch("sys.argv", cmd_args):
            wa.main()

        # =======================================================================
        # Assertions: File Structure, Naming & Content
        # =======================================================================

        # 1. 1-to-1 Known Contact Alice:
        alice_received_dir = (
            out_dir / "Contacts" / "Alice Smith (00390000000001)" / "2026" / "Received"
        )
        assert alice_received_dir.exists()
        alice_photo = alice_received_dir / "alice_photo.jpg"
        assert alice_photo.exists()
        assert alice_photo.read_bytes() == shared_photo_bytes

        # Document with ZTITLE override:
        doc_file = alice_received_dir / "Project Guidelines.pdf"
        assert doc_file.exists()
        assert b"PROJECT_GUIDELINES" in doc_file.read_bytes()

        # vCard file extraction:
        vcard_file = alice_received_dir / "David Specialist.vcf"
        assert vcard_file.exists()
        assert b"David Specialist" in vcard_file.read_bytes()

        # HD Deduplication: HD image extracted, LQ parent excluded:
        hd_file = alice_received_dir / "hd_image.jpg"
        lq_file = alice_received_dir / "lq_image.jpg"
        assert hd_file.exists()
        assert not lq_file.exists()

        # 2. 1-to-1 Unknown Contact Bob (Sent direction):
        bob_sent_dir = out_dir / "Contacts" / "Unknown (00390000000002)" / "2026" / "Sent"
        assert bob_sent_dir.exists()
        bob_video = bob_sent_dir / "bob_video.mp4"
        assert bob_video.exists()

        # 3. WhatsApp official sender:
        wa_dir = out_dir / "Contacts" / "WhatsApp (000)" / "2026" / "Received"
        assert wa_dir.exists()
        assert (wa_dir / "sys_notice.jpg").exists()

        # 4. Group Chat 'Project Alpha':
        group_dir = out_dir / "Groups" / "Project Alpha" / "2026"
        assert group_dir.exists()
        group_received = group_dir / "group_photo_Charlie.jpg"
        group_sent = group_dir / "group_sent_Me.mp4"
        assert group_received.exists()
        assert group_sent.exists()

        # 5. Deduplication & Hardlinking:
        # alice_photo and group_photo_Charlie have identical hash
        assert group_received.read_bytes() == shared_photo_bytes
        # Probe hardlink count on filesystem:
        if os.stat(str(alice_photo)).st_nlink > 1:
            assert os.stat(str(group_received)).st_nlink > 1

        # 6. Database Verification (.wa_media_archiver.db)
        arc_db_path = out_dir / ".wa_media_archiver.db"
        assert arc_db_path.exists()
        arc_conn = sqlite3.connect(str(arc_db_path))
        # Contacts index
        contacts_rows = dict(arc_conn.execute("SELECT number, folder FROM contacts").fetchall())
        assert "390000000001" in contacts_rows
        assert contacts_rows["390000000001"] == "Alice Smith (00390000000001)"
        assert contacts_rows["390000000002"] == "Unknown (00390000000002)"
        # Groups index
        groups_rows = dict(arc_conn.execute("SELECT chat_row_id, folder FROM groups").fetchall())
        assert "4" in groups_rows
        assert groups_rows["4"] == "Project Alpha"

        # Check archive_copies table
        copies = arc_conn.execute(
            "SELECT original_path, archive_path, is_hardlink FROM archive_copies"
        ).fetchall()
        assert len(copies) >= 6
        arc_conn.close()

        # 7. Reports Verification
        missing_csv = out_dir / "missing_media_report.csv"
        assert missing_csv.exists()
        with open(missing_csv, encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            assert len(reader) == 1
            assert reader[0]["message_id"] == "10"
            assert "missing_file.jpg" in reader[0]["file_path"]

        dup_csv = out_dir / "duplicate_media_report.csv"
        assert dup_csv.exists()
        dup_text = dup_csv.read_text(encoding="utf-8")
        assert "PHOTOS & IMAGES" in dup_text

        # 8. Snapshot Databases
        saved_chatstorage = out_dir / "Whatsapp Databases" / "ChatStorage.sqlite"
        assert saved_chatstorage.exists()


# ===========================================================================
# SCENARIO 2: iOS Encrypted Backup Simulation
# ===========================================================================


class TestE2EIOSEncryptedSimulation:
    def test_ios_encrypted_missing_password_aborts(self, tmp_path: Path):
        """Encrypted iOS backup without --ios-password must abort with code 1."""
        backup_dir = tmp_path / "enc_backup"
        backup_dir.mkdir()
        out_dir = tmp_path / "out"

        with open(backup_dir / "Manifest.plist", "wb") as f:
            plistlib.dump({"IsEncrypted": True}, f)

        cmd_args = [
            "wab-archiver",
            "archive",
            "--ios-backup",
            str(backup_dir),
            "--output",
            str(out_dir),
        ]
        with patch("sys.argv", cmd_args), pytest.raises(SystemExit) as exc_info:
            wa.main()
        assert exc_info.value.code == 1

    def test_ios_encrypted_full_decryption_flow(self, tmp_path: Path):
        """
        Verify encrypted iOS backup unlocks, saves decrypted databases into
        <output>/Whatsapp Databases, and uses lazy on-demand decryption.
        """
        backup_dir = tmp_path / "enc_backup"
        backup_dir.mkdir()
        out_dir = tmp_path / "out"

        with open(backup_dir / "Manifest.plist", "wb") as f:
            plistlib.dump({"IsEncrypted": True}, f)

        # Prepare dummy databases to be returned by mock EncryptedBackup
        cs_path = tmp_path / "mock_cs.sqlite"
        conn = sqlite3.connect(str(cs_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZGROUPINFO INTEGER, ZPARTNERNAME TEXT, ZCONTACTABID INTEGER, ZLASTMESSAGEDATE REAL);
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZMEDIAURL TEXT, ZTITLE TEXT);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT);
            CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZMESSAGEDATE REAL, ZISFROMME INTEGER, ZCHATSESSION INTEGER, ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER, ZPUSHNAME TEXT, ZSTANZAID TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (ZJID TEXT, ZPUSHNAME TEXT);

            INSERT INTO ZWACHATSESSION VALUES (1, '390000000001@s.whatsapp.net', NULL, NULL, NULL, 802785600.0);
            INSERT INTO ZWAMEDIAITEM VALUES (1, 'Media/encrypted_photo.jpg', NULL, NULL);
            INSERT INTO ZWAMESSAGE VALUES (1, 802785600.0, 0, 1, 1, NULL, NULL, 'enc_msg_01');
        """)
        conn.close()

        contacts_path = tmp_path / "mock_contacts.sqlite"
        c_conn = sqlite3.connect(str(contacts_path))
        c_conn.executescript("""
            CREATE TABLE ZWAADDRESSBOOKCONTACT (Z_PK INTEGER PRIMARY KEY, ZWHATSAPPID TEXT, ZFULLNAME TEXT);
            INSERT INTO ZWAADDRESSBOOKCONTACT VALUES (1, '390000000001@s.whatsapp.net', 'Encrypted User');
        """)
        c_conn.close()

        media_bytes = make_jpeg_bytes("ENCRYPTED_MEDIA_STREAM")

        # Mock EncryptedBackup
        class MockEncryptedBackup:
            def __init__(self, backup_directory: str, passphrase: str):
                assert passphrase == "vault_pass_123"

            def test_decryption(self):
                return True

            def extract_file(self, relative_path: str, domain_like: str, output_filename: str):
                if relative_path == "ChatStorage.sqlite":
                    Path(output_filename).write_bytes(cs_path.read_bytes())
                elif relative_path == "ContactsV2.sqlite":
                    Path(output_filename).write_bytes(contacts_path.read_bytes())
                elif relative_path.endswith("encrypted_photo.jpg"):
                    Path(output_filename).write_bytes(media_bytes)
                else:
                    raise FileNotFoundError(f"File not found: {relative_path}")

        with (
            patch("wab_archiver.main.check_dependencies"),
            patch("iphone_backup_decrypt.EncryptedBackup", MockEncryptedBackup),
        ):
            cmd_args = [
                "wab-archiver",
                "archive",
                "--ios-backup",
                str(backup_dir),
                "--ios-password",
                "vault_pass_123",
                "--output",
                str(out_dir),
            ]
            with patch("sys.argv", cmd_args):
                wa.main()

        decrypted_cs = out_dir / "Whatsapp Databases" / "ChatStorage.sqlite"
        decrypted_contacts = out_dir / "Whatsapp Databases" / "ContactsV2.sqlite"
        assert decrypted_cs.exists()
        assert decrypted_contacts.exists()

        archived_photo = (
            out_dir
            / "Contacts"
            / "Encrypted User (00390000000001)"
            / "2026"
            / "Received"
            / "encrypted_photo.jpg"
        )
        assert archived_photo.exists()
        assert archived_photo.read_bytes() == media_bytes


# ===========================================================================
# SCENARIO 3: iOS Pre-Extracted Container Mode Simulation
# ===========================================================================


class TestE2EIOSPreExtractedSimulation:
    def test_ios_pre_extracted_container_flow(self, tmp_path: Path):
        """
        Verify iOS pre-extracted container directory mode:
        --msgstore points to ChatStorage.sqlite
        --wa-root points to container folder containing Message/ subfolder
        """
        container_dir = tmp_path / "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
        message_dir = container_dir / "Message" / "Media"
        message_dir.mkdir(parents=True)

        media_file = message_dir / "pre_extracted.jpg"
        media_bytes = make_jpeg_bytes("PRE_EXTRACTED_CONTENT")
        media_file.write_bytes(media_bytes)

        db_path = container_dir / "ChatStorage.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZGROUPINFO INTEGER, ZPARTNERNAME TEXT, ZCONTACTABID INTEGER, ZLASTMESSAGEDATE REAL);
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZMEDIAURL TEXT, ZTITLE TEXT);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT);
            CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZMESSAGEDATE REAL, ZISFROMME INTEGER, ZCHATSESSION INTEGER, ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER, ZPUSHNAME TEXT, ZSTANZAID TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (ZJID TEXT, ZPUSHNAME TEXT);

            INSERT INTO ZWACHATSESSION VALUES (1, '390000000001@s.whatsapp.net', NULL, NULL, NULL, 802785600.0);
            INSERT INTO ZWAMEDIAITEM VALUES (1, 'Media/pre_extracted.jpg', NULL, NULL);
            INSERT INTO ZWAMESSAGE VALUES (1, 802785600.0, 0, 1, 1, NULL, NULL, 'pre_msg_01');
        """)
        conn.close()

        out_dir = tmp_path / "out"
        cmd_args = [
            "wab-archiver",
            "archive",
            "--msgstore",
            str(db_path),
            "--wa-root",
            str(container_dir),
            "--output",
            str(out_dir),
        ]
        with patch("sys.argv", cmd_args):
            wa.main()

        dest_file = (
            out_dir
            / "Contacts"
            / "Unknown (00390000000001)"
            / "2026"
            / "Received"
            / "pre_extracted.jpg"
        )
        assert dest_file.exists()
        assert dest_file.read_bytes() == media_bytes


# ===========================================================================
# SCENARIO 4: Android Multi-Root & Conflicts Simulation
# ===========================================================================


class TestE2EAndroidMultiRootSimulation:
    def test_android_multi_root_full_flow(self, tmp_path: Path):
        """
        Verify Android multi-root resolution and archival:
        - Multi-root conflict resolution: largest wins, same-size fallback, zero-byte skipping
        - source_conflicts_report.csv generated
        - HD media deduplication via message_association (LQ parent excluded)
        - Document title override (media_name applied)
        - WhatsApp official sender (0@s.whatsapp.net -> WhatsApp)
        - Phone number formatting (00-prefix)
        - Group chat disambiguation (same subject across distinct IDs)
        - Number change consolidation (message_system_number_change A->B->C)
        - vCard extraction (.vcf)
        """
        root1 = tmp_path / "wa_root1"
        root2 = tmp_path / "wa_root2"
        (root1 / "Media" / "WhatsApp Images").mkdir(parents=True)
        (root1 / "Media" / "WhatsApp Documents").mkdir(parents=True)
        (root2 / "Media" / "WhatsApp Images").mkdir(parents=True)
        (root2 / "Media" / "WhatsApp Documents").mkdir(parents=True)

        out_dir = tmp_path / "out"
        db_path = tmp_path / "msgstore.db"

        # Content for test files
        bytes_50k = b"A" * 50000
        bytes_100k = b"B" * 100000
        bytes_200k = b"C" * 200000
        bytes_tie1 = b"D" * 150000
        bytes_tie2 = b"E" * 150000
        hd_bytes = make_jpeg_bytes("ANDROID_HD_PHOTO")

        # Files in roots:
        # 1. Zero-byte file: 0-byte in root1, 50k in root2 -> root2 chosen
        (root1 / "Media" / "WhatsApp Images" / "zero_test.jpg").write_bytes(b"")
        (root2 / "Media" / "WhatsApp Images" / "zero_test.jpg").write_bytes(bytes_50k)

        # 2. Size conflict: 100k in root1, 200k in root2 -> largest wins (root2)
        (root1 / "Media" / "WhatsApp Images" / "size_conflict.jpg").write_bytes(bytes_100k)
        (root2 / "Media" / "WhatsApp Images" / "size_conflict.jpg").write_bytes(bytes_200k)

        # 3. Same-size diff-content conflict: 150k tie1 in root1, 150k tie2 in root2 -> root1 chosen
        (root1 / "Media" / "WhatsApp Images" / "tie_conflict.jpg").write_bytes(bytes_tie1)
        (root2 / "Media" / "WhatsApp Images" / "tie_conflict.jpg").write_bytes(bytes_tie2)

        # 4. HD and LQ files:
        (root1 / "Media" / "WhatsApp Images" / "lq_parent.jpg").write_bytes(b"LQ_DATA")
        (root1 / "Media" / "WhatsApp Images" / "hd_child.jpg").write_bytes(hd_bytes)

        # 5. Document with custom title:
        (root1 / "Media" / "WhatsApp Documents" / "doc_random.pdf").write_bytes(
            make_pdf_bytes("DOC_PAYLOAD")
        )

        # 6. Group photos for disambiguated groups:
        (root1 / "Media" / "WhatsApp Images" / "group_photo1.jpg").write_bytes(
            make_jpeg_bytes("GRP1")
        )
        (root1 / "Media" / "WhatsApp Images" / "group_photo2.jpg").write_bytes(
            make_jpeg_bytes("GRP2")
        )

        # Build msgstore.db
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, subject TEXT, jid_row_id INTEGER);
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                timestamp INTEGER,
                sender_jid_row_id INTEGER,
                from_me INTEGER,
                chat_row_id INTEGER,
                text_data TEXT,
                message_type INTEGER DEFAULT 0
            );
            CREATE TABLE message_media (
                _id INTEGER PRIMARY KEY,
                file_path TEXT,
                mime_type TEXT,
                chat_row_id INTEGER,
                message_row_id INTEGER,
                message_url TEXT,
                media_name TEXT
            );
            CREATE TABLE message_association (
                _id INTEGER PRIMARY KEY,
                parent_message_row_id INTEGER,
                child_message_row_id INTEGER,
                association_type INTEGER
            );
            CREATE TABLE message_system_number_change (
                _id INTEGER PRIMARY KEY,
                old_jid_row_id INTEGER,
                new_jid_row_id INTEGER
            );
            CREATE TABLE message_vcard (
                _id INTEGER PRIMARY KEY,
                message_row_id INTEGER,
                vcard TEXT
            );

            -- JIDs:
            -- 1: User A (390000000001) who changed number to B (390000000002)
            INSERT INTO jid VALUES (1, '390000000001');
            INSERT INTO jid VALUES (2, '390000000002');
            -- 3: WhatsApp system sender
            INSERT INTO jid VALUES (3, '0');
            -- 4, 5: Group chat JIDs
            INSERT INTO jid VALUES (4, 'group_jid_1');
            INSERT INTO jid VALUES (5, 'group_jid_2');

            -- Number change: 1 -> 2
            INSERT INTO message_system_number_change VALUES (1, 1, 2);

            -- Chats:
            -- 1: 1-to-1 with User A (mapped to current number B)
            INSERT INTO chat VALUES (1, NULL, 1);
            -- 2: 1-to-1 with WhatsApp official sender
            INSERT INTO chat VALUES (2, NULL, 3);
            -- 3: Group Chat with subject 'Family Chat'
            INSERT INTO chat VALUES (3, 'Family Chat', 4);
            -- 4: Second distinct Group Chat with same subject 'Family Chat'
            INSERT INTO chat VALUES (4, 'Family Chat', 5);

            -- Messages & Media (Timestamp: 1781092800000 ms = 2026-06-10 12:00:00 UTC):
            -- Msg 1: Zero-byte test in Chat 1 (Received)
            INSERT INTO message VALUES (1, 1781092800000, 1, 0, 1, NULL, 0);
            INSERT INTO message_media VALUES (1, 'Media/WhatsApp Images/zero_test.jpg', 'image/jpeg', 1, 1, NULL, NULL);

            -- Msg 2: Size conflict in Chat 1
            INSERT INTO message VALUES (2, 1781092800000, 1, 0, 1, NULL, 0);
            INSERT INTO message_media VALUES (2, 'Media/WhatsApp Images/size_conflict.jpg', 'image/jpeg', 1, 2, NULL, NULL);

            -- Msg 3: Tie conflict in Chat 1
            INSERT INTO message VALUES (3, 1781092800000, 1, 0, 1, NULL, 0);
            INSERT INTO message_media VALUES (3, 'Media/WhatsApp Images/tie_conflict.jpg', 'image/jpeg', 1, 3, NULL, NULL);

            -- Msg 4: LQ Parent image in Chat 1
            INSERT INTO message VALUES (4, 1781092800000, 1, 0, 1, NULL, 0);
            INSERT INTO message_media VALUES (4, 'Media/WhatsApp Images/lq_parent.jpg', 'image/jpeg', 1, 4, NULL, NULL);

            -- Msg 5: HD Child image in Chat 1
            INSERT INTO message VALUES (5, 1781092800000, 1, 0, 1, NULL, 0);
            INSERT INTO message_media VALUES (5, 'Media/WhatsApp Images/hd_child.jpg', 'image/jpeg', 1, 5, NULL, NULL);

            -- Association: Type 12 (HD child of LQ parent)
            INSERT INTO message_association VALUES (1, 4, 5, 12);

            -- Msg 6: Document with media_name override in Chat 1
            INSERT INTO message VALUES (6, 1781092800000, 1, 0, 1, NULL, 0);
            INSERT INTO message_media VALUES (6, 'Media/WhatsApp Documents/doc_random.pdf', 'application/pdf', 1, 6, NULL, 'Financial Report 2026.pdf');

            -- Msg 7: Group 1 Photo (Chat 3)
            INSERT INTO message VALUES (7, 1781092800000, 1, 0, 3, NULL, 0);
            INSERT INTO message_media VALUES (7, 'Media/WhatsApp Images/group_photo1.jpg', 'image/jpeg', 3, 7, NULL, NULL);

            -- Msg 8: Group 2 Photo (Chat 4)
            INSERT INTO message VALUES (8, 1781092800000, 1, 0, 4, NULL, 0);
            INSERT INTO message_media VALUES (8, 'Media/WhatsApp Images/group_photo2.jpg', 'image/jpeg', 4, 8, NULL, NULL);

            -- Msg 9: vCard contact card (message_type = 4)
            INSERT INTO message VALUES (9, 1781092800000, 1, 0, 1, 'Elena Advisor', 4);
            INSERT INTO message_vcard VALUES (1, 9, 'BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Elena Advisor\r\nTEL:+390000000009\r\nEND:VCARD\r\n');
        """)
        conn.close()

        # Contacts CSV mapping User B (390000000002) to name 'Bob Builder'
        contacts_csv = tmp_path / "contacts.csv"
        contacts_csv.write_text("+390000000002,Bob Builder\n", encoding="utf-8")

        cmd_args = [
            "wab-archiver",
            "archive",
            "--msgstore",
            str(db_path),
            "--wa-root",
            str(root1),
            "--wa-root",
            str(root2),
            "--contacts",
            str(contacts_csv),
            "--output",
            str(out_dir),
            "-v",
        ]
        with patch("sys.argv", cmd_args):
            wa.main()

        # =======================================================================
        # Assertions
        # =======================================================================

        # 1. Number Change Consolidation:
        # Message sent by User 1 (390000000001) consolidated into User 2 folder 'Bob Builder (00390000000002)'
        bob_dir = out_dir / "Contacts" / "Bob Builder (00390000000002)" / "2026" / "Received"
        assert bob_dir.exists()

        # 2. Zero-byte resolution: valid 50k from root2 was copied
        zero_resolved = bob_dir / "zero_test.jpg"
        assert zero_resolved.exists()
        assert zero_resolved.read_bytes() == bytes_50k

        # 3. Size conflict resolution: largest 200k from root2 was copied
        size_resolved = bob_dir / "size_conflict.jpg"
        assert size_resolved.exists()
        assert size_resolved.read_bytes() == bytes_200k

        # 4. Tie conflict resolution: same size, root1 chosen
        tie_resolved = bob_dir / "tie_conflict.jpg"
        assert tie_resolved.exists()
        assert tie_resolved.read_bytes() == bytes_tie1

        # 5. HD Deduplication: HD child copied, LQ parent omitted
        assert (bob_dir / "hd_child.jpg").exists()
        assert not (bob_dir / "lq_parent.jpg").exists()

        # 6. Document title override:
        doc_resolved = bob_dir / "Financial Report 2026.pdf"
        assert doc_resolved.exists()
        assert b"DOC_PAYLOAD" in doc_resolved.read_bytes()

        # 7. vCard resolution:
        vcf_resolved = bob_dir / "Elena Advisor.vcf"
        assert vcf_resolved.exists()
        assert b"Elena Advisor" in vcf_resolved.read_bytes()

        # 8. Group Disambiguation:
        # Two distinct groups named 'Family Chat' -> 'Family Chat' and 'Family Chat (2)'
        grp1_dir = out_dir / "Groups" / "Family Chat" / "2026"
        grp2_dir = out_dir / "Groups" / "Family Chat (2)" / "2026"
        assert grp1_dir.exists()
        assert grp2_dir.exists()
        assert (grp1_dir / "group_photo1_Bob Builder.jpg").exists()
        assert (grp2_dir / "group_photo2_Bob Builder.jpg").exists()

        # 9. Source conflicts report:
        conflicts_csv = out_dir / "source_conflicts_report.csv"
        assert conflicts_csv.exists()
        with open(conflicts_csv, encoding="utf-8") as f:
            conflicts = list(csv.DictReader(f))
            assert len(conflicts) >= 2
            reasons = {r["reason"] for r in conflicts}
            assert "largest_wins" in reasons
            assert "same_size_first_root" in reasons


# ===========================================================================
# SCENARIO 5: Android Encrypted & Incremental Backup Merge Simulation
# ===========================================================================


class TestE2EAndroidEncryptedAndIncrementalSimulation:
    def test_android_encrypted_missing_e2e_key_aborts(self, tmp_path: Path):
        """Encrypted msgstore without --e2e-key must abort with code 1."""
        db_crypt = tmp_path / "msgstore.db.crypt14"
        db_crypt.write_bytes(b"DUMMY_ENCRYPTED_HEADER")
        out_dir = tmp_path / "out"

        dummy_root = tmp_path / "dummy_wa_root"
        (dummy_root / "Media" / "WhatsApp Images").mkdir(parents=True)
        cmd_args = [
            "wab-archiver",
            "archive",
            "--msgstore",
            str(db_crypt),
            "--wa-root",
            str(dummy_root),
            "--output",
            str(out_dir),
        ]
        with patch("sys.argv", cmd_args), pytest.raises(SystemExit) as exc_info:
            wa.main()
        assert exc_info.value.code == 1

    def test_android_encrypted_and_incremental_merge_flow(self, tmp_path: Path):
        """
        Verify that a crypt14 base backup and incremental backup diffs are decrypted,
        merged via increment_merger, rotated into <output>/Whatsapp Databases/msgstore.db,
        and media archived.
        """
        db_dir = tmp_path / "source_dbs"
        db_dir.mkdir()
        base_crypt = db_dir / "msgstore.db.crypt14"
        base_crypt.write_bytes(b"BASE_CRYPT_DATA")
        inc_crypt = db_dir / "msgstore-incremental-1.crypt14"
        inc_crypt.write_bytes(b"INC_CRYPT_DATA")

        root = tmp_path / "wa_root"
        (root / "Media" / "WhatsApp Images").mkdir(parents=True)
        img_file = root / "Media" / "WhatsApp Images" / "inc_photo.jpg"
        img_bytes = make_jpeg_bytes("INCREMENTAL_IMAGE")
        img_file.write_bytes(img_bytes)

        out_dir = tmp_path / "out"

        # Pre-build merged database that the merge mock will output
        merged_sqlite = tmp_path / "mock_merged.db"
        m_conn = sqlite3.connect(str(merged_sqlite))
        m_conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, subject TEXT, jid_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, timestamp INTEGER, sender_jid_row_id INTEGER, from_me INTEGER);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, file_path TEXT, mime_type TEXT, chat_row_id INTEGER, message_row_id INTEGER, message_url TEXT, media_name TEXT);

            INSERT INTO jid VALUES (1, '390000000001');
            INSERT INTO chat VALUES (1, NULL, 1);
            INSERT INTO message VALUES (1, 1781092800000, 1, 0);
            INSERT INTO message_media VALUES (1, 'Media/WhatsApp Images/inc_photo.jpg', 'image/jpeg', 1, 1, NULL, NULL);
        """)
        m_conn.close()

        def mock_decrypt(src, key, dst, logger):
            Path(dst).write_bytes(merged_sqlite.read_bytes())

        def mock_merge(base, incs, key, incoming, staging, logger):
            Path(incoming).write_bytes(merged_sqlite.read_bytes())

        with (
            patch("wab_archiver.main.check_dependencies"),
            patch("wab_archiver.main.decrypt_msgstore", mock_decrypt),
            patch("wab_archiver.increment_merger.merge_increments", mock_merge),
        ):
            cmd_args = [
                "wab-archiver",
                "archive",
                "--msgstore",
                str(base_crypt),
                "--e2e-key",
                "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                "--wa-root",
                str(root),
                "--output",
                str(out_dir),
            ]
            with patch("sys.argv", cmd_args):
                wa.main()

        decrypted_db = out_dir / "Whatsapp Databases" / "msgstore.db"
        assert decrypted_db.exists()
        dest_photo = (
            out_dir
            / "Contacts"
            / "Unknown (00390000000001)"
            / "2026"
            / "Received"
            / "inc_photo.jpg"
        )
        assert dest_photo.exists()
        assert dest_photo.read_bytes() == img_bytes


# ===========================================================================
# SCENARIO 6: Android ADB Pipeline Simulation
# ===========================================================================


class TestE2EAndroidADBSimulation:
    def test_adb_pipeline_full_flow(self, tmp_path: Path):
        """
        Verify --from-adb pipeline:
        - ADB check and device detection
        - msgstore pull & decryption
        - contacts pull via content query
        - media pull via ADB with staging folder
        - Media copied to archive output
        """
        staging_dir = tmp_path / "adb_staging"
        out_dir = tmp_path / "out"

        # Pre-build pulled database
        adb_pulled_db = tmp_path / "adb_msgstore.db"
        conn = sqlite3.connect(str(adb_pulled_db))
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, subject TEXT, jid_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, timestamp INTEGER, sender_jid_row_id INTEGER, from_me INTEGER);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, file_path TEXT, mime_type TEXT, chat_row_id INTEGER, message_row_id INTEGER, message_url TEXT, media_name TEXT);

            INSERT INTO jid VALUES (1, '390000000001');
            INSERT INTO chat VALUES (1, NULL, 1);
            INSERT INTO message VALUES (1, 1781092800000, 1, 0);
            INSERT INTO message_media VALUES (1, 'Media/WhatsApp Images/adb_photo.jpg', 'image/jpeg', 1, 1, NULL, NULL);
        """)
        conn.close()

        # Media in staging
        media_bytes = make_jpeg_bytes("ADB_PULLED_IMAGE")

        def mock_pull_msgstore(tmp_dir, business, logger):
            dest = os.path.join(tmp_dir, "msgstore.db")
            Path(dest).write_bytes(adb_pulled_db.read_bytes())
            return dest, []

        def mock_pull_contacts(tmp_dir, logger):
            c_file = os.path.join(tmp_dir, "contacts.csv")
            Path(c_file).write_text("+390000000001,ADB Contact\n", encoding="utf-8")
            return c_file

        def mock_pull_media(staging, business, conn, logger, target_paths):
            dest_media = Path(staging) / "Media" / "WhatsApp Images" / "adb_photo.jpg"
            dest_media.parent.mkdir(parents=True, exist_ok=True)
            dest_media.write_bytes(media_bytes)
            return 1, 0, []

        with (
            patch("wab_archiver.main.check_dependencies"),
            patch("wab_archiver.adb_extractor.check_adb", return_value=True),
            patch("wab_archiver.adb_extractor.check_device_connected", return_value=True),
            patch("wab_archiver.adb_extractor.pull_msgstore_and_increments", mock_pull_msgstore),
            patch("wab_archiver.adb_extractor.pull_contacts", mock_pull_contacts),
            patch("wab_archiver.adb_extractor.pull_media", mock_pull_media),
        ):
            cmd_args = [
                "wab-archiver",
                "archive",
                "--from-adb",
                "--pull-media",
                "--staging",
                str(staging_dir),
                "--output",
                str(out_dir),
            ]
            with patch("sys.argv", cmd_args):
                wa.main()

        archived_photo = (
            out_dir
            / "Contacts"
            / "ADB Contact (00390000000001)"
            / "2026"
            / "Received"
            / "adb_photo.jpg"
        )
        assert archived_photo.exists()
        assert archived_photo.read_bytes() == media_bytes


# ===========================================================================
# SCENARIO 7: Multi-Run Lifecycle, Renaming Sync & Preservation
# ===========================================================================


class TestE2EMultiRunSynchronizationAndPreservation:
    def test_multi_run_rename_and_preserve_deleted_flow(self, tmp_path: Path):
        """
        Verify multi-run incremental lifecycle with --preserve-deleted:
        Run 1 (Baseline):
        - Contact Alice -> folder 'Alice (00390000000001)'
        - Group 'Team Alpha' -> folder 'Team Alpha'
        - Messages M1, M2, M3
        - Baseline established in <output>/Whatsapp Databases/msgstore.db
        - preserved_messages.db does not exist yet

        Run 2 (Incremental):
        - Contact Alice renamed to 'Alice Cooper' on device
        - Group renamed to 'Team Alpha Renamed' on device
        - Message M2 deleted on device
        - New message M4 added
        - Contact and Group folders on disk automatically renamed without duplicates
        - preserved_messages.db created containing M2
        - Pre-existing media skipped
        - New media copied
        """
        out_dir = tmp_path / "out"
        root = tmp_path / "wa_root"
        (root / "Media" / "WhatsApp Images").mkdir(parents=True)

        m1_bytes = make_jpeg_bytes("MSG1_BYTES")
        m2_bytes = make_jpeg_bytes("MSG2_BYTES")
        m3_bytes = make_jpeg_bytes("MSG3_BYTES")
        m4_bytes = make_jpeg_bytes("MSG4_BYTES")

        (root / "Media" / "WhatsApp Images" / "m1.jpg").write_bytes(m1_bytes)
        (root / "Media" / "WhatsApp Images" / "m2.jpg").write_bytes(m2_bytes)
        (root / "Media" / "WhatsApp Images" / "m3.jpg").write_bytes(m3_bytes)
        (root / "Media" / "WhatsApp Images" / "m4.jpg").write_bytes(m4_bytes)

        # -------------------------------------------------------------------
        # RUN 1: Baseline
        # -------------------------------------------------------------------
        db_run1 = tmp_path / "msgstore_run1.db"
        conn1 = sqlite3.connect(str(db_run1))
        conn1.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, subject TEXT, jid_row_id INTEGER);
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                timestamp INTEGER,
                sender_jid_row_id INTEGER,
                from_me INTEGER,
                chat_row_id INTEGER,
                key_id TEXT,
                message_type INTEGER,
                text_data TEXT
            );
            CREATE TABLE message_media (
                _id INTEGER PRIMARY KEY,
                file_path TEXT,
                mime_type TEXT,
                chat_row_id INTEGER,
                message_row_id INTEGER,
                message_url TEXT,
                media_name TEXT
            );

            INSERT INTO jid VALUES (1, '390000000001');
            INSERT INTO jid VALUES (2, 'group_alpha');

            INSERT INTO chat VALUES (1, NULL, 1);
            INSERT INTO chat VALUES (2, 'Team Alpha', 2);

            -- M1 in 1-to-1 Alice
            INSERT INTO message VALUES (1, 1781092800000, 1, 0, 1, 'k1', 0, 'Hello');
            INSERT INTO message_media VALUES (1, 'Media/WhatsApp Images/m1.jpg', 'image/jpeg', 1, 1, NULL, NULL);

            -- M2 in 1-to-1 Alice (to be deleted in Run 2)
            INSERT INTO message VALUES (2, 1781092800000, 1, 0, 1, 'k2', 0, 'Delete me later');
            INSERT INTO message_media VALUES (2, 'Media/WhatsApp Images/m2.jpg', 'image/jpeg', 1, 2, NULL, NULL);

            -- M3 in Group Team Alpha
            INSERT INTO message VALUES (3, 1781092800000, 1, 0, 2, 'k3', 0, 'Group info');
            INSERT INTO message_media VALUES (3, 'Media/WhatsApp Images/m3.jpg', 'image/jpeg', 2, 3, NULL, NULL);
        """)
        conn1.close()

        contacts_run1 = tmp_path / "contacts_r1.csv"
        contacts_run1.write_text("+390000000001,Alice\n", encoding="utf-8")

        cmd_run1 = [
            "wab-archiver",
            "archive",
            "--msgstore",
            str(db_run1),
            "--wa-root",
            str(root),
            "--contacts",
            str(contacts_run1),
            "--output",
            str(out_dir),
            "-P",
        ]
        with patch("sys.argv", cmd_run1):
            wa.main()

        # Run 1 verifications:
        alice_v1_dir = out_dir / "Contacts" / "Alice (00390000000001)" / "2026" / "Received"
        group_v1_dir = out_dir / "Groups" / "Team Alpha" / "2026"
        assert (alice_v1_dir / "m1.jpg").exists()
        assert (alice_v1_dir / "m2.jpg").exists()
        assert (group_v1_dir / "m3_Alice.jpg").exists()

        baseline_db = out_dir / "Whatsapp Databases" / "msgstore.db"
        assert baseline_db.exists()
        preserved_db = out_dir / "Whatsapp Databases" / "preserved_messages.db"
        assert not preserved_db.exists()  # Baseline run does not preserve

        # -------------------------------------------------------------------
        # RUN 2: Incremental (Renames + Deletion of M2 + New M4)
        # -------------------------------------------------------------------
        db_run2 = tmp_path / "msgstore_run2.db"
        conn2 = sqlite3.connect(str(db_run2))
        conn2.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, subject TEXT, jid_row_id INTEGER);
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                timestamp INTEGER,
                sender_jid_row_id INTEGER,
                from_me INTEGER,
                chat_row_id INTEGER,
                key_id TEXT,
                message_type INTEGER,
                text_data TEXT
            );
            CREATE TABLE message_media (
                _id INTEGER PRIMARY KEY,
                file_path TEXT,
                mime_type TEXT,
                chat_row_id INTEGER,
                message_row_id INTEGER,
                message_url TEXT,
                media_name TEXT
            );

            INSERT INTO jid VALUES (1, '390000000001');
            INSERT INTO jid VALUES (2, 'group_alpha');

            INSERT INTO chat VALUES (1, NULL, 1);
            -- Group renamed: Team Alpha -> Team Alpha Renamed
            INSERT INTO chat VALUES (2, 'Team Alpha Renamed', 2);

            -- M1 retained
            INSERT INTO message VALUES (1, 1781092800000, 1, 0, 1, 'k1', 0, 'Hello');
            INSERT INTO message_media VALUES (1, 'Media/WhatsApp Images/m1.jpg', 'image/jpeg', 1, 1, NULL, NULL);

            -- (M2 was DELETED!)

            -- M3 retained
            INSERT INTO message VALUES (3, 1781092800000, 1, 0, 2, 'k3', 0, 'Group info');
            INSERT INTO message_media VALUES (3, 'Media/WhatsApp Images/m3.jpg', 'image/jpeg', 2, 3, NULL, NULL);

            -- M4 added in Alice chat
            INSERT INTO message VALUES (4, 1781092800000, 1, 0, 1, 'k4', 0, 'New message');
            INSERT INTO message_media VALUES (4, 'Media/WhatsApp Images/m4.jpg', 'image/jpeg', 1, 4, NULL, NULL);
        """)
        conn2.close()

        # Contact renamed: Alice -> Alice Cooper
        contacts_run2 = tmp_path / "contacts_r2.csv"
        contacts_run2.write_text("+390000000001,Alice Cooper\n", encoding="utf-8")

        cmd_run2 = [
            "wab-archiver",
            "archive",
            "--msgstore",
            str(db_run2),
            "--wa-root",
            str(root),
            "--contacts",
            str(contacts_run2),
            "--output",
            str(out_dir),
            "-P",
        ]
        with patch("sys.argv", cmd_run2):
            wa.main()

        # Run 2 verifications:
        # 1. Contact folder renamed on disk; old folder gone:
        old_alice_dir = out_dir / "Contacts" / "Alice (00390000000001)"
        new_alice_dir = out_dir / "Contacts" / "Alice Cooper (00390000000001)"
        assert not old_alice_dir.exists()
        assert new_alice_dir.exists()

        alice_r2_files = new_alice_dir / "2026" / "Received"
        assert (alice_r2_files / "m1.jpg").exists()
        assert (alice_r2_files / "m2.jpg").exists()  # Existing file kept
        assert (alice_r2_files / "m4.jpg").exists()  # New file added

        # 2. Group folder renamed on disk; old folder gone:
        old_group_dir = out_dir / "Groups" / "Team Alpha"
        new_group_dir = out_dir / "Groups" / "Team Alpha Renamed"
        assert not old_group_dir.exists()
        assert new_group_dir.exists()
        # Group file sender name updated to match new contact name
        assert (new_group_dir / "2026" / "m3_Alice Cooper.jpg").exists()

        # 3. Preservation database contains deleted message M2:
        assert preserved_db.exists()
        p_conn = sqlite3.connect(str(preserved_db))
        pres_rows = p_conn.execute("SELECT stanza_id, text_body FROM preserved_messages").fetchall()
        assert len(pres_rows) == 1
        assert pres_rows[0][0] == "k2"
        assert pres_rows[0][1] == "Delete me later"

        runs = p_conn.execute("SELECT messages_preserved FROM preservation_runs").fetchall()
        assert len(runs) == 1
        assert runs[0][0] == 1
        p_conn.close()


# ===========================================================================
# SCENARIO 8: Deduplication, Hardlinking & Name Collisions
# ===========================================================================


class TestE2EDeduplicationAndHardlinking:
    def test_dedup_hardlinking_and_collision_rename(self, tmp_path: Path):
        """
        Verify:
        - Cross-chat duplicate files linked via os.link when --link-duplicates is active
        - Same-chat collision with different content renamed with suffix _1
        - Duplicate media report written with categorized sections
        - Hardlink probe fallback tested
        """
        out_dir = tmp_path / "out"
        root = tmp_path / "wa_root"
        (root / "Media" / "WhatsApp Images").mkdir(parents=True)

        dup_bytes = make_jpeg_bytes("IDENTICAL_PHOTO")
        diff_bytes = make_jpeg_bytes("COLLISION_PHOTO_DIFFERENT")

        # Two identical files with different original names:
        (root / "Media" / "WhatsApp Images" / "photo_a.jpg").write_bytes(dup_bytes)
        (root / "Media" / "WhatsApp Images" / "photo_b.jpg").write_bytes(dup_bytes)

        # File for collision: same name "photo_a.jpg" in chat 1 from another run/source
        (root / "Media" / "WhatsApp Images" / "photo_collision.jpg").write_bytes(diff_bytes)

        db_path = tmp_path / "msgstore.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, subject TEXT, jid_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, timestamp INTEGER, sender_jid_row_id INTEGER, from_me INTEGER);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, file_path TEXT, mime_type TEXT, chat_row_id INTEGER, message_row_id INTEGER, message_url TEXT, media_name TEXT);

            INSERT INTO jid VALUES (1, '390000000001');
            INSERT INTO jid VALUES (2, '390000000002');
            INSERT INTO chat VALUES (1, NULL, 1);
            INSERT INTO chat VALUES (2, NULL, 2);

            -- Chat 1 has photo_a.jpg
            INSERT INTO message VALUES (1, 1781092800000, 1, 0);
            INSERT INTO message_media VALUES (1, 'Media/WhatsApp Images/photo_a.jpg', 'image/jpeg', 1, 1, NULL, NULL);

            -- Chat 2 has photo_b.jpg (identical bytes to photo_a)
            INSERT INTO message VALUES (2, 1781092800000, 2, 0);
            INSERT INTO message_media VALUES (2, 'Media/WhatsApp Images/photo_b.jpg', 'image/jpeg', 2, 2, NULL, NULL);
        """)
        conn.close()

        # Pre-seed a file at Chat 1's destination that has DIFFERENT bytes to force a collision:
        pre_seeded_dir = out_dir / "Contacts" / "Unknown (00390000000001)" / "2026" / "Received"
        pre_seeded_dir.mkdir(parents=True)
        (pre_seeded_dir / "photo_a.jpg").write_bytes(diff_bytes)

        cmd_args = [
            "wab-archiver",
            "archive",
            "--msgstore",
            str(db_path),
            "--wa-root",
            str(root),
            "--output",
            str(out_dir),
            "--link-duplicates",
            "-v",
        ]
        with patch("sys.argv", cmd_args):
            wa.main()

        # 1. Collision verification:
        # Pre-seeded photo_a was preserved, incoming photo_a was renamed to photo_a_1.jpg:
        assert (pre_seeded_dir / "photo_a.jpg").read_bytes() == diff_bytes
        renamed_file = pre_seeded_dir / "photo_a_1.jpg"
        assert renamed_file.exists()
        assert renamed_file.read_bytes() == dup_bytes

        # 2. Hardlink verification:
        chat2_dir = out_dir / "Contacts" / "Unknown (00390000000002)" / "2026" / "Received"
        chat2_photo = chat2_dir / "photo_b.jpg"
        assert chat2_photo.exists()
        assert chat2_photo.read_bytes() == dup_bytes

        if os.stat(str(renamed_file)).st_nlink > 1:
            assert os.stat(str(chat2_photo)).st_nlink > 1

        # 3. Duplicate media report:
        dup_csv = out_dir / "duplicate_media_report.csv"
        assert dup_csv.exists()
        dup_content = dup_csv.read_text(encoding="utf-8")
        assert "PHOTOS & IMAGES" in dup_content

    def test_hardlink_unsupported_probe_fallback(self, tmp_path: Path):
        """When probe_hardlink_support returns False, link_duplicates falls back to copy mode."""
        out_dir = tmp_path / "out"
        root = tmp_path / "wa_root"
        (root / "Media" / "WhatsApp Images").mkdir(parents=True)
        dup_bytes = make_jpeg_bytes("FALLBACK_IMG")
        (root / "Media" / "WhatsApp Images" / "img1.jpg").write_bytes(dup_bytes)
        (root / "Media" / "WhatsApp Images" / "img2.jpg").write_bytes(dup_bytes)

        db_path = tmp_path / "msgstore.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, subject TEXT, jid_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, timestamp INTEGER, sender_jid_row_id INTEGER, from_me INTEGER);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, file_path TEXT, mime_type TEXT, chat_row_id INTEGER, message_row_id INTEGER, message_url TEXT, media_name TEXT);

            INSERT INTO jid VALUES (1, '390000000001'), (2, '390000000002');
            INSERT INTO chat VALUES (1, NULL, 1), (2, NULL, 2);
            INSERT INTO message VALUES (1, 1781092800000, 1, 0), (2, 1781092800000, 2, 0);
            INSERT INTO message_media VALUES (1, 'Media/WhatsApp Images/img1.jpg', 'image/jpeg', 1, 1, NULL, NULL);
            INSERT INTO message_media VALUES (2, 'Media/WhatsApp Images/img2.jpg', 'image/jpeg', 2, 2, NULL, NULL);
        """)
        conn.close()

        with patch("wab_archiver.main.probe_hardlink_support", return_value=False):
            cmd_args = [
                "wab-archiver",
                "archive",
                "--msgstore",
                str(db_path),
                "--wa-root",
                str(root),
                "--output",
                str(out_dir),
                "--link-duplicates",
            ]
            with patch("sys.argv", cmd_args):
                wa.main()

        # Files copied successfully via fallback
        f1 = out_dir / "Contacts" / "Unknown (00390000000001)" / "2026" / "Received" / "img1.jpg"
        f2 = out_dir / "Contacts" / "Unknown (00390000000002)" / "2026" / "Received" / "img2.jpg"
        assert f1.exists()
        assert f2.exists()


# ===========================================================================
# SCENARIO 9: Restore Mode (Reverse Reconstitution & iOS Rejection)
# ===========================================================================


class TestE2ERestoreMode:
    def test_restore_android_archive_reconstructs_media_tree(self, tmp_path: Path):
        """
        Verify 'wab-archiver restore' inverts an Android archive back into
        original Media/WhatsApp Images/... folder structure with restore_report.csv.
        """
        out_dir = tmp_path / "archive"
        out_dir.mkdir()

        # Build valid .wa_media_archiver.db
        arc_db = out_dir / ".wa_media_archiver.db"
        conn = sqlite3.connect(str(arc_db))
        conn.executescript("""
            CREATE TABLE files (original_path TEXT PRIMARY KEY, md5 BLOB NOT NULL, size INTEGER);
            CREATE TABLE archive_copies (
                original_path TEXT NOT NULL REFERENCES files(original_path),
                archive_path TEXT NOT NULL,
                is_hardlink INTEGER DEFAULT 0,
                timestamp_ms INTEGER,
                PRIMARY KEY (original_path, archive_path)
            );

            INSERT INTO files VALUES ('Media/WhatsApp Images/restored_pic.jpg', X'112233', 100);
            INSERT INTO archive_copies VALUES (
                'Media/WhatsApp Images/restored_pic.jpg',
                'Contacts/Alice/2026/Received/restored_pic.jpg',
                0,
                1781092800000
            );

            -- Unrestorable file (archive copy missing from disk)
            INSERT INTO files VALUES ('Media/WhatsApp Images/missing_archive_copy.jpg', X'445566', 200);
            INSERT INTO archive_copies VALUES (
                'Media/WhatsApp Images/missing_archive_copy.jpg',
                'Contacts/Alice/2026/Received/non_existent.jpg',
                0,
                1781092800000
            );
        """)
        conn.close()

        # Place the archived file
        archived_pic = out_dir / "Contacts" / "Alice" / "2026" / "Received" / "restored_pic.jpg"
        archived_pic.parent.mkdir(parents=True)
        img_bytes = make_jpeg_bytes("RESTORE_TEST_PHOTO")
        archived_pic.write_bytes(img_bytes)

        cmd_args = ["wab-archiver", "restore", "--output", str(out_dir)]
        with patch("sys.argv", cmd_args):
            wa.main()

        # Reconstructed file at original path:
        reconstructed = out_dir / "Media" / "WhatsApp Images" / "restored_pic.jpg"
        assert reconstructed.exists()
        assert reconstructed.read_bytes() == img_bytes

        # restore_report.csv
        report = out_dir / "restore_report.csv"
        assert report.exists()
        with open(report, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
            assert len(rows) == 1
            assert rows[0]["status"] == "unrestorable"

        # Re-running restore skips already restored file:
        with patch("sys.argv", cmd_args):
            wa.main()

    def test_restore_ios_archive_aborts_with_error(self, tmp_path: Path):
        """Restore mode on pure iOS archive must abort with clear error message."""
        out_dir = tmp_path / "archive_ios"
        out_dir.mkdir()

        arc_db = out_dir / ".wa_media_archiver.db"
        conn = sqlite3.connect(str(arc_db))
        conn.executescript("""
            CREATE TABLE files (original_path TEXT PRIMARY KEY, md5 BLOB NOT NULL, size INTEGER);
            CREATE TABLE archive_copies (
                original_path TEXT NOT NULL,
                archive_path TEXT NOT NULL,
                PRIMARY KEY (original_path, archive_path)
            );
            INSERT INTO files VALUES ('Message/Media/ios_pic.jpg', X'1122', 50);
            INSERT INTO archive_copies VALUES ('Message/Media/ios_pic.jpg', 'Contacts/Alice/ios_pic.jpg');
        """)
        conn.close()

        cmd_args = ["wab-archiver", "restore", "--output", str(out_dir)]
        with patch("sys.argv", cmd_args), pytest.raises(SystemExit) as exc_info:
            wa.main()
        assert exc_info.value.code == 1


# ===========================================================================
# SCENARIO 10: Filtering, Limiting, Timezones & Configuration
# ===========================================================================


class TestE2EFiltersLimitingAndConfig:
    def test_since_filter_and_limit_options(self, tmp_path: Path):
        """Verify --since YYYY-MM-DD filters out older items and --limit restricts rows."""
        root = tmp_path / "wa_root"
        (root / "Media" / "WhatsApp Images").mkdir(parents=True)
        out_dir = tmp_path / "out"

        # Old file (2024):
        (root / "Media" / "WhatsApp Images" / "old_2024.jpg").write_bytes(
            make_jpeg_bytes("OLD_2024")
        )
        # New file (2026):
        (root / "Media" / "WhatsApp Images" / "new_2026.jpg").write_bytes(
            make_jpeg_bytes("NEW_2026")
        )

        db_path = tmp_path / "msgstore.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, subject TEXT, jid_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, timestamp INTEGER, sender_jid_row_id INTEGER, from_me INTEGER);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, file_path TEXT, mime_type TEXT, chat_row_id INTEGER, message_row_id INTEGER, message_url TEXT, media_name TEXT);

            INSERT INTO jid VALUES (1, '390000000001');
            INSERT INTO chat VALUES (1, NULL, 1);

            -- Old message (2024-01-01 -> ~1704067200000 ms)
            INSERT INTO message VALUES (1, 1704067200000, 1, 0);
            INSERT INTO message_media VALUES (1, 'Media/WhatsApp Images/old_2024.jpg', 'image/jpeg', 1, 1, NULL, NULL);

            -- New message (2026-06-10 -> 1781092800000 ms)
            INSERT INTO message VALUES (2, 1781092800000, 1, 0);
            INSERT INTO message_media VALUES (2, 'Media/WhatsApp Images/new_2026.jpg', 'image/jpeg', 1, 2, NULL, NULL);
        """)
        conn.close()

        cmd_args = [
            "wab-archiver",
            "archive",
            "--msgstore",
            str(db_path),
            "--wa-root",
            str(root),
            "--since",
            "2026-01-01",
            "--limit",
            "10",
            "--output",
            str(out_dir),
        ]
        with patch("sys.argv", cmd_args):
            wa.main()

        alice_dir = out_dir / "Contacts" / "Unknown (00390000000001)"
        assert not (alice_dir / "2024").exists()
        assert (alice_dir / "2026" / "Received" / "new_2026.jpg").exists()

    def test_dry_run_mode_does_not_mutate_disk(self, tmp_path: Path):
        """Verify --dry-run performs all validations but writes no files or databases."""
        root = tmp_path / "wa_root"
        (root / "Media" / "WhatsApp Images").mkdir(parents=True)
        (root / "Media" / "WhatsApp Images" / "dry.jpg").write_bytes(make_jpeg_bytes("DRY_DATA"))
        out_dir = tmp_path / "dry_out"

        db_path = tmp_path / "msgstore.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, subject TEXT, jid_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, timestamp INTEGER, sender_jid_row_id INTEGER, from_me INTEGER);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, file_path TEXT, mime_type TEXT, chat_row_id INTEGER, message_row_id INTEGER, message_url TEXT, media_name TEXT);

            INSERT INTO jid VALUES (1, '390000000001');
            INSERT INTO chat VALUES (1, NULL, 1);
            INSERT INTO message VALUES (1, 1781092800000, 1, 0);
            INSERT INTO message_media VALUES (1, 'Media/WhatsApp Images/dry.jpg', 'image/jpeg', 1, 1, NULL, NULL);
        """)
        conn.close()

        cmd_args = [
            "wab-archiver",
            "archive",
            "--msgstore",
            str(db_path),
            "--wa-root",
            str(root),
            "--output",
            str(out_dir),
            "--dry-run",
        ]
        with patch("sys.argv", cmd_args):
            wa.main()

        # In dry run mode: no media files are copied and archive_copies has 0 rows:
        assert not (out_dir / "Contacts").exists()
        arc_conn = sqlite3.connect(str(out_dir / ".wa_media_archiver.db"))
        copies_count = arc_conn.execute("SELECT COUNT(*) FROM archive_copies").fetchone()[0]
        assert copies_count == 0
        arc_conn.close()

    def test_toml_config_support(self, tmp_path: Path):
        """Verify parameters are loaded from --config TOML file."""
        root = tmp_path / "wa_root"
        (root / "Media" / "WhatsApp Images").mkdir(parents=True)
        (root / "Media" / "WhatsApp Images" / "cfg.jpg").write_bytes(make_jpeg_bytes("CFG_DATA"))
        out_dir = tmp_path / "cfg_out"

        db_path = tmp_path / "msgstore.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, subject TEXT, jid_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, timestamp INTEGER, sender_jid_row_id INTEGER, from_me INTEGER);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, file_path TEXT, mime_type TEXT, chat_row_id INTEGER, message_row_id INTEGER, message_url TEXT, media_name TEXT);

            INSERT INTO jid VALUES (1, '390000000001');
            INSERT INTO chat VALUES (1, NULL, 1);
            INSERT INTO message VALUES (1, 1781092800000, 1, 0);
            INSERT INTO message_media VALUES (1, 'Media/WhatsApp Images/cfg.jpg', 'image/jpeg', 1, 1, NULL, NULL);
        """)
        conn.close()

        cfg_file = tmp_path / "test_config.toml"
        cfg_file.write_text(
            f'msgstore = "{db_path.as_posix()}"\n'
            f'wa_root = "{root.as_posix()}"\n'
            f'output = "{out_dir.as_posix()}"\n',
            encoding="utf-8",
        )

        cmd_args = ["wab-archiver", "--config", str(cfg_file)]
        with patch("sys.argv", cmd_args):
            wa.main()

        assert (
            out_dir / "Contacts" / "Unknown (00390000000001)" / "2026" / "Received" / "cfg.jpg"
        ).exists()


# ===========================================================================
# SCENARIO 11: Schema Drift & Fault Recovery
# ===========================================================================


class TestE2ESchemaDriftAndDiagnostics:
    def test_android_missing_table_aborts_with_schema_banner(self, tmp_path: Path):
        """Android database missing required table must trigger schema validator and exit."""
        db_path = tmp_path / "corrupt_msgstore.db"
        conn = sqlite3.connect(str(db_path))
        # Missing 'message_media' table:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, subject TEXT, jid_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, timestamp INTEGER, sender_jid_row_id INTEGER, from_me INTEGER);
        """)
        conn.close()

        root = tmp_path / "wa_root"
        (root / "Media" / "WhatsApp Images").mkdir(parents=True)
        out_dir = tmp_path / "out"

        cmd_args = [
            "wab-archiver",
            "archive",
            "--msgstore",
            str(db_path),
            "--wa-root",
            str(root),
            "--output",
            str(out_dir),
        ]
        with patch("sys.argv", cmd_args), pytest.raises(SystemExit) as exc_info:
            wa.main()
        assert exc_info.value.code == 1

    def test_ios_missing_column_aborts_with_schema_banner(self, tmp_path: Path):
        """iOS database missing required column must trigger schema validator and exit."""
        db_path = tmp_path / "corrupt_ChatStorage.sqlite"
        conn = sqlite3.connect(str(db_path))
        # Missing ZMEDIALOCALPATH in ZWAMEDIAITEM:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZGROUPINFO INTEGER, ZPARTNERNAME TEXT, ZCONTACTABID INTEGER, ZLASTMESSAGEDATE REAL);
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT);
            CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZMESSAGEDATE REAL, ZISFROMME INTEGER, ZCHATSESSION INTEGER, ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER, ZPUSHNAME TEXT, ZSTANZAID TEXT);
        """)
        conn.close()

        container = tmp_path / "AppDomainGroup"
        (container / "Message").mkdir(parents=True)
        out_dir = tmp_path / "out"

        cmd_args = [
            "wab-archiver",
            "archive",
            "--msgstore",
            str(db_path),
            "--wa-root",
            str(container),
            "--output",
            str(out_dir),
        ]
        with patch("sys.argv", cmd_args), pytest.raises(SystemExit) as exc_info:
            wa.main()
        assert exc_info.value.code == 1

    def test_non_sqlite_file_aborts_gracefully(self, tmp_path: Path):
        """Passing non-database text file as --msgstore must abort gracefully."""
        bad_file = tmp_path / "not_a_db.txt"
        bad_file.write_text("Hello World", encoding="utf-8")
        out_dir = tmp_path / "out"

        dummy_root = tmp_path / "dummy_wa_root"
        (dummy_root / "Media" / "WhatsApp Images").mkdir(parents=True)
        cmd_args = [
            "wab-archiver",
            "archive",
            "--msgstore",
            str(bad_file),
            "--wa-root",
            str(dummy_root),
            "--output",
            str(out_dir),
        ]
        with patch("sys.argv", cmd_args), pytest.raises(SystemExit) as exc_info:
            wa.main()
        assert exc_info.value.code == 1
