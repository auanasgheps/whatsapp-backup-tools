"""Tests for wab_archiver."""

from __future__ import annotations

import hashlib
import logging
import os
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


import wab_archiver.archive_db as arc
import wab_archiver.main as wa


class TestBuildMissingRow:
    def test_1on1_known_sender(self):
        contacts = {"441234": "Alice"}
        row = wa._build_missing_row(
            msg_id=1,
            timestamp=1705276800000,
            file_path="img.jpg",
            mime_type="image/jpeg",
            chat_subject=None,
            sender="441234",
            key_from_me=0,
            message_url=None,
            media_name=None,
            contacts=contacts,
            number_map={},
        )
        assert row["chat_name"] == "Alice"
        assert row["sender"] == "Alice"

    def test_1on1_null_sender_unknown(self):
        row = wa._build_missing_row(
            msg_id=1,
            timestamp=None,
            file_path=None,
            mime_type=None,
            chat_subject=None,
            sender=None,
            key_from_me=0,
            message_url=None,
            media_name=None,
            contacts={},
            number_map={},
        )
        assert row["chat_name"] == "Unknown"
        assert row["sender"] == "Unknown"


class TestWriteMissingReport:
    def test_no_rows_does_not_create_file(self, tmp_path, logger):
        path = str(tmp_path / "missing.csv")
        wa.write_missing_report(path, [], logger)
        assert not os.path.exists(path)

    def test_rows_written_to_csv_with_correct_fields(self, tmp_path, logger):
        rows = [
            {
                "message_id": 1,
                "timestamp_human": "2024-01-15 00:00:00",
                "original_filename": "img.jpg",
                "mime_type": "image/jpeg",
                "sender": "Alice",
                "chat_name": "Family",
                "direction": "Received",
                "file_path": "Media/WhatsApp Images/img.jpg",
                "message_url": "https://mmg.whatsapp.net/v/example",
                "media_name": "",
            }
        ]
        path = str(tmp_path / "missing.csv")
        wa.write_missing_report(path, rows, logger)
        assert os.path.isfile(path)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "message_id" in content
        assert "img.jpg" in content
        assert "Alice" in content


class TestWriteDuplicateReport:
    def test_no_duplicates_does_not_create_file(self, tmp_path, logger):
        conn = arc.open_archive_db(str(tmp_path))
        path = str(tmp_path / "dups.csv")
        wa.write_duplicate_report(path, conn, logger, None)
        conn.close()
        assert not os.path.exists(path)

    def test_duplicates_written_to_csv(self, tmp_path, logger):

        conn = arc.open_archive_db(str(tmp_path))
        cursor = conn.cursor()
        md5 = hashlib.md5(b"data").digest()
        cursor.execute("INSERT INTO files (original_path, md5) VALUES (?, ?)", ("orig.jpg", md5))
        cursor.execute(
            "INSERT INTO archive_copies (original_path, archive_path, is_hardlink, timestamp_ms) VALUES (?, ?, ?, ?)",
            ("orig.jpg", "Contacts/Alice (00111)/2024/Received/orig.jpg", 0, 1718000000000),
        )
        cursor.execute(
            "INSERT INTO archive_copies (original_path, archive_path, is_hardlink, timestamp_ms) VALUES (?, ?, ?, ?)",
            ("orig.jpg", "Groups/Family/2024/orig_Alice.jpg", 1, 1718000000000),
        )
        conn.commit()
        path = str(tmp_path / "dups.csv")
        wa.write_duplicate_report(path, conn, logger, None)
        conn.close()
        assert os.path.isfile(path)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "md5_hex" in content
        assert "file_count" in content
        assert "link_status" in content
        assert "original_timestamp" in content
        assert "media_type" in content
        assert "2024-" in content
        assert "# --- PHOTOS & IMAGES" in content

    def test_duplicate_report_excludes_thumbnails_and_empty_hashes(self, tmp_path, logger):

        conn = arc.open_archive_db(str(tmp_path))
        cursor = conn.cursor()

        # 1. Unhashed / empty-hashed thumbnails (empty bytes b"")
        cursor.execute(
            "INSERT INTO files (original_path, md5) VALUES (?, ?)", ("Message/thumb1.thumb", b"")
        )
        cursor.execute(
            "INSERT INTO archive_copies (original_path, archive_path, is_hardlink, timestamp_ms) VALUES (?, ?, ?, ?)",
            (
                "Message/thumb1.thumb",
                "Whatsapp Databases/.thumbnails/thumb1.thumb",
                0,
                1718000000000,
            ),
        )
        cursor.execute(
            "INSERT INTO files (original_path, md5) VALUES (?, ?)", ("Message/thumb2.thumb", b"")
        )
        cursor.execute(
            "INSERT INTO archive_copies (original_path, archive_path, is_hardlink, timestamp_ms) VALUES (?, ?, ?, ?)",
            (
                "Message/thumb2.thumb",
                "Whatsapp Databases/.thumbnails/thumb2.thumb",
                0,
                1718000000000,
            ),
        )

        # 2. Hashed thumbnails that share an identical MD5
        shared_thumb_md5 = hashlib.md5(b"shared_thumbnail_bytes").digest()
        cursor.execute(
            "INSERT INTO files (original_path, md5) VALUES (?, ?)",
            ("Message/thumb3.jpg", shared_thumb_md5),
        )
        cursor.execute(
            "INSERT INTO archive_copies (original_path, archive_path, is_hardlink, timestamp_ms) VALUES (?, ?, ?, ?)",
            ("Message/thumb3.jpg", "Whatsapp Databases/.thumbnails/thumb3.jpg", 0, 1718000000000),
        )
        cursor.execute(
            "INSERT INTO files (original_path, md5) VALUES (?, ?)",
            ("Message/thumb4.jpg", shared_thumb_md5),
        )
        cursor.execute(
            "INSERT INTO archive_copies (original_path, archive_path, is_hardlink, timestamp_ms) VALUES (?, ?, ?, ?)",
            ("Message/thumb4.jpg", "Whatsapp Databases/.thumbnails/thumb4.jpg", 0, 1718000000000),
        )

        # 3. Legitimate user media duplicate
        user_md5 = hashlib.md5(b"user_photo").digest()
        cursor.execute(
            "INSERT INTO files (original_path, md5) VALUES (?, ?)", ("Media/photo.jpg", user_md5)
        )
        cursor.execute(
            "INSERT INTO archive_copies (original_path, archive_path, is_hardlink, timestamp_ms) VALUES (?, ?, ?, ?)",
            ("Media/photo.jpg", "Contacts/Alice (001)/2024/Received/photo.jpg", 0, 1718000000000),
        )
        cursor.execute(
            "INSERT INTO archive_copies (original_path, archive_path, is_hardlink, timestamp_ms) VALUES (?, ?, ?, ?)",
            ("Media/photo.jpg", "Groups/Friends/2024/photo_Alice.jpg", 1, 1718000000000),
        )
        conn.commit()

        path = str(tmp_path / "dups_thumb_test.csv")
        summary = wa.write_duplicate_report(path, conn, logger, None)
        conn.close()

        assert summary[0] == 1  # Exactly 1 duplicate group (the user media)
        assert summary[1] == 2  # Exactly 2 files in that group
        assert os.path.isfile(path)
        with open(path, encoding="utf-8") as f:
            content = f.read()

        assert "Contacts/Alice (001)/2024/Received/photo.jpg" in content
        assert "Groups/Friends/2024/photo_Alice.jpg" in content
        assert ".thumbnails" not in content

    def test_load_canonical_copies_excludes_thumbnails_and_empty_hashes(self, tmp_path):

        conn = arc.open_archive_db(str(tmp_path))
        cursor = conn.cursor()

        thumb_dir = tmp_path / "Whatsapp Databases" / ".thumbnails"
        thumb_dir.mkdir(parents=True, exist_ok=True)
        thumb_file = thumb_dir / "thumb.jpg"
        thumb_file.write_bytes(b"thumb_content")

        user_dir = tmp_path / "Contacts" / "Bob"
        user_dir.mkdir(parents=True, exist_ok=True)
        user_file = user_dir / "pic.jpg"
        user_file.write_bytes(b"user_content")

        thumb_md5 = hashlib.md5(b"thumb_content").digest()
        user_md5 = hashlib.md5(b"user_content").digest()

        # Empty hash record
        cursor.execute("INSERT INTO files (original_path, md5) VALUES (?, ?)", ("empty.jpg", b""))
        cursor.execute(
            "INSERT INTO archive_copies (original_path, archive_path, is_hardlink, timestamp_ms) VALUES (?, ?, ?, ?)",
            ("empty.jpg", "Contacts/Bob/empty.jpg", 0, 1718000000000),
        )

        # Thumbnail record
        cursor.execute(
            "INSERT INTO files (original_path, md5) VALUES (?, ?)", ("thumb.jpg", thumb_md5)
        )
        cursor.execute(
            "INSERT INTO archive_copies (original_path, archive_path, is_hardlink, timestamp_ms) VALUES (?, ?, ?, ?)",
            ("thumb.jpg", "Whatsapp Databases/.thumbnails/thumb.jpg", 0, 1718000000000),
        )

        # User media record
        cursor.execute(
            "INSERT INTO files (original_path, md5) VALUES (?, ?)", ("pic.jpg", user_md5)
        )
        cursor.execute(
            "INSERT INTO archive_copies (original_path, archive_path, is_hardlink, timestamp_ms) VALUES (?, ?, ?, ?)",
            ("pic.jpg", "Contacts/Bob/pic.jpg", 0, 1718000000000),
        )
        conn.commit()

        canonical_map = arc.load_canonical_copies(conn, str(tmp_path))
        conn.close()

        assert b"" not in canonical_map
        assert thumb_md5 not in canonical_map
        assert user_md5 in canonical_map
        assert canonical_map[user_md5] == str(user_file)

    def test_classify_media_type(self):
        from shared.db import classify_media_type

        # Basic extensions
        assert classify_media_type("photo.jpg", None) == "image"
        assert classify_media_type("photo.jpeg", None) == "image"
        assert classify_media_type("photo.png", None) == "image"
        assert classify_media_type("photo.heic", None) == "image"
        assert classify_media_type("clip.mp4", None) == "video"
        assert classify_media_type("clip.mov", None) == "video"
        assert classify_media_type("doc.pdf", None) == "document"
        assert classify_media_type("sheet.xlsx", None) == "document"
        assert classify_media_type("song.mp3", None) == "audio"
        assert classify_media_type("voice.opus", None) == "audio"
        assert classify_media_type("contact.vcf", None) == "vcard"
        assert classify_media_type("vcard:123", None) == "vcard"

        # Stickers (both iOS and Android)
        assert classify_media_type("stk-123.webp", None) == "sticker"
        assert classify_media_type("Media/WhatsApp Stickers/random.webp", None) == "sticker"
        assert classify_media_type("Message/stickers/abc.webp", None) == "sticker"
        assert classify_media_type("Message/Media/123/abc.webp", None) == "sticker"
        assert (
            classify_media_type(
                "Media/390000000005-1600000000@g.us/6/f/11111111-2222-3333-4444-555555555555.webp",
                None,
            )
            == "sticker"
        )
        assert classify_media_type("Contacts/Alice/2026/Received/abc.webp", None) == "sticker"
        assert classify_media_type("custom_pack.was", None) == "sticker"
        assert classify_media_type("random_hash.webp", None) == "sticker"

        # GIFs (including WhatsApp Android mp4 gif storage)
        assert classify_media_type("funny.gif", None) == "gif"
        assert classify_media_type("Media/WhatsApp Animated Gifs/GIF-123.mp4", None) == "gif"
        assert classify_media_type("GIF-123.mp4", None) == "gif"

        # Fallback to folder structure
        assert classify_media_type("Media/WhatsApp Images/unknown_blob", None) == "image"
        assert classify_media_type("Media/WhatsApp Video/unknown_blob", None) == "video"
        assert classify_media_type("Media/WhatsApp Audio/unknown_blob", None) == "audio"
        assert classify_media_type("Media/WhatsApp Documents/unknown_blob", None) == "document"

        # Fallback path parameter
        assert classify_media_type(None, "Media/WhatsApp Images/photo.jpg") == "image"

    def test_duplicate_report_sections_and_ordering(self, tmp_path, logger):
        import csv

        conn = arc.open_archive_db(str(tmp_path))
        cursor = conn.cursor()

        # 1. Sticker with highest duplicate count (4 duplicates)
        stk_h = hashlib.md5(b"sticker_bytes").digest()
        cursor.execute(
            "INSERT INTO files (original_path, md5) VALUES (?, ?)",
            ("Media/WhatsApp Stickers/stk.webp", stk_h),
        )
        for i in range(4):
            cursor.execute(
                "INSERT INTO archive_copies (original_path, archive_path, is_hardlink, timestamp_ms) VALUES (?, ?, ?, ?)",
                (
                    "Media/WhatsApp Stickers/stk.webp",
                    f"Contacts/C{i}/2024/stk.webp",
                    1 if i > 0 else 0,
                    1718000000000,
                ),
            )

        # 2. Animated GIF (2 duplicates)
        gif_h = hashlib.md5(b"gif_bytes").digest()
        cursor.execute(
            "INSERT INTO files (original_path, md5) VALUES (?, ?)",
            ("Media/WhatsApp Animated Gifs/GIF-01.mp4", gif_h),
        )
        for i in range(2):
            cursor.execute(
                "INSERT INTO archive_copies (original_path, archive_path, is_hardlink, timestamp_ms) VALUES (?, ?, ?, ?)",
                (
                    "Media/WhatsApp Animated Gifs/GIF-01.mp4",
                    f"Contacts/C{i}/2024/gif.mp4",
                    1 if i > 0 else 0,
                    1718000000000,
                ),
            )

        # 3. Photo (2 duplicates)
        img_h = hashlib.md5(b"image_bytes").digest()
        cursor.execute(
            "INSERT INTO files (original_path, md5) VALUES (?, ?)",
            ("Media/WhatsApp Images/IMG-01.jpg", img_h),
        )
        for i in range(2):
            cursor.execute(
                "INSERT INTO archive_copies (original_path, archive_path, is_hardlink, timestamp_ms) VALUES (?, ?, ?, ?)",
                (
                    "Media/WhatsApp Images/IMG-01.jpg",
                    f"Contacts/C{i}/2024/img.jpg",
                    1 if i > 0 else 0,
                    1718000000000,
                ),
            )

        # 4. Video (2 duplicates)
        vid_h = hashlib.md5(b"video_bytes").digest()
        cursor.execute(
            "INSERT INTO files (original_path, md5) VALUES (?, ?)",
            ("Media/WhatsApp Video/VID-01.mp4", vid_h),
        )
        for i in range(2):
            cursor.execute(
                "INSERT INTO archive_copies (original_path, archive_path, is_hardlink, timestamp_ms) VALUES (?, ?, ?, ?)",
                (
                    "Media/WhatsApp Video/VID-01.mp4",
                    f"Contacts/C{i}/2024/vid.mp4",
                    1 if i > 0 else 0,
                    1718000000000,
                ),
            )

        conn.commit()

        path = str(tmp_path / "dup_sections.csv")
        res = wa.write_duplicate_report(path, conn, logger, None)
        group_count, file_count = res
        conn.close()

        assert group_count == 4
        assert file_count == 10
        assert res.sections == [
            ("PHOTOS & IMAGES", 1, 2),
            ("VIDEOS", 1, 2),
            ("ANIMATED GIFS", 1, 2),
            ("STICKERS", 1, 4),
        ]

        with open(path, newline="", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]

        # Check section divider presence and ordering:
        # High value first: PHOTOS -> VIDEOS -> ANIMATED GIFS -> STICKERS
        divider_lines = [
            line for line in lines if line.startswith('"# ---') or line.startswith("# ---")
        ]
        assert len(divider_lines) == 4
        assert "PHOTOS & IMAGES" in divider_lines[0]
        assert "VIDEOS" in divider_lines[1]
        assert "ANIMATED GIFS" in divider_lines[2]
        assert "STICKERS" in divider_lines[3]

        with open(path, newline="", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))

        # Filter out divider rows to examine media items
        media_rows = [r for r in reader if r["group_id"]]
        assert len(media_rows) == 10

        # Verify group_id numbering increments across sections (1..4)
        group_ids = [int(r["group_id"]) for r in media_rows]
        assert set(group_ids) == {1, 2, 3, 4}

        # Photos are group 1, Videos group 2, GIFs group 3, Stickers group 4
        img_rows = [r for r in media_rows if r["media_type"] == "image"]
        vid_rows = [r for r in media_rows if r["media_type"] == "video"]
        gif_rows = [r for r in media_rows if r["media_type"] == "gif"]
        stk_rows = [r for r in media_rows if r["media_type"] == "sticker"]

        assert all(r["group_id"] == "1" for r in img_rows)
        assert all(r["group_id"] == "2" for r in vid_rows)
        assert all(r["group_id"] == "3" for r in gif_rows)
        assert all(r["group_id"] == "4" for r in stk_rows)

    def test_duplicate_report_ios_and_android_stickers_classified_as_stickers(
        self, tmp_path, logger
    ):
        import csv

        conn = arc.open_archive_db(str(tmp_path))
        cursor = conn.cursor()

        # 1. Photo (2 duplicates)
        img_h = hashlib.md5(b"real_photo_bytes").digest()
        cursor.execute(
            "INSERT INTO files (original_path, md5) VALUES (?, ?)",
            ("Media/WhatsApp Images/IMG-01.jpg", img_h),
        )
        for i in range(2):
            cursor.execute(
                "INSERT INTO archive_copies (original_path, archive_path, is_hardlink, timestamp_ms) VALUES (?, ?, ?, ?)",
                (
                    "Media/WhatsApp Images/IMG-01.jpg",
                    f"Contacts/C{i}/2024/img.jpg",
                    1 if i > 0 else 0,
                    1718000000000,
                ),
            )

        # 2. iOS Sticker with typical hex hash without stk- prefix or stickers folder
        ios_stk_h = hashlib.md5(b"ios_sticker_payload").digest()
        ios_orig = "Message/Media/390000000005-1600000000@g.us/6/f/11111111-2222-3333-4444-555555555555.webp"
        cursor.execute(
            "INSERT INTO files (original_path, md5) VALUES (?, ?)",
            (ios_orig, ios_stk_h),
        )
        for i in range(2):
            cursor.execute(
                "INSERT INTO archive_copies (original_path, archive_path, is_hardlink, timestamp_ms) VALUES (?, ?, ?, ?)",
                (
                    ios_orig,
                    f"Contacts/C{i}/2026/Received/11111111-2222-3333-4444-555555555555.webp",
                    1 if i > 0 else 0,
                    1718000000000,
                ),
            )

        # 3. Android Sticker
        android_stk_h = hashlib.md5(b"android_sticker_payload").digest()
        android_orig = "Media/WhatsApp Stickers/STK-20240115-WA0001.webp"
        cursor.execute(
            "INSERT INTO files (original_path, md5) VALUES (?, ?)",
            (android_orig, android_stk_h),
        )
        for i in range(2):
            cursor.execute(
                "INSERT INTO archive_copies (original_path, archive_path, is_hardlink, timestamp_ms) VALUES (?, ?, ?, ?)",
                (
                    android_orig,
                    f"Contacts/C{i}/2024/STK-20240115-WA0001.webp",
                    1 if i > 0 else 0,
                    1718000000000,
                ),
            )

        conn.commit()

        path = str(tmp_path / "dup_stickers_report.csv")
        group_count, file_count = wa.write_duplicate_report(path, conn, logger, None)
        conn.close()

        assert group_count == 3
        assert file_count == 6

        with open(path, newline="", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]

        divider_lines = [
            line for line in lines if line.startswith('"# ---') or line.startswith("# ---")
        ]
        assert len(divider_lines) == 2
        assert "PHOTOS & IMAGES" in divider_lines[0]
        assert "STICKERS" in divider_lines[1]

        with open(path, newline="", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))

        media_rows = [r for r in reader if r["group_id"]]
        img_rows = [r for r in media_rows if r["media_type"] == "image"]
        stk_rows = [r for r in media_rows if r["media_type"] == "sticker"]

        # Only the real photo must be in images
        assert len(img_rows) == 2
        assert all("img.jpg" in r["archived_path"] for r in img_rows)

        # Both iOS and Android stickers must be in stickers
        assert len(stk_rows) == 4
        ios_in_report = [
            r for r in stk_rows if "11111111-2222-3333-4444-555555555555.webp" in r["archived_path"]
        ]
        android_in_report = [
            r for r in stk_rows if "STK-20240115-WA0001.webp" in r["archived_path"]
        ]
        assert len(ios_in_report) == 2
        assert len(android_in_report) == 2


class TestWriteConflictReport:
    def test_no_rows_does_not_create_file(self, tmp_path, logger):
        path = str(tmp_path / "conflicts.csv")
        wa.write_conflict_report(path, [], logger)
        assert not os.path.exists(path)

    def test_rows_written_with_correct_fields(self, tmp_path, logger):
        rows = [
            {
                "file_path": "Media/WhatsApp Images/img.jpg",
                "chosen_source": "/root1/Media/WhatsApp Images/img.jpg",
                "chosen_size": 12345,
                "rejected_source": "/root2/Media/WhatsApp Images/img.jpg",
                "rejected_size": "9000",
                "reason": "largest_wins",
            }
        ]
        path = str(tmp_path / "conflicts.csv")
        wa.write_conflict_report(path, rows, logger)
        assert os.path.isfile(path)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "file_path" in content
        assert "largest_wins" in content
        assert "Media/WhatsApp Images/img.jpg" in content


class TestBuildArchiveFilenameIndex:
    def test_returns_basename_size_md5(self, tmp_path):
        conn = arc.open_archive_db(str(tmp_path))
        cursor = conn.cursor()
        md5a = b"\x01" * 16
        md5b = b"\x02" * 16
        arc.record_file_archived(
            cursor,
            "/staging/WhatsApp Images/photo.jpg",
            md5a,
            "Contacts/Alice/photo.jpg",
            2048,
            0,
            None,
        )
        arc.record_file_archived(
            cursor,
            "/staging/WhatsApp Video/clip.mp4",
            md5b,
            "Contacts/Bob/clip.mp4",
            512000,
            0,
            None,
        )
        conn.commit()
        index = arc.build_archive_filename_index(conn)
        assert "photo.jpg" in index
        assert index["photo.jpg"] == (2048, md5a)
        assert "clip.mp4" in index
        assert index["clip.mp4"] == (512000, md5b)
        conn.close()

    def test_null_size_preserved(self, tmp_path):
        conn = arc.open_archive_db(str(tmp_path))
        # Simulate a pre-size-column record by inserting with NULL size directly
        conn.execute(
            "INSERT INTO files (original_path, md5, size) VALUES (?, ?, NULL)",
            ("/old/path/legacy.jpg", b"\xaa" * 16),
        )
        conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES (?, ?)",
            ("/old/path/legacy.jpg", "Contacts/old/legacy.jpg"),
        )
        conn.commit()
        index = arc.build_archive_filename_index(conn)
        assert "legacy.jpg" in index
        size, md5 = index["legacy.jpg"]
        assert size is None
        assert md5 == b"\xaa" * 16
        conn.close()

    def test_migration_adds_size_column_to_existing_db(self, tmp_path):
        # Create a DB without size column, then re-open — migration should add it
        import sqlite3 as _sqlite3

        db_path = str(tmp_path / ".wa_media_archiver.db")
        old_conn = _sqlite3.connect(db_path)
        old_conn.executescript("""
            CREATE TABLE IF NOT EXISTS files (
                original_path TEXT PRIMARY KEY,
                md5 BLOB NOT NULL
            );
        """)
        old_conn.close()
        conn = arc.open_archive_db(str(tmp_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
        assert "size" in cols
        conn.close()


class TestRouteContact:
    def test_falls_back_to_contact_index(self, tmp_path):
        contact_index = {"441234": ("Alice (00441234)", "Alice")}
        dest_dir, filename = wa._route_contact(
            sender="441234",
            key_from_me=0,
            contacts={},
            number_map={},
            filename="img.jpg",
            year="2024",
            output_root=str(tmp_path),
            contact_index=contact_index,
        )
        assert "Alice" in dest_dir
        assert filename == "img.jpg"


class TestCopyOrSkip:
    def test_dry_run_identical_file_returns_skip(self, tmp_path, logger):
        src = tmp_path / "src.jpg"
        src.write_bytes(b"test content")
        dest_path = tmp_path / "dest.jpg"
        dest_path.write_bytes(b"test content")
        result = wa._copy_or_skip(
            str(src),
            str(dest_path),
            str(tmp_path / "dest_dir"),
            "original/path.jpg",
            1705276800000,
            str(tmp_path),
            True,
            logger,
            None,
            False,
            {},
        )
        assert result == (0, 1, 0)

    def test_existing_identical_with_cursor_records_archived(self, tmp_path, logger):
        src = tmp_path / "src.jpg"
        src.write_bytes(b"test content")
        dest_dir = tmp_path / "Contacts" / "Alice"
        dest_dir.mkdir(parents=True)
        dest_path = dest_dir / "dest.jpg"
        dest_path.write_bytes(b"test content")
        conn = arc.open_archive_db(str(tmp_path))
        cursor = conn.cursor()
        result = wa._copy_or_skip(
            str(src),
            str(dest_path),
            str(dest_dir),
            "original/path.jpg",
            1705276800000,
            str(tmp_path),
            False,
            logger,
            cursor,
            False,
            {},
        )
        conn.commit()
        assert result == (0, 1, 0)
        rows = conn.execute("SELECT * FROM archive_copies").fetchall()
        assert len(rows) == 1
        conn.close()

    def test_copy_error_returns_warning(self, tmp_path, logger):
        src = tmp_path / "src.jpg"
        src.write_bytes(b"test content")
        dest_path = tmp_path / "out" / "dest.jpg"
        with patch("wab_archiver.main.shutil.copy", side_effect=OSError("disk full")):
            result = wa._copy_or_skip(
                str(src),
                str(dest_path),
                str(dest_path.parent),
                "original/path.jpg",
                1705276800000,
                str(tmp_path),
                False,
                logger,
                None,
                False,
                {},
            )
        assert result == (0, 0, 1)

    def test_set_file_times_oserror_does_not_fail(self, tmp_path, logger):
        src = tmp_path / "src.jpg"
        src.write_bytes(b"test content")
        dest_path = tmp_path / "out" / "dest.jpg"
        with patch("wab_archiver.main.set_file_times", side_effect=OSError("no access")):
            result = wa._copy_or_skip(
                str(src),
                str(dest_path),
                str(dest_path.parent),
                "original/path.jpg",
                1705276800000,
                str(tmp_path),
                False,
                logger,
                None,
                False,
                {},
            )
        assert result == (1, 0, 0)
        assert dest_path.exists()


class TestProcessRows:
    """
    Uses a fixed timestamp of 1705276800000 (2024-01-15 00:00:00 UTC).
    All destination path assertions use year='2024'.
    """

    _TS = 1705276800000  # 2024-01-15 00:00:00 UTC

    def _row(
        self,
        msg_id=1,
        timestamp=None,
        file_path="img.jpg",
        mime_type=None,
        chat_row_id="42",
        chat_subject="Family",
        sender="111",
        key_from_me=0,
        message_url=None,
        media_name=None,
    ):
        return (
            msg_id,
            timestamp if timestamp is not None else self._TS,
            file_path,
            mime_type,
            chat_row_id,
            chat_subject,
            sender,
            key_from_me,
            message_url,
            media_name,
        )

    def _setup_src(self, tmp_path, name="img.jpg", content=b"image data"):
        src_dir = tmp_path / "src"
        src_dir.mkdir(exist_ok=True)
        (src_dir / name).write_bytes(content)
        return str(src_dir)

    def _resolver(self, src_dir):
        return lambda fp: os.path.join(src_dir, fp)

    # --- Routing ---

    def test_group_chat_routed_to_groups_folder(self, tmp_path, logger):
        src_dir = self._setup_src(tmp_path)
        out = str(tmp_path / "out")
        os.makedirs(out)
        rows = [self._row(chat_subject="Family", sender="111", key_from_me=0)]
        stats, _, _, missing = wa.process_rows(
            rows,
            1,
            {"111": "Alice"},
            {},
            {},
            {},
            self._resolver(src_dir),
            out,
            logger,
            False,
            None,
            None,
            False,
            {},
        )
        assert stats["copied"] == 1
        assert stats["missing"] == 0
        assert os.path.isfile(os.path.join(out, "Groups", "Family", "2024", "img_Alice.jpg"))

    def test_group_own_message_tagged_me(self, tmp_path, logger):
        src_dir = self._setup_src(tmp_path)
        out = str(tmp_path / "out")
        os.makedirs(out)
        # key_from_me=1, sender=None — own group message
        rows = [self._row(chat_subject="Family", sender=None, key_from_me=1)]
        stats, _, _, _ = wa.process_rows(
            rows,
            1,
            {},
            {},
            {},
            {},
            self._resolver(src_dir),
            out,
            logger,
            False,
            None,
            None,
            False,
            {},
        )
        assert stats["copied"] == 1
        assert os.path.isfile(os.path.join(out, "Groups", "Family", "2024", "img_Me.jpg"))

    def test_one_to_one_received_routed_to_received_folder(self, tmp_path, logger):
        src_dir = self._setup_src(tmp_path)
        out = str(tmp_path / "out")
        os.makedirs(out)
        # chat_subject=None → 1-to-1 chat
        rows = [self._row(chat_subject=None, sender="111", key_from_me=0)]
        stats, _, _, _ = wa.process_rows(
            rows,
            1,
            {"111": "Alice"},
            {},
            {},
            {},
            self._resolver(src_dir),
            out,
            logger,
            False,
            None,
            None,
            False,
            {},
        )
        assert stats["copied"] == 1
        assert os.path.isfile(
            os.path.join(out, "Contacts", "Alice (00111)", "2024", "Received", "img.jpg")
        )

    def test_one_to_one_sent_routed_to_sent_folder(self, tmp_path, logger):
        src_dir = self._setup_src(tmp_path)
        out = str(tmp_path / "out")
        os.makedirs(out)
        rows = [self._row(chat_subject=None, sender="111", key_from_me=1)]
        wa.process_rows(
            rows,
            1,
            {"111": "Alice"},
            {},
            {},
            {},
            self._resolver(src_dir),
            out,
            logger,
            False,
            None,
            None,
            False,
            {},
        )
        assert os.path.isfile(
            os.path.join(out, "Contacts", "Alice (00111)", "2024", "Sent", "img.jpg")
        )

    def test_sticker_archiving_group_and_contact(self, tmp_path, logger):
        src_dir = tmp_path / "src"
        stk_file = src_dir / "Media" / "WhatsApp Stickers" / "STK-20240115-WA0001.webp"
        stk_file.parent.mkdir(parents=True, exist_ok=True)
        stk_file.write_bytes(b"RIFF....WEBP")
        out = str(tmp_path / "out")
        os.makedirs(out)
        rows = [
            self._row(
                file_path="Media/WhatsApp Stickers/STK-20240115-WA0001.webp",
                chat_subject="Family",
                sender="111",
                key_from_me=0,
            )
        ]
        stats, _, _, _ = wa.process_rows(
            rows,
            1,
            {"111": "Alice"},
            {},
            {},
            {},
            self._resolver(src_dir),
            out,
            logger,
            False,
            None,
            None,
            False,
            {},
        )
        assert stats["copied"] == 1
        assert os.path.isfile(
            os.path.join(
                out,
                "Groups",
                "Family",
                "2024",
                "STK-20240115-WA0001_Alice.webp",
            )
        )

    def test_unknown_sender_falls_back_to_phone_number(self, tmp_path, logger):
        src_dir = self._setup_src(tmp_path)
        out = str(tmp_path / "out")
        os.makedirs(out)
        rows = [self._row(chat_subject=None, sender="999", key_from_me=0)]
        wa.process_rows(
            rows,
            1,
            {},
            {},
            {},
            {},
            self._resolver(src_dir),
            out,
            logger,
            False,
            None,
            None,
            False,
            {},
        )
        assert os.path.isfile(
            os.path.join(out, "Contacts", "Unknown (00999)", "2024", "Received", "img.jpg")
        )

    def test_number_map_consolidates_old_number_to_canonical(self, tmp_path, logger):
        src_dir = self._setup_src(tmp_path)
        out = str(tmp_path / "out")
        os.makedirs(out)
        rows = [self._row(chat_subject=None, sender="111", key_from_me=0)]
        # 111 is the old number; 222 is canonical; contacts only knows 222
        wa.process_rows(
            rows,
            1,
            {"222": "Alice"},
            {"111": "222"},
            {},
            {},
            self._resolver(src_dir),
            out,
            logger,
            False,
            None,
            None,
            False,
            {},
        )
        assert os.path.isfile(
            os.path.join(out, "Contacts", "Alice (00222)", "2024", "Received", "img.jpg")
        )

    # --- Missing / invalid rows ---

    def test_missing_source_file_added_to_missing_rows(self, tmp_path, logger):
        out = str(tmp_path / "out")
        os.makedirs(out)
        rows = [self._row(file_path="nonexistent.jpg")]
        stats, _, _, missing = wa.process_rows(
            rows,
            1,
            {},
            {},
            {},
            {},
            lambda fp: str(tmp_path / "nowhere" / fp),
            out,
            logger,
            False,
            None,
            None,
            False,
            {},
        )
        assert stats["missing"] == 1
        assert stats["copied"] == 0
        assert len(missing) == 1

    def test_missing_source_file_with_thumbnail_logs_viewer_notice(self, tmp_path, caplog):
        out = str(tmp_path / "out")
        os.makedirs(out)
        thumb_src = tmp_path / "thumb.jpg"
        thumb_src.write_bytes(b"THUMB")

        log = logging.getLogger("test_thumb_warn")
        row = self._row(msg_id=42, file_path="deep/path/missing_photo.jpg") + (str(thumb_src),)

        def resolver(fp):
            if fp == str(thumb_src):
                return str(thumb_src)
            return None

        with caplog.at_level(logging.WARNING, logger="test_thumb_warn"):
            wa.process_rows(
                [row],
                1,
                {},
                {},
                {},
                {},
                resolver,
                out,
                log,
                False,
                None,
                None,
                False,
                {},
            )
        assert any(
            "MISSING source file 'missing_photo.jpg' (message_id=42) — A thumbnail has been found and will be used as backup in the Viewer"
            in r.message
            for r in caplog.records
        )

    def test_missing_source_file_without_thumbnail_logs_compact_warning(self, tmp_path, caplog):
        out = str(tmp_path / "out")
        os.makedirs(out)
        log = logging.getLogger("test_no_thumb_warn")
        row = self._row(msg_id=42, file_path="deep/path/missing_photo.jpg")

        with caplog.at_level(logging.WARNING, logger="test_no_thumb_warn"):
            wa.process_rows(
                [row],
                1,
                {},
                {},
                {},
                {},
                lambda fp: None,
                out,
                log,
                False,
                None,
                None,
                False,
                {},
            )
        assert any(
            "MISSING source file 'missing_photo.jpg' (message_id=42)" in r.message
            for r in caplog.records
        )
        assert not any("A thumbnail has been found" in r.message for r in caplog.records)

    def test_null_file_path_added_to_missing_rows(self, tmp_path, logger):
        out = str(tmp_path / "out")
        os.makedirs(out)
        rows = [self._row(file_path=None)]
        stats, _, _, missing = wa.process_rows(
            rows,
            1,
            {},
            {},
            {},
            {},
            lambda fp: fp,
            out,
            logger,
            False,
            None,
            None,
            False,
            {},
        )
        assert stats["missing"] == 1
        assert len(missing) == 1

    def test_null_timestamp_added_to_missing_rows(self, tmp_path, logger):
        src_dir = self._setup_src(tmp_path)
        out = str(tmp_path / "out")
        os.makedirs(out)
        row = (1, None, "img.jpg", None, "42", "Family", "111", 0, None, None)
        stats, _, _, missing = wa.process_rows(
            [row],
            1,
            {},
            {},
            {},
            {},
            self._resolver(src_dir),
            out,
            logger,
            False,
            None,
            None,
            False,
            {},
        )
        assert stats["missing"] == 1
        assert stats["copied"] == 0
        assert len(missing) == 1

    # --- Dry run ---

    def test_dry_run_increments_copied_but_writes_no_file(self, tmp_path, logger):
        src_dir = self._setup_src(tmp_path)
        out = str(tmp_path / "out")
        os.makedirs(out)
        rows = [self._row(chat_subject="Family", sender="111")]
        stats, _, _, _ = wa.process_rows(
            rows,
            1,
            {"111": "Alice"},
            {},
            {},
            {},
            self._resolver(src_dir),
            out,
            logger,
            True,
            None,
            None,
            False,
            {},
        )
        assert stats["copied"] == 1
        assert not os.path.isfile(os.path.join(out, "Groups", "Family", "2024", "img_Alice.jpg"))

    # --- Re-run / deduplication ---

    def test_identical_file_skipped_on_rerun(self, tmp_path, logger):
        src_dir = self._setup_src(tmp_path, content=b"same data")
        out = str(tmp_path / "out")
        # Pre-place identical file at the expected destination
        dest_dir = os.path.join(out, "Groups", "Family", "2024")
        os.makedirs(dest_dir)
        with open(os.path.join(dest_dir, "img_Alice.jpg"), "wb") as f:
            f.write(b"same data")
        rows = [self._row(chat_subject="Family", sender="111")]
        stats, _, _, _ = wa.process_rows(
            rows,
            1,
            {"111": "Alice"},
            {},
            {},
            {},
            self._resolver(src_dir),
            out,
            logger,
            False,
            None,
            None,
            False,
            {},
        )
        assert stats["skipped"] == 1
        assert stats["copied"] == 0

    # --- Archive DB integration ---

    def test_archive_db_updated_on_new_copy(self, tmp_path, logger):
        src_dir = self._setup_src(tmp_path)
        out = str(tmp_path / "out")
        os.makedirs(out)
        rows = [self._row(chat_subject=None, sender="111", key_from_me=0, file_path="img.jpg")]
        archive_conn = arc.open_archive_db(out)
        try:
            wa.process_rows(
                rows,
                1,
                {"111": "Alice"},
                {},
                {},
                {},
                self._resolver(src_dir),
                out,
                logger,
                False,
                archive_conn,
                None,
                False,
                {},
            )
            archive_conn.commit()
            count = archive_conn.execute(
                "SELECT COUNT(*) FROM archive_copies WHERE original_path = 'img.jpg'"
            ).fetchone()[0]
            assert count == 1
        finally:
            archive_conn.close()

    def test_progress_logged_at_1000_rows(self, tmp_path):
        rows = [
            (i, 1705276800000, None, None, "42", "Group", "111", 0, None, None) for i in range(1001)
        ]
        mock_logger = MagicMock()
        info_calls = []
        mock_logger.info.side_effect = lambda msg, *a, **kw: info_calls.append(str(msg))
        wa.process_rows(
            rows,
            total=1001,
            contacts={},
            number_map={},
            folder_index={},
            group_index={},
            media_resolver=lambda fp: fp,
            output_root=str(tmp_path),
            logger=mock_logger,
            dry_run=True,
            conn=None,
            tz=None,
            link_duplicates=False,
            hash_to_canonical={},
        )
        assert any("Progress: 1000/" in m for m in info_calls)


class TestHardlinkDuplicates:
    def _create_rows(self):
        # Two messages in different chats sharing the same content
        return [
            (1, 1705276800000, "img.jpg", "image/jpeg", "1", "Family", "111", 0, None, None),
            (2, 1705276900000, "img.jpg", "image/jpeg", None, None, "222", 0, None, None),
        ]

    def _resolver(self, src_dir):
        def _resolve(fp):
            return str(src_dir / fp)

        return _resolve

    def test_hardlink_duplicates_across_chats(self, tmp_path, logger):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        content = b"duplicate binary media content"
        (src_dir / "img.jpg").write_bytes(content)

        out = tmp_path / "out"
        out.mkdir()
        conn = arc.open_archive_db(str(out))

        rows = self._create_rows()
        resolver = self._resolver(src_dir)
        hash_to_canonical = {}

        stats, _, _, missing = wa.process_rows(
            rows,
            2,
            {"111": "Alice", "222": "Bob"},
            {},
            {},
            {},
            resolver,
            str(out),
            logger,
            False,
            conn,
            None,
            True,
            hash_to_canonical,
        )
        conn.commit()

        assert stats["copied"] == 2
        assert stats["warnings"] == 0
        assert len(missing) == 0

        p1 = out / "Groups" / "Family" / "2024" / "img_Alice.jpg"
        p2 = out / "Contacts" / "Bob (00222)" / "2024" / "Received" / "img.jpg"
        assert p1.exists()
        assert p2.exists()

        # Check hardlink attributes on disk
        s1 = os.stat(str(p1))
        s2 = os.stat(str(p2))
        assert s1.st_nlink >= 2
        assert s2.st_nlink >= 2
        assert s1.st_ino == s2.st_ino
        assert s1.st_dev == s2.st_dev

        # Content modification on one reflects on the other
        with open(str(p1), "wb") as f:
            f.write(b"modified data")
        assert p2.read_bytes() == b"modified data"

        # Deleting one link preserves the other
        p1.unlink()
        assert p2.exists()
        assert p2.read_bytes() == b"modified data"

        # Check DB records
        records = conn.execute(
            "SELECT archive_path, is_hardlink FROM archive_copies ORDER BY rowid ASC"
        ).fetchall()
        assert len(records) == 2
        assert records[0][1] == 0  # original copy
        assert records[1][1] == 1  # hardlinked copy

        # Check duplicate report
        report_path = out / "duplicate_media_report.csv"
        wa.write_duplicate_report(str(report_path), conn, logger, None)
        assert report_path.exists()
        import csv

        with open(str(report_path), newline="", encoding="utf-8") as f:
            reader = [r for r in csv.DictReader(f) if r["group_id"]]
        assert len(reader) == 2
        paths_to_link = {r["archived_path"]: r["link_status"] for r in reader}
        assert paths_to_link["Groups/Family/2024/img_Alice.jpg"] == "duplicate_copy"
        assert paths_to_link["Contacts/Bob (00222)/2024/Received/img.jpg"] == "hardlink"
        paths_to_ts = {r["archived_path"]: r["original_timestamp"] for r in reader}
        assert paths_to_ts["Groups/Family/2024/img_Alice.jpg"].startswith("2024-")
        assert paths_to_ts["Contacts/Bob (00222)/2024/Received/img.jpg"].startswith("2024-")
        conn.close()

    def test_no_hardlink_when_flag_disabled(self, tmp_path, logger):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        content = b"unique separate content"
        (src_dir / "img.jpg").write_bytes(content)

        out = tmp_path / "out"
        out.mkdir()
        conn = arc.open_archive_db(str(out))

        rows = self._create_rows()
        resolver = self._resolver(src_dir)
        hash_to_canonical = {}

        stats, _, _, _ = wa.process_rows(
            rows,
            2,
            {"111": "Alice", "222": "Bob"},
            {},
            {},
            {},
            resolver,
            str(out),
            logger,
            False,
            conn,
            None,
            False,  # link_duplicates disabled
            hash_to_canonical,
        )
        conn.commit()

        p1 = out / "Groups" / "Family" / "2024" / "img_Alice.jpg"
        p2 = out / "Contacts" / "Bob (00222)" / "2024" / "Received" / "img.jpg"
        assert p1.exists()
        assert p2.exists()

        s1 = os.stat(str(p1))
        s2 = os.stat(str(p2))
        assert s1.st_nlink == 1
        assert s2.st_nlink == 1
        assert s1.st_ino != s2.st_ino

        records = conn.execute("SELECT is_hardlink FROM archive_copies").fetchall()
        assert all(r[0] == 0 for r in records)

        # Duplicate report reflects separate copies as duplicate_copy
        report_path = out / "duplicate_media_report.csv"
        wa.write_duplicate_report(str(report_path), conn, logger, None)
        import csv

        with open(str(report_path), newline="", encoding="utf-8") as f:
            reader = [r for r in csv.DictReader(f) if r["group_id"]]
        assert len(reader) == 2
        for r in reader:
            assert r["link_status"] == "duplicate_copy"
        conn.close()

    def test_duplicate_report_reflects_active_hardlinks_on_disk(self, tmp_path, logger):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        content = b"active hardlink content"
        (src_dir / "img.jpg").write_bytes(content)

        out = tmp_path / "out"
        out.mkdir()
        conn = arc.open_archive_db(str(out))

        rows = self._create_rows()
        resolver = self._resolver(src_dir)
        hash_to_canonical = {}

        wa.process_rows(
            rows,
            2,
            {"111": "Alice", "222": "Bob"},
            {},
            {},
            {},
            resolver,
            str(out),
            logger,
            False,
            conn,
            None,
            True,
            hash_to_canonical,
        )
        conn.commit()

        # Both files exist and are hardlinks on disk
        report_path = out / "duplicate_media_report.csv"
        wa.write_duplicate_report(str(report_path), conn, logger, None)
        import csv

        with open(str(report_path), newline="", encoding="utf-8") as f:
            reader = [r for r in csv.DictReader(f) if r["group_id"]]
        assert len(reader) == 2
        for r in reader:
            assert r["link_status"] == "hardlink"

        # Re-run over existing archive: skip branch preserves hardlink status
        wa.process_rows(
            rows,
            2,
            {"111": "Alice", "222": "Bob"},
            {},
            {},
            {},
            resolver,
            str(out),
            logger,
            False,
            conn,
            None,
            True,
            hash_to_canonical,
        )
        conn.commit()

        records = conn.execute(
            "SELECT archive_path, is_hardlink FROM archive_copies ORDER BY rowid ASC"
        ).fetchall()
        assert len(records) == 2
        # Hardlinked second file is not downgraded to 0 on skip
        assert records[1][1] == 1
        conn.close()

    def test_hardlink_dry_run_logs_would_link(self, tmp_path, logger):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "img.jpg").write_bytes(b"sample data")

        out = tmp_path / "out"
        out.mkdir()

        rows = self._create_rows()
        resolver = self._resolver(src_dir)
        mock_logger = MagicMock()

        stats, _, _, _ = wa.process_rows(
            rows,
            2,
            {"111": "Alice", "222": "Bob"},
            {},
            {},
            {},
            resolver,
            str(out),
            mock_logger,
            True,  # dry_run
            None,
            None,
            True,  # link_duplicates
            {},
        )
        assert stats["copied"] == 2
        calls = [str(c) for c in mock_logger.info.mock_calls]
        assert any("Would copy" in c for c in calls)
        assert any("Would link" in c for c in calls)

    def test_hardlink_incremental_run_links_to_previous_canonical(self, tmp_path, logger):
        out = tmp_path / "out"
        out.mkdir()
        conn = arc.open_archive_db(str(out))

        src_dir = tmp_path / "src"
        src_dir.mkdir()
        content = b"content across incremental runs"
        (src_dir / "img.jpg").write_bytes(content)

        resolver = self._resolver(src_dir)

        # Run 1: archives first chat
        row1 = [(1, 1705276800000, "img.jpg", "image/jpeg", "1", "Family", "111", 0, None, None)]
        wa.process_rows(
            row1,
            1,
            {"111": "Alice"},
            {},
            {},
            {},
            resolver,
            str(out),
            logger,
            False,
            conn,
            None,
            True,
            {},
        )
        conn.commit()

        # Run 2: starts fresh with canonical map loaded from existing DB
        hash_to_canonical = arc.load_canonical_copies(conn, str(out))
        assert len(hash_to_canonical) == 1

        row2 = [(2, 1705276900000, "img.jpg", "image/jpeg", None, None, "222", 0, None, None)]
        wa.process_rows(
            row2,
            1,
            {"222": "Bob"},
            {},
            {},
            {},
            resolver,
            str(out),
            logger,
            False,
            conn,
            None,
            True,
            hash_to_canonical,
        )
        conn.commit()

        p1 = out / "Groups" / "Family" / "2024" / "img_Alice.jpg"
        p2 = out / "Contacts" / "Bob (00222)" / "2024" / "Received" / "img.jpg"
        assert p1.exists()
        assert p2.exists()
        assert os.stat(str(p1)).st_ino == os.stat(str(p2)).st_ino

        records = conn.execute(
            "SELECT is_hardlink FROM archive_copies ORDER BY rowid ASC"
        ).fetchall()
        assert records == [(0,), (1,)]
        conn.close()

    def test_fallback_to_copy_when_os_link_raises(self, tmp_path, logger):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        content = b"fallback media content"
        (src_dir / "img.jpg").write_bytes(content)

        out = tmp_path / "out"
        out.mkdir()
        conn = arc.open_archive_db(str(out))

        rows = self._create_rows()
        resolver = self._resolver(src_dir)
        hash_to_canonical = {}

        # Simulate os.link failure on the duplicate
        with patch("os.link", side_effect=OSError(17, "Cross-device link")):
            stats, _, _, _ = wa.process_rows(
                rows,
                2,
                {"111": "Alice", "222": "Bob"},
                {},
                {},
                {},
                resolver,
                str(out),
                logger,
                False,
                conn,
                None,
                True,
                hash_to_canonical,
            )
            conn.commit()

        assert stats["copied"] == 2
        p1 = out / "Groups" / "Family" / "2024" / "img_Alice.jpg"
        p2 = out / "Contacts" / "Bob (00222)" / "2024" / "Received" / "img.jpg"
        assert p1.exists()
        assert p2.exists()
        # Fallback copied, so separate inodes
        assert os.stat(str(p1)).st_ino != os.stat(str(p2)).st_ino

        records = conn.execute("SELECT is_hardlink FROM archive_copies").fetchall()
        assert records == [(0,), (0,)]
        conn.close()

    def test_hardlink_fallback_copy_error_logged(self, tmp_path, logger):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "img.jpg").write_bytes(b"content")
        out = tmp_path / "out"
        out.mkdir()
        conn = arc.open_archive_db(str(out))
        rows = self._create_rows()
        resolver = self._resolver(src_dir)
        hash_to_canonical = {}

        import shutil

        orig_copy = shutil.copy

        def _fail_on_fallback(src, dst):
            if "Bob" in dst:
                raise OSError("Disk full")
            return orig_copy(src, dst)

        with (
            patch("os.link", side_effect=OSError(17, "Cross-device")),
            patch("shutil.copy", side_effect=_fail_on_fallback),
        ):
            stats, _, _, _ = wa.process_rows(
                rows,
                2,
                {"111": "Alice", "222": "Bob"},
                {},
                {},
                {},
                resolver,
                str(out),
                logger,
                False,
                conn,
                None,
                True,
                hash_to_canonical,
            )
            assert stats["warnings"] == 1
        conn.close()

    def test_hardlink_canonical_copy_error_logged(self, tmp_path, logger):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "img.jpg").write_bytes(b"content")
        out = tmp_path / "out"
        out.mkdir()
        conn = arc.open_archive_db(str(out))
        rows = self._create_rows()
        resolver = self._resolver(src_dir)
        hash_to_canonical = {}

        with patch("shutil.copy", side_effect=OSError("Copy error on canonical")):
            stats, _, _, _ = wa.process_rows(
                rows[:1],
                1,
                {"111": "Alice"},
                {},
                {},
                {},
                resolver,
                str(out),
                logger,
                False,
                conn,
                None,
                True,
                hash_to_canonical,
            )
            assert stats["warnings"] == 1
        conn.close()

    def test_hardlink_skip_branch_populates_hash_to_canonical(self, tmp_path, logger):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        content = b"preexisting content"
        (src_dir / "img.jpg").write_bytes(content)
        out = tmp_path / "out"
        out.mkdir()
        conn = arc.open_archive_db(str(out))
        rows = self._create_rows()
        resolver = self._resolver(src_dir)
        p1 = out / "Groups" / "Family" / "2024" / "img_Alice.jpg"
        p1.parent.mkdir(parents=True, exist_ok=True)
        p1.write_bytes(content)

        hash_to_canonical = {}
        stats, _, _, _ = wa.process_rows(
            rows[:1],
            1,
            {"111": "Alice"},
            {},
            {},
            {},
            resolver,
            str(out),
            logger,
            False,
            conn,
            None,
            True,
            hash_to_canonical,
        )
        assert stats["skipped"] == 1
        md5 = wa.file_md5(str(p1))
        assert md5 in hash_to_canonical
        assert hash_to_canonical[md5] == str(p1)
        conn.close()

    def test_write_duplicate_report_os_stat_os_error_handled(self, tmp_path, logger):
        out = tmp_path / "out"
        out.mkdir()
        conn = arc.open_archive_db(str(out))
        cur = conn.cursor()
        md5 = b"1234567890123456"
        p = out / "Contacts" / "Alice" / "a.jpg"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"data")
        arc.record_file_archived(cur, "orig1", md5, "Contacts/Alice/a.jpg", 4, 0, None)
        arc.record_file_archived(cur, "orig2", md5, "Contacts/Bob/b.jpg", 4, 1, None)
        conn.commit()

        report_path = out / "duplicate_media_report.csv"
        with patch("os.stat", side_effect=OSError("Permission denied on stat")):
            wa.write_duplicate_report(str(report_path), conn, logger, None)
        assert report_path.exists()
        conn.close()

    def test_load_canonical_copies_skips_missing_files_on_disk(self, tmp_path):
        conn = arc.open_archive_db(str(tmp_path))
        cur = conn.cursor()
        md5 = b"missingfile12345"
        arc.record_file_archived(cur, "orig1", md5, "Contacts/Nonexistent/a.jpg", 10, 0, None)
        conn.commit()
        canonical_map = arc.load_canonical_copies(conn, str(tmp_path))
        assert md5 not in canonical_map
        conn.close()


class TestDuplicateAndSkippedBreakdown:
    def test_copy_result_contract(self):
        res = wa.CopyResult(1, 0, 0, 1)
        assert res == (1, 0, 0)
        copied, skipped, warnings = res
        assert copied == 1
        assert skipped == 0
        assert warnings == 0
        assert res.is_hardlink == 1

    def test_process_rows_skipped_preexisting_vs_duplicate(self, tmp_path, logger):
        out = tmp_path / "out"
        out.mkdir()
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        f1 = src_dir / "pic.jpg"
        f1.write_bytes(b"content-a")
        f2 = src_dir / "pic2.jpg"
        f2.write_bytes(b"content-b")

        # Pre-place pic.jpg in output destination to simulate existing from previous run
        folder = wa.build_contact_folder_name("Alice", "111")
        year = wa.get_year(1705276800000, None)
        pre_dir = out / "Contacts" / folder / year / "Received"
        pre_dir.mkdir(parents=True)
        (pre_dir / "pic.jpg").write_bytes(b"content-a")

        # Row 1 targets existing pic.jpg -> should be skipped_preexisting
        # Row 2 targets pic2.jpg -> should be copied
        # Row 3 targets pic2.jpg again with same filename in same chat -> should be skipped_duplicate
        rows = [
            (1, 1705276800000, "Media/pic.jpg", "image/jpeg", 1, None, "111", 0, None, "pic.jpg"),
            (2, 1705276800000, "Media/pic2.jpg", "image/jpeg", 1, None, "111", 0, None, "pic2.jpg"),
            (3, 1705276800000, "Media/pic2.jpg", "image/jpeg", 1, None, "111", 0, None, "pic2.jpg"),
        ]

        def resolver(fp):
            if "pic2" in fp:
                return str(f2)
            return str(f1)

        stats, _, _, _ = wa.process_rows(
            rows,
            len(rows),
            {"111": "Alice"},
            {},
            {},
            {},
            resolver,
            str(out),
            logger,
            False,
            None,
            None,
            False,
            {},
        )

        assert stats["copied"] == 1
        assert stats["skipped"] == 2
        assert stats["skipped_preexisting"] == 1
        assert stats["skipped_duplicate"] == 1

    def test_summary_report_logging_format(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        log_records = []

        class TestHandler(logging.Handler):
            def emit(self, record):
                log_records.append(record.getMessage())

        t_logger = logging.getLogger("test_summary_report")
        t_logger.setLevel(logging.INFO)
        handler = TestHandler()
        t_logger.addHandler(handler)

        stats = {
            "copied": 10,
            "skipped": 5,
            "missing": 1,
            "warnings": 0,
            "hardlinked": 2,
            "skipped_preexisting": 4,
            "skipped_duplicate": 1,
            "skipped_empty_vcard": 0,
        }
        dup_summary = wa.DuplicateReportSummary(
            2,
            4,
            [("PHOTOS & IMAGES", 1, 2), ("VIDEOS", 1, 2)],
        )

        # Emulate the summary reporting block
        copied_label = "Copied"
        hardlinked_count = stats.get("hardlinked", 0)
        if hardlinked_count > 0:
            t_logger.info(
                f"  {copied_label:<20}: {stats['copied']} ({hardlinked_count} linked duplicates)"
            )
        else:
            t_logger.info(f"  {copied_label:<20}: {stats['copied']}")

        n_skipped = stats["skipped"]
        n_pre = stats.get("skipped_preexisting", 0)
        n_dup = stats.get("skipped_duplicate", 0)
        n_empty = stats.get("skipped_empty_vcard", 0)
        if n_skipped > 0 and (n_pre > 0 or n_dup > 0 or n_empty > 0):
            parts = []
            if n_pre > 0:
                parts.append(f"{n_pre} already archived")
            if n_dup > 0:
                parts.append(f"{n_dup} same-chat duplicates")
            if n_empty > 0:
                parts.append(f"{n_empty} empty vCards")
            t_logger.info(f"  {'Skipped':<20}: {n_skipped} ({', '.join(parts)})")
        else:
            t_logger.info(f"  {'Skipped':<20}: {n_skipped}")

        t_logger.info(f"  {'Errors / Warnings':<20}: {stats['warnings']}")

        t_logger.info("")
        t_logger.info(f"  {'Missing source files':<20}: {stats['missing']}")

        if dup_summary[1] > 0:
            t_logger.info(
                f"  {'Duplicate media':<20}: {dup_summary[1]} file(s) across {dup_summary[0]} group(s) "
                "→ see duplicate_media_report.csv"
            )
            for sec_title, sec_groups, sec_files in getattr(dup_summary, "sections", []):
                t_logger.info(f"    - {sec_title:<18}: {sec_groups} group(s), {sec_files} file(s)")

        t_logger.removeHandler(handler)

        output_text = "\n".join(log_records)
        assert "Copied              : 10 (2 linked duplicates)" in output_text
        assert "Skipped             : 5 (4 already archived, 1 same-chat duplicates)" in output_text
        assert "Errors / Warnings   : 0" in output_text
        assert "Missing source files: 1" in output_text
        assert (
            "Duplicate media     : 4 file(s) across 2 group(s) → see duplicate_media_report.csv"
            in output_text
        )
        assert "- PHOTOS & IMAGES   : 1 group(s), 2 file(s)" in output_text
        assert "- VIDEOS            : 1 group(s), 2 file(s)" in output_text

    def test_process_vcard_rows_and_merge_stats(self, tmp_path, logger):
        out = tmp_path / "out"
        out.mkdir()

        # Simulate pre-existing vcard in archive
        vcard_raw = "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Bob\r\nEND:VCARD"
        vcf_content = vcard_raw.strip().encode("utf-8") + b"\r\n"
        folder = wa.build_contact_folder_name("Bob", "222")
        year = wa.get_year(1705276800000, None)
        pre_dir = out / "Contacts" / folder / year / "Received"
        pre_dir.mkdir(parents=True)
        (pre_dir / "Bob.vcf").write_bytes(vcf_content)

        vcard_rows = [
            # 1: empty vcard -> skipped_empty_vcard
            {
                "message_id": 1,
                "timestamp_ms": 1705276800000,
                "chat_row_id": 1,
                "chat_subject": None,
                "sender": "222",
                "key_from_me": 0,
                "contact_name": "Blank",
                "vcard_text": "",
            },
            # 2: matches pre-existing Bob.vcf -> skipped_preexisting
            {
                "message_id": 2,
                "timestamp_ms": 1705276800000,
                "chat_row_id": 1,
                "chat_subject": None,
                "sender": "222",
                "key_from_me": 0,
                "contact_name": "Bob",
                "vcard_text": vcard_raw,
            },
            # 3: also matches Bob.vcf -> skipped_duplicate
            {
                "message_id": 3,
                "timestamp_ms": 1705276800000,
                "chat_row_id": 1,
                "chat_subject": None,
                "sender": "222",
                "key_from_me": 0,
                "contact_name": "Bob",
                "vcard_text": vcard_raw,
            },
        ]

        # Base stats from media processing (ensure process_rows stats dictionary shape)
        base_stats, _, _, _ = wa.process_rows(
            [], 0, {}, {}, {}, {}, lambda x: x, str(out), logger, False, None, None, False, {}
        )

        v_stats, _, _ = wa.process_vcard_rows(
            vcard_rows,
            {"222": "Bob"},
            {},
            {},
            {},
            str(out),
            logger,
            False,
            None,
            None,
        )

        assert v_stats["skipped"] == 3
        assert v_stats["skipped_empty_vcard"] == 1
        assert v_stats["skipped_preexisting"] == 1
        assert v_stats["skipped_duplicate"] == 1

        # Verify merge into base_stats does not raise KeyError
        base_stats["copied"] = base_stats.get("copied", 0) + v_stats.get("copied", 0)
        base_stats["skipped"] = base_stats.get("skipped", 0) + v_stats.get("skipped", 0)
        base_stats["warnings"] = base_stats.get("warnings", 0) + v_stats.get("warnings", 0)
        base_stats["hardlinked"] = base_stats.get("hardlinked", 0) + v_stats.get("hardlinked", 0)
        base_stats["skipped_preexisting"] = base_stats.get("skipped_preexisting", 0) + v_stats.get(
            "skipped_preexisting", 0
        )
        base_stats["skipped_duplicate"] = base_stats.get("skipped_duplicate", 0) + v_stats.get(
            "skipped_duplicate", 0
        )
        base_stats["skipped_empty_vcard"] = base_stats.get("skipped_empty_vcard", 0) + v_stats.get(
            "skipped_empty_vcard", 0
        )

        assert base_stats["skipped"] == 3
        assert base_stats["skipped_empty_vcard"] == 1
        assert base_stats["skipped_preexisting"] == 1
        assert base_stats["skipped_duplicate"] == 1
