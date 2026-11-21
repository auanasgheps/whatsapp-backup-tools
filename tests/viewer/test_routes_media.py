"""Tests for wab_viewer."""

from __future__ import annotations

import os

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

import os
import sqlite3
from pathlib import Path

import pytest
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
    seed_android_db_with_media,
)
from wab_viewer import hydration as viewer_hydration
from wab_viewer import main as viewer
from wab_viewer import preserved as viewer_preserved

# Protobuf wire builder aliases for compatibility
_encode_varint = encode_varint
_lid_field1_bytes = lid_field1_bytes
_make_receipt_blob = make_receipt_blob
_make_compact_receipt_blob = make_compact_receipt_blob
_make_zreceipt_blob_with_reactions = make_zreceipt_blob_with_reactions
_make_ios_reaction_blob = make_ios_reaction_blob


class TestMediaTypeFromPath:
    def test_image_jpg(self):
        assert viewer_hydration._media_type_from_path("foo/bar.jpg") == "image"

    def test_image_png(self):
        assert viewer_hydration._media_type_from_path("foo/IMG.png") == "image"

    def test_video_mp4(self):
        assert viewer_hydration._media_type_from_path("foo/video.mp4") == "video"

    def test_audio_mp3(self):
        assert viewer_hydration._media_type_from_path("foo/voice.mp3") == "audio"

    def test_gif(self):
        assert viewer_hydration._media_type_from_path("foo/anim.gif") == "gif"
        assert (
            viewer_hydration._media_type_from_path(
                "Media/WhatsApp Animated Gifs/VID-20230101-WA0001.mp4"
            )
            == "gif"
        )
        assert viewer_hydration._media_type_from_path("WhatsApp Animated Gifs/GIF-123.mp4") == "gif"
        assert viewer_hydration._media_type_from_path("foo/GIF-123.mp4") == "gif"

    def test_sticker(self):
        assert viewer_hydration._media_type_from_path("foo/Sticker/abc.webp") == "sticker"
        assert viewer_hydration._media_type_from_path("foo/stickers/abc.webp") == "sticker"
        assert (
            viewer_hydration._media_type_from_path("Groups/Chat/2026/STK-20260101-WA0001.webp")
            == "sticker"
        )
        assert (
            viewer_hydration._media_type_from_path("Groups/Chat/2026/STK-20260101-WA0001.was")
            == "sticker"
        )

    def test_document(self):
        assert viewer_hydration._media_type_from_path("foo/document.pdf") == "document"

    def test_none_returns_text(self):
        assert viewer_hydration._media_type_from_path(None) == "text"

    def test_empty_string_returns_document(self):
        # No extension → falls through to "document"
        assert viewer_hydration._media_type_from_path("") == "document"

    def test_no_extension_returns_document(self):
        # No recognized extension → falls through to "document"
        assert viewer_hydration._media_type_from_path("foo/bar") == "document"

    def test_uppercase_extension_normalized(self):
        assert viewer_hydration._media_type_from_path("foo/bar.MP4") == "video"

    def test_path_with_multiple_dots(self):
        # Last extension wins
        assert viewer_hydration._media_type_from_path("foo/file.name.tar.gz") == "document"
        assert viewer_hydration._media_type_from_path("foo/file.name.jpg") == "image"

    def test_mixed_case_extension(self):
        assert viewer_hydration._media_type_from_path("photo.JpG") == "image"


class TestApiMedia:
    @pytest.fixture
    def app_with_media(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        seed_android_db_with_media(wa_conn, archive_conn, tmp_path)
        wa_conn.close()
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client, tmp_path

    def test_returns_only_archived_media(self, app_with_media):
        """Text message excluded; image media row returned (archive_path set)."""
        client, _ = app_with_media
        resp = client.get("/api/media?chat_id=123456789&chat_type=contact")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["media_type"] == "image"

    def test_returns_correct_fields(self, app_with_media):
        """Response includes archive_path, media_type, timestamp_ms."""
        client, _ = app_with_media
        data = client.get("/api/media?chat_id=123456789&chat_type=contact").get_json()
        item = data[0]
        assert "archive_path" in item
        assert "timestamp_ms" in item
        assert item["timestamp_ms"] == 1700000001000

    def test_includes_unarchived_media_with_missing_flag(self, tmp_path):
        """/api/media returns missing items with is_missing=1 and archive_path=None."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, message_type) "
            "VALUES (3, 10, 0, 1700000002000, 3)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (3, 'Media/Images/missing.jpg', 'missing.jpg')"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/media?chat_id=123456789&chat_type=contact").get_json()
        assert len(data) == 1
        assert data[0]["is_missing"] == 1
        assert data[0]["archive_path"] is None

    def test_archived_param_filters_only_available_media(self, tmp_path):
        """/api/media?archived=1 strictly excludes unarchived/missing media."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        seed_android_db_with_media(wa_conn, archive_conn, tmp_path)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, message_type) "
            "VALUES (99, 10, 0, 1700000009999, 1)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (99, 'Media/Images/unarchived_missing.jpg', 'unarchived_missing.jpg')"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            all_media = client.get("/api/media?chat_id=123456789&chat_type=contact").get_json()
            archived_only = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&archived=1"
            ).get_json()

        assert len(all_media) == 2
        assert len(archived_only) == 1
        assert archived_only[0]["is_missing"] == 0
        assert archived_only[0]["archive_path"] is not None

    def test_archive_view_scripts_contract(self):
        """Verify archive view JS requests archived=1, avoids grid injection, and prevents busy-loops."""
        from wab_viewer.chat_viewer import builder

        bundle = builder.get_bundle_content(debug=True)
        assert "archived=1" in bundle
        assert "currentArchiveItems" in bundle
        assert "!archiveViewActive" in bundle

    def test_ordered_newest_first(self, app_with_media):
        """Results are sorted descending by timestamp."""
        client, _ = app_with_media
        data = client.get("/api/media?chat_id=123456789&chat_type=contact").get_json()
        timestamps = [r["timestamp_ms"] for r in data]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_pagination_first_page_limited(self, tmp_path):
        """First page respects GALLERY_PAGE_SIZE — seed 101 media messages, expect 100 returned."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)
        seed_android_db(wa_conn, archive_conn)

        for i in range(101):
            msg_id = 100 + i
            orig = f"Media/Images/photo{i}.jpg"
            arch = f"Alice (00123456789)/photo{i}.jpg"
            wa_conn.execute(
                "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
                f"VALUES ({msg_id}, 10, 1, {1700000010000 + i * 1000}, NULL, 3)"
            )
            wa_conn.execute(
                f"INSERT INTO message_media (message_row_id, file_path, media_name) VALUES ({msg_id}, '{orig}', 'p.jpg')"
            )
            archive_conn.execute(
                f"INSERT INTO files (original_path, md5) VALUES ('{orig}', x'deadbeef')"
            )
            archive_conn.execute(
                f"INSERT INTO archive_copies (original_path, archive_path) VALUES ('{orig}', '{arch}')"
            )
        wa_conn.commit()
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/media?chat_id=123456789&chat_type=contact").get_json()
        assert len(data) == 100

    def test_pagination_before_cursor(self, tmp_path):
        """before cursor returns the next page, not overlapping with the first."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)
        seed_android_db(wa_conn, archive_conn)

        for i in range(101):
            msg_id = 100 + i
            orig = f"Media/Images/photo{i}.jpg"
            arch = f"Alice (00123456789)/photo{i}.jpg"
            wa_conn.execute(
                "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
                f"VALUES ({msg_id}, 10, 1, {1700000010000 + i * 1000}, NULL, 3)"
            )
            wa_conn.execute(
                f"INSERT INTO message_media (message_row_id, file_path, media_name) VALUES ({msg_id}, '{orig}', 'p.jpg')"
            )
            archive_conn.execute(
                f"INSERT INTO files (original_path, md5) VALUES ('{orig}', x'deadbeef')"
            )
            archive_conn.execute(
                f"INSERT INTO archive_copies (original_path, archive_path) VALUES ('{orig}', '{arch}')"
            )
        wa_conn.commit()
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            page1 = client.get("/api/media?chat_id=123456789&chat_type=contact").get_json()
            oldest_ts = page1[-1]["timestamp_ms"]
            page2 = client.get(
                f"/api/media?chat_id=123456789&chat_type=contact&before={oldest_ts}"
            ).get_json()

        assert len(page1) == 100
        assert len(page2) == 1  # the 101st item
        # no overlap
        page1_ts = {r["timestamp_ms"] for r in page1}
        assert all(r["timestamp_ms"] not in page1_ts for r in page2)

    def test_pagination_with_live_photo_companion_videos_tops_up_to_full_page(self, tmp_path):
        """When live photo companion videos are excluded from a page, /api/media tops up to 100 items."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)
        seed_android_db(wa_conn, archive_conn)

        base_ts = 1700000000000
        for i in range(150):
            msg_id = 1000 + i
            ts = base_ts + i * 10000
            orig = f"Media/Images/photo{i}.jpg"
            arch = f"Alice (00123456789)/photo{i}.jpg"
            wa_conn.execute(
                "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
                f"VALUES ({msg_id}, 10, 1, {ts}, NULL, 1)"
            )
            wa_conn.execute(
                f"INSERT INTO message_media (message_row_id, file_path, media_name) VALUES ({msg_id}, '{orig}', 'p{i}.jpg')"
            )
            archive_conn.execute(
                f"INSERT INTO files (original_path, md5) VALUES ('{orig}', x'deadbeef')"
            )
            archive_conn.execute(
                f"INSERT INTO archive_copies (original_path, archive_path) VALUES ('{orig}', '{arch}')"
            )

        # Make item 140 a companion video for item 139 via message_association type 11
        wa_conn.execute(
            "CREATE TABLE IF NOT EXISTS message_association (_id INTEGER PRIMARY KEY, parent_message_row_id INTEGER, child_message_row_id INTEGER, association_type INTEGER)"
        )
        wa_conn.execute(
            "INSERT INTO message_association (parent_message_row_id, child_message_row_id, association_type) VALUES (1139, 1140, 11)"
        )
        wa_conn.execute("UPDATE message SET message_type = 3 WHERE _id = 1140")

        wa_conn.commit()
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            page1 = client.get("/api/media?chat_id=123456789&chat_type=contact").get_json()
            # Despite child video 1140 being excluded, page1 must top up to 100 items from the 150 available
            assert len(page1) == 100
            assert all(r["msg_id"] != 1140 for r in page1)
            oldest_ts = page1[-1]["timestamp_ms"]
            page2 = client.get(
                f"/api/media?chat_id=123456789&chat_type=contact&before={oldest_ts}"
            ).get_json()
            assert len(page2) == 49
            assert len(page1) + len(page2) == 149

    def test_media_count_returns_totals(self, app_with_media):
        """Count endpoint returns total and per-type breakdown."""
        client, _ = app_with_media
        data = client.get("/api/media/count?chat_id=123456789&chat_type=contact").get_json()
        assert data["total"] == 1
        assert data["archived"] == 1
        assert "image" in data["by_type"]
        assert data["by_type"]["image"]["count"] == 1
        assert data["by_type"]["image"]["missing"] == 0

    def test_media_count_missing(self, tmp_path):
        """Count endpoint counts unarchived items as missing."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)
        seed_android_db(wa_conn, archive_conn)
        # media message with no archive_copies entry
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (5, 10, 1, 1700000005000, NULL, 1)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (5, 'Media/Images/lost.jpg', 'lost.jpg')"
        )
        archive_conn.execute(
            "INSERT INTO files (original_path, md5) VALUES ('Media/Images/lost.jpg', x'deadbeef')"
        )
        # deliberately no archive_copies row
        wa_conn.commit()
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/media/count?chat_id=123456789&chat_type=contact").get_json()
        assert data["total"] == 1
        assert data["archived"] == 0
        assert data["by_type"]["image"]["missing"] == 1

    def test_media_count_includes_links(self, tmp_path):
        """/api/media/count includes link-text messages and merges them into total."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)
        seed_android_db(wa_conn, archive_conn)
        seed_android_db_with_media(wa_conn, archive_conn, tmp_path)
        # add a link message (type=0, text contains https URL)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (50, 10, 0, 1700000050000, 'check this out https://example.com/page', NULL)"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/media/count?chat_id=123456789&chat_type=contact").get_json()
        assert data["total"] == 2  # 1 image + 1 link
        assert data["archived"] == 1  # only the image is archived
        assert data["by_type"]["link"]["count"] == 1
        assert data["by_type"]["link"]["missing"] == 0  # links have no archive concept
        assert data["by_type"]["image"]["count"] == 1

    def test_ios_media_count_includes_links_without_missing(self, tmp_path):
        """/api/media/count on iOS includes ZMESSAGETYPE=7 links without marking them missing or double-counting."""
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
            -- 1. Archived photo (ZMESSAGETYPE=1)
            INSERT INTO ZWAMEDIAITEM VALUES (20, 'Media/photo.jpg', NULL);
            INSERT INTO ZWAMESSAGE VALUES (100, 10, 0, 1000.0, 'MSG_1', 1, 20, NULL, NULL, 'User1', NULL, '15550001111@s.whatsapp.net');

            -- 2. Link with preview (ZMESSAGETYPE=7)
            INSERT INTO ZWAMEDIAITEM VALUES (21, NULL, 'Example Title');
            INSERT INTO ZWAMESSAGE VALUES (101, 10, 0, 1001.0, 'MSG_2', 7, 21, NULL, NULL, 'User1', 'https://example.com/preview', '15550001111@s.whatsapp.net');

            -- 3. Plain text link (ZMESSAGETYPE=0)
            INSERT INTO ZWAMESSAGE VALUES (102, 10, 0, 1002.0, 'MSG_3', 0, NULL, NULL, NULL, 'User1', 'Check out https://another.com', '15550001111@s.whatsapp.net');
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) "
            "VALUES ('Message/Media/photo.jpg', 'photos/photo.jpg')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/media/count?chat_id=10&chat_type=group")
            data = resp.get_json()

        assert data["total"] == 3  # 1 image + 2 links
        assert data["archived"] == 1  # only image is archived
        assert data["by_type"]["image"]["count"] == 1
        assert data["by_type"]["image"]["missing"] == 0
        assert data["by_type"]["link"]["count"] == 2
        assert data["by_type"]["link"]["missing"] == 0  # links cannot be missing

    def test_ios_api_media_all_types_with_mdi_table(self, tmp_path):
        """/api/media with types=all on iOS with ZWAMESSAGEDATAITEM table executes stage1_sql without column error."""
        wa_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZTITLE TEXT);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
            CREATE TABLE ZWAMESSAGEDATAITEM (Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZINDEX INTEGER, ZMATCHEDTEXT TEXT, ZTITLE TEXT, ZSUMMARY TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
                ZMESSAGEDATE REAL, ZSTANZAID TEXT, ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER, ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT, ZTEXT TEXT, ZFROMJID TEXT
            );

            INSERT INTO ZWACHATSESSION VALUES (10, 1, 'group1@g.us', 'Test Group');
            INSERT INTO ZWAMEDIAITEM VALUES (20, 'Media/photo.jpg', NULL);
            INSERT INTO ZWAMESSAGE VALUES (100, 10, 0, 1000.0, 'MSG_1', 1, 20, NULL, NULL, 'User1', NULL, '15550001111@s.whatsapp.net');
            INSERT INTO ZWAMESSAGEDATAITEM VALUES (1, 100, 0, 'https://example.com', 'Title', 'Summary');
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) "
            "VALUES ('Message/Media/photo.jpg', 'photos/photo.jpg')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/media?chat_id=10&chat_type=group&types=all")
            assert resp.status_code == 200
            data = resp.get_json()
            assert len(data) >= 1

    def test_media_links_endpoint(self, tmp_path):
        """/api/media/links returns link rows; /api/media never returns them."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)
        seed_android_db(wa_conn, archive_conn)
        seed_android_db_with_media(wa_conn, archive_conn, tmp_path)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (51, 10, 1, 1700000051000, 'see http://example.org/thing', NULL)"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            links = client.get("/api/media/links?chat_id=123456789&chat_type=contact").get_json()
            media = client.get("/api/media?chat_id=123456789&chat_type=contact").get_json()
        # links endpoint returns the link row
        assert len(links) == 1
        assert links[0]["media_type"] == "link"
        assert "http://example.org/thing" in links[0]["text_body"]
        # /api/media must NOT contain any link rows — regression guard
        assert all(r["media_type"] != "link" for r in media)
        # /api/media still returns the image
        assert any(r["media_type"] == "image" for r in media)

    def test_media_types_parameter_filtering(self, tmp_path):
        """/api/media supports types parameter for server-side filtering and combined streams."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)
        seed_android_db(wa_conn, archive_conn)
        seed_android_db_with_media(wa_conn, archive_conn, tmp_path)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (51, 10, 1, 1700000051000, 'see http://example.org/thing', NULL)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (52, 10, 0, 1, 1700000052000, NULL, 6)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (52, NULL, 'contract.pdf')"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            links = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=link"
            ).get_json()
            assert len(links) == 1
            assert links[0]["media_type"] == "link"

            images = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=image"
            ).get_json()
            assert len(images) == 1
            assert images[0]["media_type"] == "image"

            mixed = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=image,link"
            ).get_json()
            assert len(mixed) == 2
            assert [r["media_type"] for r in mixed] == ["link", "image"]

            all_items = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=all"
            ).get_json()
            assert len(all_items) == 3
            assert {r["media_type"] for r in all_items} == {"image", "link", "document"}

            paged = client.get(
                f"/api/media?chat_id=123456789&chat_type=contact&types=all&before={mixed[0]['timestamp_ms']}"
            ).get_json()
            assert all(r["timestamp_ms"] < mixed[0]["timestamp_ms"] for r in paged)

    def test_documents_do_not_leak_into_video_or_image_filter(self, tmp_path):
        """Documents (message_type 9) even with video/image extensions must never leak into video or image filters."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)
        seed_android_db(wa_conn, archive_conn)

        # 1. Real video (message_type = 3)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (101, 10, 1, 1700000001000, NULL, 3)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (101, 'Media/Videos/real_video.mp4', 'real_video.mp4')"
        )
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Media/Videos/real_video.mp4', 'Contacts/Alice/2026/real_video.mp4')"
        )

        # 2. Document with .mp4 extension (uncompressed video sent as document, message_type = 9)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (102, 10, 1, 1700000002000, NULL, 9)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (102, 'Media/Documents/attached_video.mp4', 'attached_video.mp4')"
        )
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Media/Documents/attached_video.mp4', 'Contacts/Alice/2026/attached_video.mp4')"
        )

        # 3. Standard PDF document (message_type = 9)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (103, 10, 1, 1700000003000, NULL, 9)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (103, 'Media/Documents/paper.pdf', 'paper.pdf')"
        )
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Media/Documents/paper.pdf', 'Contacts/Alice/2026/paper.pdf')"
        )

        wa_conn.commit()
        wa_conn.close()
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            # Video filter must strictly contain only real_video.mp4, not attached_video.mp4
            videos = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=video"
            ).get_json()
            assert len(videos) == 1
            assert videos[0]["msg_id"] == 101
            assert videos[0]["media_type"] == "video"

            # Image filter must contain 0 items
            images = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=image"
            ).get_json()
            assert len(images) == 0

            # Document filter must return both documents
            docs = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=document"
            ).get_json()
            assert len(docs) == 2
            doc_ids = {d["msg_id"] for d in docs}
            assert doc_ids == {102, 103}
            assert all(d["media_type"] == "document" for d in docs)

            # Counts must reflect 1 video and 2 documents
            counts = client.get("/api/media/count?chat_id=123456789&chat_type=contact").get_json()
            assert counts["by_type"]["video"]["count"] == 1
            assert counts["by_type"]["document"]["count"] == 2

    def test_image_filter_strictly_excludes_stickers_and_videos(self, tmp_path):
        """Gallery: types=image strictly returns only images, excluding webp stickers, videos, and vcards."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)
        seed_android_db(wa_conn, archive_conn)

        # 1. Real photo (message_type = 1)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (201, 10, 1, 1700000001000, NULL, 1)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (201, 'Media/Images/photo.jpg', 'photo.jpg')"
        )
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Media/Images/photo.jpg', 'Contacts/Alice/2026/photo.jpg')"
        )

        # 2. WebP sticker with STK- prefix (message_type = 20)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (202, 10, 1, 1700000002000, NULL, 20)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (202, 'Media/Stickers/STK-2026.webp', 'STK-2026.webp')"
        )
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Media/Stickers/STK-2026.webp', 'Contacts/Alice/2026/STK-2026.webp')"
        )

        # 3. WebP sticker without STK- prefix (hash filename, message_type = 20)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (203, 10, 1, 1700000003000, NULL, 20)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (203, 'Media/Stickers/a1b2c3d4e5f6.webp', 'a1b2c3d4e5f6.webp')"
        )
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Media/Stickers/a1b2c3d4e5f6.webp', 'Contacts/Alice/2026/a1b2c3d4e5f6.webp')"
        )

        # 4. Video (message_type = 3)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (204, 10, 1, 1700000004000, NULL, 3)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (204, 'Media/Videos/video.mp4', 'video.mp4')"
        )
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Media/Videos/video.mp4', 'Contacts/Alice/2026/video.mp4')"
        )

        # 5. vCard contact (message_type = 4)
        wa_conn.execute(
            "CREATE TABLE IF NOT EXISTS message_vcard (message_row_id INTEGER PRIMARY KEY, vcard TEXT)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (205, 10, 1, 1700000005000, 'John Doe', 4)"
        )
        wa_conn.execute(
            "INSERT INTO message_vcard (message_row_id, vcard) VALUES (205, 'BEGIN:VCARD\\r\\nFN:John Doe\\r\\nEND:VCARD\\r\\n')"
        )

        wa_conn.commit()
        wa_conn.close()
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            images = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=image"
            ).get_json()
            assert len(images) == 1
            assert images[0]["msg_id"] == 201
            assert images[0]["media_type"] == "image"

            stickers = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=sticker"
            ).get_json()
            assert len(stickers) == 2
            stk_ids = {s["msg_id"] for s in stickers}
            assert stk_ids == {202, 203}
            assert all(s["media_type"] == "sticker" for s in stickers)

            videos = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=video"
            ).get_json()
            assert len(videos) == 1
            assert videos[0]["msg_id"] == 204
            assert videos[0]["media_type"] == "video"

            counts = client.get("/api/media/count?chat_id=123456789&chat_type=contact").get_json()
            assert counts["by_type"]["image"]["count"] == 1
            assert counts["by_type"]["sticker"]["count"] == 2
            assert counts["by_type"]["video"]["count"] == 1
            assert counts["by_type"]["vcard"]["count"] == 1
            assert counts["by_type"]["vcard"]["missing"] == 0

    def test_android_select_honors_message_type_over_filename(self, tmp_path):
        """Android SQL query prioritizes message_type enum over unusual file extensions."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)
        seed_android_db(wa_conn, archive_conn)

        # Image message_type=1 with unusual extension .bin
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (301, 10, 1, 1700000001000, NULL, 1)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (301, 'Media/Images/photo.bin', 'photo.bin')"
        )
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Media/Images/photo.bin', 'Contacts/Alice/2026/photo.bin')"
        )

        # Audio message_type=2 with deceptive .jpg name
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (302, 10, 1, 1700000002000, NULL, 2)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (302, 'Media/Audio/recording.jpg', 'recording.jpg')"
        )
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Media/Audio/recording.jpg', 'Contacts/Alice/2026/recording.jpg')"
        )

        wa_conn.commit()
        wa_conn.close()
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            images = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=image"
            ).get_json()
            assert len(images) == 1
            assert images[0]["msg_id"] == 301
            assert images[0]["media_type"] == "image"

            audios = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=audio"
            ).get_json()
            assert len(audios) == 1
            assert audios[0]["msg_id"] == 302
            assert audios[0]["media_type"] == "audio"

    def test_media_documents_endpoint_returns_undownloaded_docs(self, tmp_path):
        """Documents with no file_path but a media_name are returned by /api/media/documents."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        # Undownloaded document: message_type=6 (document), file_path=NULL, media_name set
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (5, 10, 0, 1, 1700000005000, NULL, 6)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (5, NULL, 'Annual Report 2024.pdf')"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            docs = client.get("/api/media/documents?chat_id=123456789&chat_type=contact").get_json()
        assert len(docs) == 1
        assert docs[0]["media_name"] == "Annual Report 2024.pdf"

    def test_media_documents_excludes_downloaded_media(self, tmp_path):
        """Regular downloaded media (file_path set) must not appear in /api/media/documents."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        seed_android_db_with_media(wa_conn, archive_conn, tmp_path)
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            docs = client.get("/api/media/documents?chat_id=123456789&chat_type=contact").get_json()
        assert len(docs) == 0  # the seeded media has file_path set

    def test_media_documents_excludes_text_messages(self, tmp_path):
        """Text messages (message_type=0) must not appear in /api/media/documents."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)  # message 1 is type=0 text
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            docs = client.get("/api/media/documents?chat_id=123456789&chat_type=contact").get_json()
        assert len(docs) == 0

    def test_media_documents_excludes_undownloaded_images_and_view_once(self, tmp_path):
        """Undownloaded images (type 1) and view-once media (type 42) must not appear in /api/media/documents."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, message_type) "
            "VALUES (10, 10, 0, 1700000010000, 1), (11, 10, 0, 1700000011000, 42)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (10, NULL, 'photo.jpg'), (11, NULL, 'vo_photo.jpg')"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            docs = client.get("/api/media/documents?chat_id=123456789&chat_type=contact").get_json()
        assert len(docs) == 0

    def test_media_count_and_gallery_exclude_locations_android(self, tmp_path):
        """Static locations (type 5) and live locations (type 16) must be excluded from gallery and counts on Android."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)
        seed_android_db(wa_conn, archive_conn)
        seed_android_db_with_media(wa_conn, archive_conn, tmp_path)

        # Static location (type 5) and Live location (type 16)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (51, 10, 0, 1700000051000, 'Pinned Location', 5)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (52, 10, 0, 1700000052000, 'Live Location Sharing', 16)"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            counts = client.get("/api/media/count?chat_id=123456789&chat_type=contact").get_json()
            default_media = client.get("/api/media?chat_id=123456789&chat_type=contact").get_json()
            all_media = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=all"
            ).get_json()
            loc_media = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=location"
            ).get_json()

        # Count must only reflect the 1 archived image, not locations
        assert counts["total"] == 1
        assert "location" not in counts["by_type"]
        assert "live_location" not in counts["by_type"]
        assert counts["by_type"]["image"]["missing"] == 0

        # Gallery streams must exclude locations
        assert len(default_media) == 1
        assert default_media[0]["media_type"] == "image"
        assert len(all_media) == 1
        assert all_media[0]["media_type"] == "image"
        assert len(loc_media) == 0

    def test_media_count_and_gallery_exclude_locations_ios(self, tmp_path):
        """Static locations (type 5) and live locations (type 16) must be excluded from gallery and counts on iOS."""
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
            -- Archived photo (ZMESSAGETYPE=1)
            INSERT INTO ZWAMEDIAITEM VALUES (20, 'Media/photo.jpg', NULL);
            INSERT INTO ZWAMESSAGE VALUES (100, 10, 0, 1000.0, 'MSG_1', 1, 20, NULL, NULL, 'User1', NULL, '15550001111@s.whatsapp.net');
            -- Static location (ZMESSAGETYPE=5)
            INSERT INTO ZWAMESSAGE VALUES (101, 10, 0, 1001.0, 'MSG_2', 5, NULL, NULL, NULL, 'User1', 'Static Location', '15550001111@s.whatsapp.net');
            -- Live location (ZMESSAGETYPE=16)
            INSERT INTO ZWAMESSAGE VALUES (102, 10, 0, 1002.0, 'MSG_3', 16, NULL, NULL, NULL, 'User1', 'Live Location', '15550001111@s.whatsapp.net');
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) "
            "VALUES ('Message/Media/photo.jpg', 'photos/photo.jpg')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            counts = client.get("/api/media/count?chat_id=10&chat_type=group").get_json()
            default_media = client.get("/api/media?chat_id=10&chat_type=group").get_json()
            all_media = client.get("/api/media?chat_id=10&chat_type=group&types=all").get_json()

        assert counts["total"] == 1
        assert "location" not in counts["by_type"]
        assert "live_location" not in counts["by_type"]
        assert counts["by_type"]["image"]["missing"] == 0
        assert len(default_media) == 1
        assert default_media[0]["media_type"] == "image"
        assert len(all_media) == 1
        assert all_media[0]["media_type"] == "image"

    def test_media_vcard_is_not_marked_missing_when_unarchived(self, tmp_path):
        """Contact cards (vcard) are never marked missing even if unarchived on disk."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)
        seed_android_db(wa_conn, archive_conn)

        # Contact card (type 4) with text_data (vCard string) and no archive copy
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (53, 10, 0, 1700000053000, 'BEGIN:VCARD\\nFN:John Doe\\nEND:VCARD', 4)"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            counts = client.get("/api/media/count?chat_id=123456789&chat_type=contact").get_json()
            vcards = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=vcard"
            ).get_json()

        assert counts["total"] == 1
        assert counts["by_type"]["vcard"]["count"] == 1
        assert counts["by_type"]["vcard"]["missing"] == 0
        assert len(vcards) == 1
        assert vcards[0]["media_type"] == "vcard"
        assert vcards[0]["is_missing"] == 0

    def test_media_count_does_not_double_count_undownloaded_documents(self, tmp_path):
        """Undownloaded documents must be counted once in /api/media/count and returned by /api/media/documents."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)
        seed_android_db(wa_conn, archive_conn)

        # Undownloaded document (type 9) with file_path=NULL and media_name set
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (54, 10, 0, 1700000054000, NULL, 9)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (54, NULL, 'spec.pdf')"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            counts = client.get("/api/media/count?chat_id=123456789&chat_type=contact").get_json()
            docs = client.get("/api/media/documents?chat_id=123456789&chat_type=contact").get_json()

        assert counts["total"] == 1
        assert counts["by_type"]["document"]["count"] == 1
        assert counts["by_type"]["document"]["missing"] == 1
        assert len(docs) == 1
        assert docs[0]["media_name"] == "spec.pdf"

    def test_media_count_excludes_live_photo_companion_video(self, tmp_path):
        """/api/media/count must exclude motion photo / live photo companion videos from video counts."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)

        # Insert parent photo (type 1) and child video (type 3) associated via message_association type 11
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, message_type) VALUES (101, 10, 0, 1700000001000, 1)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) VALUES (101, 'Media/Images/parent.jpg', 'parent.jpg')"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, message_type) VALUES (102, 10, 0, 1700000001000, 3)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) VALUES (102, 'Media/Video/child.mp4', 'child.mp4')"
        )
        wa_conn.execute(
            "CREATE TABLE IF NOT EXISTS message_association (_id INTEGER PRIMARY KEY, parent_message_row_id INTEGER, child_message_row_id INTEGER, association_type INTEGER)"
        )
        wa_conn.execute(
            "INSERT INTO message_association (parent_message_row_id, child_message_row_id, association_type) VALUES (101, 102, 11)"
        )
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Media/Images/parent.jpg', 'Contacts/123456789/2023/Received/parent.jpg')"
        )
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Media/Video/child.mp4', 'Contacts/123456789/2023/Received/child.mp4')"
        )
        wa_conn.commit()
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            counts = client.get("/api/media/count?chat_id=123456789&chat_type=contact").get_json()
            media = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=video"
            ).get_json()

        # Child video must NOT be counted as a standalone video
        assert counts["by_type"].get("video", {}).get("count", 0) == 0
        assert counts["by_type"]["image"]["count"] == 1
        assert len(media) == 0  # companion video excluded from /api/media

    def test_media_endpoint_includes_preserved_media(self, tmp_path):
        """/api/media and /api/media/count include preserved media from preserved_messages.db."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        wa_conn.close()
        archive_conn.close()

        pres_db = tmp_path / "preserved_messages.db"
        with sqlite3.connect(str(pres_db)) as pres_conn:
            pres_conn.executescript(
                """
                CREATE TABLE preserved_messages (
                    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                    msg_id INTEGER,
                    stanza_id TEXT,
                    chat_id TEXT,
                    chat_type TEXT,
                    timestamp_ms INTEGER,
                    sender TEXT,
                    from_me INTEGER,
                    media_type TEXT,
                    text_body TEXT,
                    archive_path TEXT,
                    original_path TEXT,
                    is_view_once INTEGER DEFAULT 0,
                    is_edited INTEGER DEFAULT 0,
                    is_starred INTEGER DEFAULT 0,
                    is_pinned INTEGER DEFAULT 0,
                    preservation_reason TEXT,
                    source_platform TEXT
                );
                INSERT INTO preserved_messages (
                    rowid, msg_id, stanza_id, chat_id, chat_type, timestamp_ms,
                    sender, from_me, media_type, text_body, archive_path, preservation_reason, source_platform
                ) VALUES (
                    10, 999, 'STANZA_PRES_1', '123456789', 'contact', 1700000099000,
                    'You', 1, 'video', 'Preserved Video Note', 'Contacts/123456789/2023/Sent/ptv-123.mp4', 'deleted', 'android'
                );
                """
            )

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            counts = client.get("/api/media/count?chat_id=123456789&chat_type=contact").get_json()
            media = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=video"
            ).get_json()

        assert counts["by_type"]["video"]["count"] == 1
        assert len(media) == 1
        assert media[0]["msg_id"] == -10
        assert media[0]["is_preserved"] == 1
        assert media[0]["media_type"] == "video"

    def test_media_filtering_returns_all_videos_including_missing(self, tmp_path):
        """/api/media with types=video returns all videos, both archived and missing."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)

        # Insert 1 archived video (type 3) and 2 missing videos (type 3 and type 81 PTV)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, message_type) VALUES "
            "(201, 10, 0, 1700000001000, 3), "
            "(202, 10, 0, 1700000002000, 3), "
            "(203, 10, 0, 1700000003000, 81)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) VALUES "
            "(201, 'Media/Video/vid1.mp4', 'vid1.mp4'), "
            "(202, 'Media/Video/vid2_missing.mp4', 'vid2_missing.mp4'), "
            "(203, 'Media/Video Notes/ptv_missing.mp4', 'ptv_missing.mp4')"
        )
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES "
            "('Media/Video/vid1.mp4', 'Contacts/123456789/2023/Received/vid1.mp4')"
        )
        wa_conn.commit()
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            counts = client.get("/api/media/count?chat_id=123456789&chat_type=contact").get_json()
            media = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=video"
            ).get_json()

        assert counts["by_type"]["video"]["count"] == 3
        assert counts["by_type"]["video"]["missing"] == 2
        assert len(media) == 3
        archived = [m for m in media if m["is_missing"] == 0]
        missing = [m for m in media if m["is_missing"] == 1]
        assert len(archived) == 1
        assert len(missing) == 2
        assert any(m["is_video_note"] == 1 for m in missing)

    def test_gallery_missing_media_card_css_and_warning_badge(self):
        """Gallery missing media cards must use the warning badge, yellow icon, and blurred DB thumbnail."""
        css_path = Path(_ROOT) / "wab_viewer" / "chat_viewer" / "app.css"
        css = css_path.read_text(encoding="utf-8")
        js = _get_app_js()

        assert ".gallery-item.is-missing" in css
        assert ".gallery-missing-badge" in css
        assert ".gallery-missing-icon-warn" in css
        assert "#f59e0b" in css, "Warning yellow color token must be present in app.css"
        assert ".gallery-missing-indicator-badge" in css
        assert ".gallery-item.has-thumb" in css
        assert "buildMissingGalleryCard" in js
        assert "_sanitizeMediaName" in js
        assert "View Once" in js
        assert "gallery-missing-indicator-badge" in js

    def test_gallery_video_note_circular_styling_and_lightbox(self):
        """Video notes (PTV) must enforce 1:1 circular round preview in gallery and lightbox."""
        css_path = Path(_ROOT) / "wab_viewer" / "chat_viewer" / "app.css"
        css = css_path.read_text(encoding="utf-8")
        js = _get_app_js()

        assert ".gallery-item.is-video-note" in css
        assert "border-radius: 50%" in css
        assert ".gallery-ptv-badge" in css
        assert ".lb-video-note" in css
        assert "is-video-note" in js
        assert "gallery-ptv-badge" in js
        assert "lb-video-note" in js

    def test_chat_missing_media_fallback_and_grid_styling(self):
        """Chat view and media grid must dynamically fallback to renderMissingMedia on error."""
        css_path = Path(_ROOT) / "wab_viewer" / "chat_viewer" / "app.css"
        css = css_path.read_text(encoding="utf-8")
        js = _get_app_js()

        assert ".media-grid-cell .media-missing-card" in css
        assert ".media-grid-cell .media-missing-badge" in css
        assert "renderMissingMedia(msg)" in js
        assert "wrap.replaceWith(missingCard)" in js
        assert "!m.is_missing" in js
        assert "showLightboxForGroup" in js
        assert "_sanitizeMediaName(msg.media_name)" in js


class TestLivePhotoPairing:
    """Verify Android Motion Photo (type 11) and iOS Live Photo (type 12) pairing."""

    def test_android_motion_photo_pairing(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER,
                from_me INTEGER,
                timestamp INTEGER,
                text_data TEXT,
                message_type INTEGER
            );
            CREATE TABLE message_media (
                message_row_id INTEGER PRIMARY KEY,
                file_path TEXT
            );
            CREATE TABLE message_association (
                _id INTEGER PRIMARY KEY,
                parent_message_row_id INTEGER,
                child_message_row_id INTEGER,
                association_type INTEGER
            );

            -- Parent image
            INSERT INTO message VALUES (10, 1, 0, 1700000000000, '', 1);
            INSERT INTO message_media VALUES (10, 'Media/WhatsApp Images/IMG_001.jpg');

            -- Child companion motion clip
            INSERT INTO message VALUES (11, 1, 0, 1700000000100, '', 3);
            INSERT INTO message_media VALUES (11, 'Media/WhatsApp Video/VID_001.mp4');

            -- Association (type 11 = Motion Photo)
            INSERT INTO message_association VALUES (1, 10, 11, 11);
        """)
        conn.commit()

        # Archive DB
        arch_conn = sqlite3.connect(str(archive_path))
        arch_conn.executescript("""
            CREATE TABLE archive_copies (
                original_path TEXT PRIMARY KEY,
                archive_path TEXT
            );
            INSERT INTO archive_copies VALUES ('Media/WhatsApp Video/VID_001.mp4', 'Media/VID_001.mp4');
        """)
        arch_conn.commit()
        arch_conn.close()

        conn.execute("ATTACH DATABASE ? AS arch", (str(archive_path),))

        rows = [
            {"msg_id": 10, "media_type": "image", "archive_path": "Media/IMG_001.jpg"},
            {"msg_id": 11, "media_type": "video", "archive_path": "Media/VID_001.mp4"},
        ]

        viewer_hydration._hydrate_live_photos(rows, "android", conn)

        assert rows[0].get("is_live_photo") == 1
        assert rows[0].get("live_photo_path") == "Media/VID_001.mp4"
        assert rows[1].get("is_live_photo_video") == 1

        conn.close()

    def test_ios_live_photo_pairing(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        ext_path = tmp_path / "ExtChatDatabase.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZSTANZAID TEXT,
                ZMEDIAITEM INTEGER
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT
            );

            INSERT INTO ZWAMESSAGE VALUES (10, 'STANZA_IMG', 1);
            INSERT INTO ZWAMEDIAITEM VALUES (1, 'Media/IMG_001.jpg');

            INSERT INTO ZWAMESSAGE VALUES (11, 'STANZA_VID', 2);
            INSERT INTO ZWAMEDIAITEM VALUES (2, 'Media/VID_001.mp4');
        """)
        conn.commit()

        ext_conn = sqlite3.connect(str(ext_path))
        ext_conn.executescript("""
            CREATE TABLE message_parent_association (
                stanza_id TEXT,
                parent_stanza_id TEXT,
                type INTEGER
            );
            INSERT INTO message_parent_association VALUES ('STANZA_VID', 'STANZA_IMG', 12);
        """)
        ext_conn.commit()
        ext_conn.close()

        arch_conn = sqlite3.connect(str(archive_path))
        arch_conn.executescript("""
            CREATE TABLE archive_copies (
                original_path TEXT PRIMARY KEY,
                archive_path TEXT
            );
            INSERT INTO archive_copies VALUES ('Message/Media/VID_001.mp4', 'Media/VID_001.mp4');
        """)
        arch_conn.commit()
        arch_conn.close()

        conn.execute("ATTACH DATABASE ? AS ext", (str(ext_path),))
        conn.execute("ATTACH DATABASE ? AS arch", (str(archive_path),))

        rows = [
            {"stanza_id": "STANZA_IMG", "msg_id": 10, "media_type": "image"},
            {"stanza_id": "STANZA_VID", "msg_id": 11, "media_type": "video"},
        ]

        viewer_hydration._hydrate_live_photos(rows, "ios", conn)

        assert rows[0].get("is_live_photo") == 1
        assert rows[0].get("live_photo_path") == "Media/VID_001.mp4"
        assert rows[1].get("is_live_photo_video") == 1

        conn.close()


class TestExtendedMediaMap:
    def test_android_extended_media_data_hydration(self, tmp_path):
        """Test that Android messages referencing extended_media_data via message_media_map are hydrated."""
        db_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER, message_type INTEGER, text_data TEXT, from_me INTEGER, key_id TEXT);
            CREATE TABLE message_media (message_row_id INTEGER PRIMARY KEY, file_path TEXT, mime_type TEXT, width INTEGER, height INTEGER, file_size INTEGER, media_name TEXT, media_duration INTEGER);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY, message_row_id INTEGER, text_data TEXT, from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER);
            CREATE TABLE message_media_map (_id INTEGER PRIMARY KEY AUTOINCREMENT, message_row_id INTEGER NOT NULL, chat_row_id INTEGER NOT NULL, media_row_id INTEGER NOT NULL);
            CREATE TABLE extended_media_data (row_id INTEGER PRIMARY KEY AUTOINCREMENT, type INTEGER NOT NULL, direct_path TEXT, file_path TEXT, file_hash TEXT, file_size INTEGER, media_key BLOB, media_key_timestamp INTEGER, enc_file_hash TEXT, width INTEGER, height INTEGER, media_caption TEXT, transferred INTEGER, preview_path TEXT, external_url TEXT, mime_type TEXT, display_type INTEGER);

            INSERT INTO chat VALUES (1, 1, 'Project Chat');
            INSERT INTO jid VALUES (1, '15551234567', 's.whatsapp.net', '15551234567@s.whatsapp.net');
            INSERT INTO message VALUES (100, 1, 1, 1700000000000, 1, 'Photo message', 0, 'STANZA_EXT_1');
            INSERT INTO message_media_map VALUES (1, 100, 1, 50);
            INSERT INTO extended_media_data (row_id, type, file_path, file_size, width, height, mime_type)
            VALUES (50, 1, 'Media/WhatsApp Images/carousel_item_1.jpg', 98765, 1200, 800, 'image/jpeg');
        """)
        conn.commit()
        conn.close()

        arch_conn = sqlite3.connect(archive_path)
        arch_conn.execute("CREATE TABLE archive_copies (original_path TEXT, archive_path TEXT)")
        arch_conn.execute(
            "INSERT INTO archive_copies VALUES ('Media/WhatsApp Images/carousel_item_1.jpg', 'Archived/carousel_item_1.jpg')"
        )
        arch_conn.commit()
        arch_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=1&chat_type=group")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 1
            m = msgs[0]
            assert m["msg_id"] == 100
            assert m["media_type"] == "image"
            assert m["media_width"] == 1200
            assert m["media_height"] == 800
            assert m["media_size"] == 98765
            assert m["archive_path"] == "Archived/carousel_item_1.jpg"

    def test_ios_quoted_extended_text_reply_and_self_reply(self, tmp_path):
        """Test iOS quoted replies referencing extendedTextMessage (URLs) and self-replies without participant tags."""
        wa_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZGROUPINFO INTEGER,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT,
                ZTITLE TEXT,
                ZMETADATA BLOB,
                ZMEDIAURL TEXT
            );
            CREATE TABLE ZWAGROUPINFO (
                Z_PK INTEGER PRIMARY KEY,
                ZCREATIONDATE REAL,
                ZCREATORJID TEXT,
                ZPICTUREID TEXT
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMEMBERJID TEXT,
                ZISACTIVE INTEGER
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (
                Z_PK INTEGER PRIMARY KEY,
                ZJID TEXT,
                ZPUSHNAME TEXT
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEDATE REAL,
                ZSTANZAID TEXT,
                ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER,
                ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT,
                ZTEXT TEXT,
                ZFROMJID TEXT
            );

            INSERT INTO ZWACHATSESSION VALUES (1, 1, '120363000000000002@g.us', 'Team Project');
            INSERT INTO ZWAGROUPMEMBER VALUES (10, 1, '145500000000002@lid', 1);
            INSERT INTO ZWAPROFILEPUSHNAME VALUES (1, '145500000000002@lid', 'Bob Tester');
        """)

        def encode_varint(v):
            res = bytearray()
            while v > 0x7F:
                res.append((v & 0x7F) | 0x80)
                v >>= 7
            res.append(v & 0x7F)
            return bytes(res)

        stanza_orig = b"STANZA_LINK_ORIG"
        url_text = b"https://youtu.be/dQw4w9WgXcQ"

        # ExtendedTextMessage (subfield 6 in message container 19)
        # subfield 1: text
        # subfield 2: matched text
        ex_payload = (
            encode_varint((1 << 3) | 2)
            + encode_varint(len(url_text))
            + url_text
            + encode_varint((2 << 3) | 2)
            + encode_varint(len(url_text))
            + url_text
        )
        t19_sub6 = encode_varint((6 << 3) | 2) + encode_varint(len(ex_payload)) + ex_payload

        # Self-reply blob: contains wire tag 5 (q_stanza) and 19 (message), but OMITS tag 6 (participant)
        metadata_self_reply = (
            encode_varint((5 << 3) | 2)
            + encode_varint(len(stanza_orig))
            + stanza_orig
            + encode_varint((19 << 3) | 2)
            + encode_varint(len(t19_sub6))
            + t19_sub6
        )

        conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZMETADATA, ZMEDIAURL) VALUES (1, NULL, NULL, NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZMETADATA, ZMEDIAURL) VALUES (2, NULL, NULL, ?, NULL)",
            (metadata_self_reply,),
        )

        # Message 50: Original message with YouTube link sent by Bob Tester (ZGROUPMEMBER=10)
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (50, 1, 0, 700000000.0, 'STANZA_LINK_ORIG', 7, 1, 10, NULL, 'Bob Tester', 'https://youtu.be/dQw4w9WgXcQ', '120363000000000002@g.us')
        """)

        # Message 51: Self-reply sent by Bob Tester (ZGROUPMEMBER=10) referencing Message 50
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (51, 1, 0, 700000010.0, 'STANZA_SELF_REPLY', 0, 2, 10, NULL, 'Bob Tester', 'Check out this presentation video!', '120363000000000002@g.us')
        """)

        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=1&chat_type=group")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 2
            reply_msg = next(m for m in msgs if m["msg_id"] == 51)

            # Quoted text must be extracted from extendedTextMessage (not empty!)
            assert reply_msg["quoted_text"] == "https://youtu.be/dQw4w9WgXcQ"
            # Quoted sender must be inferred from the message sender for self-replies
            assert reply_msg["quoted_sender"] == "Bob Tester"
            assert reply_msg["quoted_msg_id"] == 50
            assert reply_msg["quoted_ts"] == int((700000000.0 + 978307200) * 1000)

    def test_ios_resolve_quoted_msg_ids_backfill_text_and_sender(self, tmp_path):
        """Test _resolve_quoted_msg_ids backfills quoted_text and quoted_sender from live ZWAMESSAGE when empty."""
        wa_path = tmp_path / "ChatStorage.sqlite"
        conn = sqlite3.connect(str(wa_path))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZSTANZAID TEXT,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZISFROMME INTEGER,
                ZPUSHNAME TEXT,
                ZFROMJID TEXT,
                ZMEDIAITEM INTEGER
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZTITLE TEXT
            );
            INSERT INTO ZWAMEDIAITEM VALUES (1, 'Caption photo');
            INSERT INTO ZWAMESSAGE VALUES (10, 'STANZA_PARENT_1', 700000000.0, 1, NULL, 0, 'Alice', '1555123456@s.whatsapp.net', 1);
            INSERT INTO ZWAMESSAGE VALUES (20, 'STANZA_PARENT_2', 700000050.0, 0, 'Original text from You', 1, 'You', NULL, NULL);
        """)
        conn.commit()

        # Rows simulating incoming messages where ZMETADATA had quoted_stanza_id but missing text/sender
        rows = [
            {
                "msg_id": 100,
                "stanza_id": "STANZA_CHILD_100",
                "quoted_stanza_id": "STANZA_PARENT_1",
                "quoted_msg_id": None,
                "quoted_text": "",
                "quoted_sender": "",
                "from_me": 0,
                "sender": "Bob",
            },
            {
                "msg_id": 101,
                "stanza_id": "STANZA_CHILD_101",
                "quoted_stanza_id": "STANZA_PARENT_2",
                "quoted_msg_id": None,
                "quoted_text": "",
                "quoted_sender": "",
                "from_me": 1,
                "sender": "You",
            },
            # Row 102 quotes row 101 via local_map window
            {
                "msg_id": 102,
                "stanza_id": "STANZA_CHILD_102",
                "quoted_stanza_id": "STANZA_CHILD_101",
                "quoted_msg_id": None,
                "quoted_text": "",
                "quoted_sender": "",
                "from_me": 0,
                "sender": "Charlie",
                "text_body": "Replying to 101",
            },
        ]

        # Give row 101 a text_body so row 102 can backfill from local_map
        rows[1]["text_body"] = "I am message 101"

        viewer_preserved._resolve_quoted_msg_ids(rows, "ios", conn, None)

        # Row 100 resolved against ZWAMESSAGE (parent 10)
        assert rows[0]["quoted_msg_id"] == 10
        assert rows[0]["quoted_text"] == "Caption photo"
        assert rows[0]["quoted_has_thumb"] == 1
        assert rows[0]["quoted_sender"] == "Alice"

        # Row 101 resolved against ZWAMESSAGE (parent 20)
        assert rows[1]["quoted_msg_id"] == 20
        assert rows[1]["quoted_text"] == "Original text from You"
        assert rows[1]["quoted_sender"] == "You"

        # Row 102 resolved against local_map (row 101)
        assert rows[2]["quoted_msg_id"] == 101
        assert rows[2]["quoted_text"] == "I am message 101"
        assert rows[2]["quoted_sender"] == "You"
