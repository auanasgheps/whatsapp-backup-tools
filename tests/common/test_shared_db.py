"""Unit tests for shared database utilities, sidecars, and formatters.

Tests shared.db with 100% synthetic data.
"""

from __future__ import annotations

from unittest.mock import patch

from shared import db
from tests.fixtures.protobuf_factory import build_field

# ---------------------------------------------------------------------------
# 1. Tests for SQLite Database & Sidecars Lifecycle (shared/db.py)
# ---------------------------------------------------------------------------


class TestDatabaseLifecycleAndSidecars:
    """Tests for clean_sqlite_files and safe_replace_sqlite_db."""

    def test_clean_sqlite_files_removes_all_sidecars(self, tmp_path) -> None:
        db_file = tmp_path / "msgstore.db"
        wal_file = tmp_path / "msgstore.db-wal"
        shm_file = tmp_path / "msgstore.db-shm"
        journal_file = tmp_path / "msgstore.db-journal"

        db_file.write_bytes(b"database")
        wal_file.write_bytes(b"wal_data")
        shm_file.write_bytes(b"shm_data")
        journal_file.write_bytes(b"journal_data")

        assert db_file.exists()
        assert wal_file.exists()
        assert shm_file.exists()
        assert journal_file.exists()

        db.clean_sqlite_files(str(db_file))

        assert not db_file.exists()
        assert not wal_file.exists()
        assert not shm_file.exists()
        assert not journal_file.exists()

    def test_clean_sqlite_files_missing_file_resilience(self, tmp_path) -> None:
        missing_db = tmp_path / "non_existent.db"
        # Must not raise OSError
        db.clean_sqlite_files(str(missing_db))

    def test_safe_replace_sqlite_db_with_sidecars(self, tmp_path) -> None:
        src_db = tmp_path / "incoming.db"
        src_wal = tmp_path / "incoming.db-wal"
        src_shm = tmp_path / "incoming.db-shm"

        dst_db = tmp_path / "target.db"
        dst_wal = tmp_path / "target.db-wal"
        dst_journal = tmp_path / "target.db-journal"

        src_db.write_bytes(b"src_db_content")
        src_wal.write_bytes(b"src_wal_content")
        src_shm.write_bytes(b"src_shm_content")

        dst_db.write_bytes(b"old_target_content")
        dst_wal.write_bytes(b"stale_wal_content")
        dst_journal.write_bytes(b"stale_journal_content")

        db.safe_replace_sqlite_db(str(src_db), str(dst_db))

        # Destination must now contain source content and source sidecars
        assert dst_db.exists()
        assert dst_db.read_bytes() == b"src_db_content"

        assert dst_wal.exists()
        assert dst_wal.read_bytes() == b"src_wal_content"

        dst_shm = tmp_path / "target.db-shm"
        assert dst_shm.exists()
        assert dst_shm.read_bytes() == b"src_shm_content"

        # Stale journal must have been cleaned
        assert not dst_journal.exists()

        # Source files should have been moved/replaced
        assert not src_db.exists()

    def test_safe_replace_sqlite_db_without_sidecars(self, tmp_path) -> None:
        src_db = tmp_path / "source.db"
        dst_db = tmp_path / "dest.db"

        src_db.write_bytes(b"fresh_source_data")
        dst_db.write_bytes(b"existing_dest_data")

        db.safe_replace_sqlite_db(str(src_db), str(dst_db))

        assert dst_db.exists()
        assert dst_db.read_bytes() == b"fresh_source_data"
        assert not src_db.exists()


# ---------------------------------------------------------------------------
# 2. Tests for Windows Long Path Handling (shared/db.py)
# ---------------------------------------------------------------------------


class TestWindowsLongPathHandling:
    """Tests for ensure_long_path on Windows and other platforms."""

    def test_ensure_long_path_short_path_unchanged(self) -> None:
        short_p = "C:\\WhatsApp\\Media\\IMG_001.jpg"
        assert db.ensure_long_path(short_p) == short_p

    def test_ensure_long_path_already_prefixed_unchanged(self) -> None:
        prefixed = "\\\\?\\C:\\WhatsApp\\VeryLongPath\\" + ("a" * 300)
        assert db.ensure_long_path(prefixed) == prefixed

    def test_ensure_long_path_drive_path_prefixing(self) -> None:
        long_drive_path = "C:\\WhatsAppArchive\\" + ("subfolder\\" * 30) + "file.dat"
        assert len(long_drive_path) >= 240

        with patch("sys.platform", "win32"):
            res = db.ensure_long_path(long_drive_path)
            assert res.startswith("\\\\?\\")
            assert "WhatsAppArchive" in res

    def test_ensure_long_path_unc_share_prefixing(self) -> None:
        long_unc_path = "\\\\nas_server\\whatsapp_backups\\" + ("media\\" * 35) + "video.mp4"
        assert len(long_unc_path) >= 240

        with patch("sys.platform", "win32"):
            with patch("os.path.abspath", return_value=long_unc_path):
                res = db.ensure_long_path(long_unc_path)
                assert res.startswith("\\\\?\\UNC\\")
                assert "nas_server\\whatsapp_backups" in res

    def test_ensure_long_path_non_windows_platform_unchanged(self) -> None:
        long_path = "/var/whatsapp/backups/" + ("subfolder/" * 30) + "file.dat"
        with patch("sys.platform", "linux"):
            assert db.ensure_long_path(long_path) == long_path


# ---------------------------------------------------------------------------
# 3. Tests for Media Classification (shared/db.py)
# ---------------------------------------------------------------------------


class TestMediaClassification:
    """Tests for classify_media_type."""

    def test_classify_media_type_empty_and_none(self) -> None:
        assert db.classify_media_type(None, None) == "document"
        assert db.classify_media_type("", "") == "document"
        assert db.classify_media_type(None, "") == "document"

    def test_classify_media_type_vcards(self) -> None:
        assert db.classify_media_type("contact.vcf", None) == "vcard"
        assert db.classify_media_type(None, "vcard:John Doe") == "vcard"
        assert db.classify_media_type("vcard:Multiple Contacts", None) == "vcard"

    def test_classify_media_type_stickers(self) -> None:
        assert db.classify_media_type("stk-12345.webp", None) == "sticker"
        assert db.classify_media_type("stk-99999.was", None) == "sticker"
        assert db.classify_media_type("WhatsApp Stickers/custom.webp", None) == "sticker"
        assert db.classify_media_type("Media/stickers/item.png", None) == "sticker"
        assert db.classify_media_type("item.webp", None) == "sticker"
        assert db.classify_media_type("item.was", None) == "sticker"

        # Document folder with webp is not treated as sticker
        assert db.classify_media_type("WhatsApp Documents/item.webp", None) != "sticker"

    def test_classify_media_type_gifs(self) -> None:
        assert db.classify_media_type("clip.gif", None) == "gif"
        assert db.classify_media_type("gif-12345.mp4", None) == "gif"
        assert db.classify_media_type("WhatsApp Animated Gifs/anim.mp4", None) == "gif"
        assert db.classify_media_type("Media/animated gifs/fun.mp4", None) == "gif"

    def test_classify_media_type_extensions(self) -> None:
        # Images
        for ext in (".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".bmp"):
            assert db.classify_media_type(f"photo{ext}", None) == "image"

        # Videos
        for ext in (".mp4", ".mov", ".avi", ".mkv", ".3gp", ".m4v"):
            assert db.classify_media_type(f"movie{ext}", None) == "video"

        # Audio
        for ext in (".mp3", ".ogg", ".aac", ".opus", ".m4a", ".wav", ".amr"):
            assert db.classify_media_type(f"track{ext}", None) == "audio"

        # Documents
        for ext in (
            ".pdf",
            ".doc",
            ".docx",
            ".xls",
            ".xlsx",
            ".ppt",
            ".pptx",
            ".txt",
            ".csv",
            ".rtf",
            ".zip",
            ".rar",
            ".7z",
            ".tar",
            ".gz",
        ):
            assert db.classify_media_type(f"manual{ext}", None) == "document"

    def test_classify_media_type_folder_fallbacks(self) -> None:
        # Extensionless or non-standard extension mapped by folder
        assert db.classify_media_type("Media/WhatsApp Images/FILE_001", None) == "image"
        assert db.classify_media_type("Media/photos/IMG_UNKNOWN", None) == "image"
        assert db.classify_media_type("Media/WhatsApp Video/VID_UNKNOWN", None) == "video"
        assert db.classify_media_type("Media/videos/CLIP_UNKNOWN", None) == "video"
        assert db.classify_media_type("Media/WhatsApp Audio/REC_UNKNOWN", None) == "audio"
        assert db.classify_media_type("Media/WhatsApp Voice Notes/PTT_UNKNOWN", None) == "audio"
        assert db.classify_media_type("Media/voice notes/NOTE_UNKNOWN", None) == "audio"
        assert db.classify_media_type("Media/WhatsApp Documents/DOC_UNKNOWN", None) == "document"
        assert db.classify_media_type("Media/docs/MANUAL_UNKNOWN", None) == "document"

        # Lines 175 and 177: sticker and gif folder fallbacks
        assert db.classify_media_type("Media/custom_sticker_repo/item", None) == "sticker"
        assert db.classify_media_type("Media/funny_gif_collection/item", None) == "gif"

        # Unmatched folder defaults to document
        assert db.classify_media_type("Media/OtherFiles/mystery.xyz", None) == "document"


# ---------------------------------------------------------------------------
# 4. Tests for vCard & Contact Formatting (shared/db.py)
# ---------------------------------------------------------------------------


class TestVCardAndContactFormatting:
    """Tests for resolve_vcard_display_name, format_phone, build_contact_folder_name."""

    def test_resolve_vcard_display_name_name_separator(self) -> None:
        # 1. Single name after separator
        sep_single = "Header_$!<Name-Separator>!$_Alice Smith"
        assert db.resolve_vcard_display_name(sep_single, None) == "Alice Smith"

        # 2. Two names after separator
        sep_pair = "Header_$!<Name-Separator>!$_Alice Smith_$!<Name-Separator>!$_Bob Jones"
        assert db.resolve_vcard_display_name(sep_pair, None) == "Alice Smith & Bob Jones"

        # 3. Three or more names after separator
        sep_multi = (
            "Header_$!<Name-Separator>!$_Alice Smith"
            "_$!<Name-Separator>!$_Bob Jones"
            "_$!<Name-Separator>!$_Charlie Brown"
        )
        assert db.resolve_vcard_display_name(sep_multi, None) == "Alice Smith and 2 others"

        # 4. Trailing separator with only single part (lines 48-49)
        sep_one_part = "OnlyHeader_$!<Name-Separator>!$_"
        assert db.resolve_vcard_display_name(sep_one_part, None) == "OnlyHeader"

    def test_resolve_vcard_display_name_localized_counters(self) -> None:
        # 1. Two contacts with 2 FN lines in vcard_text (line 79)
        vcard_pair = "BEGIN:VCARD\nFN:Alice\nEND:VCARD\nBEGIN:VCARD\nFN:Bob\nEND:VCARD"
        assert db.resolve_vcard_display_name("2 contatti", vcard_pair) == "Alice & Bob"

        # 2. Three contacts with 3 FN lines in vcard_text
        vcard_triple = "FN:Alice\nFN:Bob\nFN:Charlie"
        assert db.resolve_vcard_display_name("3 contacts", vcard_triple) == "Alice and 2 others"

        # 3. Single contact counter with 1 FN line
        vcard_single = "FN;CHARSET=UTF-8:Alice"
        assert db.resolve_vcard_display_name("1 kişi", vcard_single) == "Alice"

        # 4. Counter header but NO FN lines in vcard_text (line 83)
        assert (
            db.resolve_vcard_display_name("2 contatti", "BEGIN:VCARD\nTEL:123\nEND:VCARD")
            == "2 contatti"
        )

    def test_resolve_vcard_display_name_clean_and_fallback(self) -> None:
        # Normal display name
        assert db.resolve_vcard_display_name("Dr. Evelyn Reed", None) == "Dr. Evelyn Reed"

        # Bi-directional control characters stripped
        bidi_name = "\u200e\u200fJohn Doe\u200e"
        assert db.resolve_vcard_display_name(bidi_name, None) == "John Doe"

        # Empty / None raw name fallback to Contact
        assert db.resolve_vcard_display_name(None, None) == "Contact"
        assert db.resolve_vcard_display_name("", None) == "Contact"
        assert db.resolve_vcard_display_name("   ", "") == "Contact"

    def test_format_phone_and_folder_name(self) -> None:
        # format_phone prepends '00'
        assert db.format_phone("39123456789") == "0039123456789"
        assert db.format_phone("") == ""
        assert db.format_phone(None) == ""  # type: ignore[arg-type]

        # build_contact_folder_name
        # 1. Normal display name and number
        f1 = db.build_contact_folder_name("Alice Smith", "39123456789")
        assert f1 == "Alice Smith (0039123456789)"

        # 2. Display name identical to number -> Unknown (00number)
        f2 = db.build_contact_folder_name("39123456789", "39123456789")
        assert f2 == "Unknown (0039123456789)"

        # 3. Empty display name -> Unknown (00number)
        f3 = db.build_contact_folder_name("", "39123456789")
        assert f3 == "Unknown (0039123456789)"

        # 4. Empty display name and empty number -> Unknown Contact
        f4 = db.build_contact_folder_name("", "")
        assert f4 == "Unknown Contact"

        # 5. Display name with no number
        f5 = db.build_contact_folder_name("Alice Smith", "")
        assert f5 == "Alice Smith"

        # 6. Unsafe filename characters sanitized
        f6 = db.build_contact_folder_name('Dr. Who: The "Special" <Hero> / Rogue?', "15550010")
        assert "/" not in f6
        assert ":" not in f6
        assert '"' not in f6
        assert "<" not in f6
        assert ">" not in f6
        assert "?" not in f6


# ---------------------------------------------------------------------------
# 5. Tests for Protobuf Link Description & SQL Escaping (shared/db.py)
# ---------------------------------------------------------------------------


class TestProtobufLinkDescriptionAndSQLEscaping:
    """Tests for extract_ios_link_description, escape_like, and sanitize_filename."""

    def test_escape_like_special_characters(self) -> None:
        raw = "50%_discount\\deal"
        escaped = db.escape_like(raw)
        assert escaped == "50\\%\\_discount\\\\deal"

    def test_sanitize_filename_filesystem_characters(self) -> None:
        unsafe = 'my:bad?file*name<with>"illegal"|chars/and\\slashes.txt'
        sanitized = db.sanitize_filename(unsafe)
        assert ":" not in sanitized
        assert "?" not in sanitized
        assert "*" not in sanitized
        assert "<" not in sanitized
        assert ">" not in sanitized
        assert '"' not in sanitized
        assert "|" not in sanitized
        assert "/" not in sanitized
        assert "\\" not in sanitized

    def test_extract_ios_link_description_field_3(self) -> None:
        payload = "Detailed description of the shared web article."
        # Wire tag 3, wire type 2 length-delimited
        blob = build_field(3, 2, payload.encode("utf-8"))

        desc = db.extract_ios_link_description(blob)
        assert desc == payload

    def test_extract_ios_link_description_skips_other_wire_types(self) -> None:
        # Prepend:
        # - Wire 0 (varint) tag 1, value 1000 (multi-byte varint: lines 195-197)
        # - Wire 5 (fixed32) tag 2, 4 bytes (line 213)
        # - Wire 1 (fixed64) tag 4, 8 bytes (line 215)
        # - Wire 2 (len) tag 5, non-matching field num (line 211)
        # Followed by target tag 3:
        target_text = "Target Article Description"
        blob = (
            build_field(1, 0, 300)  # multi-byte varint
            + build_field(2, 5, b"\x01\x02\x03\x04")  # fixed32
            + build_field(4, 1, b"\x01\x02\x03\x04\x05\x06\x07\x08")  # fixed64
            + build_field(5, 2, b"other_string")  # len non-field-3
            + build_field(3, 2, target_text.encode("utf-8"))
        )

        desc = db.extract_ios_link_description(blob)
        assert desc == target_text

    def test_extract_ios_link_description_multi_byte_length_varint(self) -> None:
        # Large description payload requiring multi-byte length varint (lines 206-207)
        large_text = "A" * 300
        blob = build_field(3, 2, large_text.encode("utf-8"))

        desc = db.extract_ios_link_description(blob)
        assert desc == large_text

    def test_extract_ios_link_description_malformed_and_edge_cases(self) -> None:
        # None and non-bytes
        assert db.extract_ios_link_description(None) is None
        assert db.extract_ios_link_description(b"") is None
        assert db.extract_ios_link_description("not bytes") is None  # type: ignore[arg-type]

        # Truncated length-delimited payload for non-target field (field 2, len 16)
        truncated_non_target = b"\x12\x10abc"
        assert db.extract_ios_link_description(truncated_non_target) is None

        # Truncated varint
        incomplete_varint = b"\x08\x80\x80"
        assert db.extract_ios_link_description(incomplete_varint) is None

        # Unknown wire type (line 217 else: break)
        unknown_wire = b"\x03"  # wire type 3
        assert db.extract_ios_link_description(unknown_wire) is None

        # Exception handling (lines 218-220)
        class FaultyBytes(bytearray):
            def __getitem__(self, item):
                raise IndexError("synthetic fault")

        assert db.extract_ios_link_description(FaultyBytes(b"\x08")) is None
