"""Tests for wab_archiver.preservation."""

from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
from pathlib import Path

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

_TEST_LOGGER = logging.getLogger("test_preservation")
_TEST_LOGGER.setLevel(logging.INFO)

from tests.preservation.conftest import _make_dummy_archive_db
from wab_archiver import preservation


def test_diff_and_extract_ios(tmp_path: Path) -> None:
    old_db = tmp_path / "old_chatstorage.sqlite"
    new_db = tmp_path / "new_chatstorage.sqlite"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, 'partner@s.whatsapp.net', 'Partner', NULL);

            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZSTANZAID TEXT,
                ZISFROMME INTEGER,
                ZFROMJID TEXT,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER
            );
            INSERT INTO ZWAMESSAGE VALUES
                (1, 1, 'ios_stay', 0, 'partner@s.whatsapp.net', 1000.0, 0, 'iOS Stay', 'Partner', NULL, NULL),
                (2, 1, 'ios_del', 0, 'partner@s.whatsapp.net', 1001.0, 0, 'iOS Deleted', 'Partner', NULL, NULL),
                (3, 1, 'ios_rev', 0, 'partner@s.whatsapp.net', 1002.0, 0, 'iOS Revoked', 'Partner', NULL, NULL);
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, 'partner@s.whatsapp.net', 'Partner', NULL);

            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZSTANZAID TEXT,
                ZISFROMME INTEGER,
                ZFROMJID TEXT,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER
            );
            INSERT INTO ZWAMESSAGE VALUES
                (1, 1, 'ios_stay', 0, 'partner@s.whatsapp.net', 1000.0, 0, 'iOS Stay', 'Partner', NULL, NULL),
                (3, 1, 'ios_rev', 0, 'partner@s.whatsapp.net', 1002.0, 14, '', 'Partner', NULL, NULL);
        """)

    extracted = preservation.diff_and_extract_ios(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    by_key = {r["stanza_id"]: r for r in extracted}
    assert "ios_stay" not in by_key
    assert "ios_del" in by_key
    assert by_key["ios_del"]["preservation_reason"] == "deleted_on_phone"
    assert "ios_rev" in by_key
    assert by_key["ios_rev"]["preservation_reason"] == "revoked_by_sender"


def test_diff_and_extract_ios_stubs_not_preserved(tmp_path: Path) -> None:
    old_db = tmp_path / "old_ios_stubs.sqlite"
    new_db = tmp_path / "new_ios_stubs.sqlite"
    arch_db = tmp_path / "arch_ios_stubs.db"
    _make_dummy_archive_db(arch_db)

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, 'partner@s.whatsapp.net', 'Partner', NULL);

            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZSTANZAID TEXT,
                ZISFROMME INTEGER,
                ZFROMJID TEXT,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER
            );
            INSERT INTO ZWAMESSAGE VALUES
                (1, 1, 'ios_sys_stub', 0, 'partner@s.whatsapp.net', 1000.0, 6, NULL, 'Partner', NULL, NULL),
                (2, 1, 'ios_empty_text_stub', 0, 'partner@s.whatsapp.net', 1001.0, 0, '', 'Partner', NULL, NULL),
                (3, 1, 'ios_already_rev_purged', 0, 'partner@s.whatsapp.net', 1002.0, 14, '', 'Partner', NULL, NULL),
                (4, 1, 'ios_real_rev', 0, 'partner@s.whatsapp.net', 1003.0, 0, 'Valid text', 'Partner', NULL, NULL);
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, 'partner@s.whatsapp.net', 'Partner', NULL);

            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZSTANZAID TEXT,
                ZISFROMME INTEGER,
                ZFROMJID TEXT,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER
            );
            INSERT INTO ZWAMESSAGE VALUES
                (1, 1, 'ios_sys_stub', 0, 'partner@s.whatsapp.net', 1000.0, 14, '', 'Partner', NULL, NULL),
                (2, 1, 'ios_empty_text_stub', 0, 'partner@s.whatsapp.net', 1001.0, 14, '', 'Partner', NULL, NULL),
                (4, 1, 'ios_real_rev', 0, 'partner@s.whatsapp.net', 1003.0, 14, '', 'Partner', NULL, NULL);
        """)

    extracted = preservation.diff_and_extract_ios(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    by_key = {r["stanza_id"]: r for r in extracted}
    assert "ios_sys_stub" not in by_key
    assert "ios_empty_text_stub" not in by_key
    assert "ios_already_rev_purged" not in by_key
    assert "ios_real_rev" in by_key
    assert by_key["ios_real_rev"]["text_body"] == "Valid text"
    assert by_key["ios_real_rev"]["preservation_reason"] == "revoked_by_sender"


def test_diff_and_extract_ios_deleted_on_phone_excludes_purged_and_empty_stubs(
    tmp_path: Path,
) -> None:
    old_db = tmp_path / "old_ios_purged.sqlite"
    new_db = tmp_path / "new_ios_purged.sqlite"
    arch_db = tmp_path / "arch_ios_purged.db"
    _make_dummy_archive_db(arch_db)

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, 'partner@s.whatsapp.net', 'Partner', NULL);

            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZSTANZAID TEXT,
                ZISFROMME INTEGER,
                ZFROMJID TEXT,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER,
                ZGROUPEVENTTYPE INTEGER
            );
            INSERT INTO ZWAMESSAGE VALUES
                (1, 1, 'ios_purged_stub', 0, 'partner@s.whatsapp.net', 1000.0, 0, '', 'Partner', NULL, NULL, NULL),
                (2, 1, 'ios_empty_sys_stub', 0, 'partner@s.whatsapp.net', 1001.0, 6, NULL, 'Partner', NULL, NULL, NULL),
                (3, 1, 'ios_sys_notice_e2e', 0, 'partner@s.whatsapp.net', 1002.0, 6, 'Messages are end-to-end encrypted', 'Partner', NULL, NULL, NULL),
                (4, 1, 'ios_valid_user_msg', 0, 'partner@s.whatsapp.net', 1003.0, 0, 'Hello world', 'Partner', NULL, NULL, NULL);
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, 'partner@s.whatsapp.net', 'Partner', NULL);

            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZSTANZAID TEXT,
                ZISFROMME INTEGER,
                ZFROMJID TEXT,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER,
                ZGROUPEVENTTYPE INTEGER
            );
        """)

    extracted = preservation.diff_and_extract_ios(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    by_key = {r["stanza_id"]: r for r in extracted}
    assert "ios_purged_stub" not in by_key
    assert "ios_empty_sys_stub" not in by_key
    assert "ios_sys_notice_e2e" in by_key
    assert by_key["ios_sys_notice_e2e"]["media_type"] == "service"
    assert by_key["ios_sys_notice_e2e"]["text_body"] == "Messages are end-to-end encrypted"
    assert "ios_valid_user_msg" in by_key
    assert by_key["ios_valid_user_msg"]["text_body"] == "Hello world"
    assert by_key["ios_valid_user_msg"]["media_type"] == "text"


def test_diff_and_extract_ios_location_and_view_once(tmp_path: Path) -> None:
    old_db = tmp_path / "old_chatstorage.sqlite"
    new_db = tmp_path / "new_chatstorage.sqlite"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, '15551234@s.whatsapp.net', 'Alice Partner', NULL);

            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZFILESIZE INTEGER,
                ZMOVIEDURATION INTEGER,
                ZLATITUDE REAL,
                ZLONGITUDE REAL,
                ZMEDIALOCALPATH TEXT,
                ZMEDIAURL TEXT,
                ZTITLE TEXT,
                ZVCARDSTRING TEXT
            );
            INSERT INTO ZWAMEDIAITEM VALUES
                (10, NULL, NULL, 40.7128, -74.0060, NULL, 'https://maps.apple.com/?q=40.7128,-74.0060', 'Statue of Liberty', NULL),
                (11, 307200, 0, NULL, NULL, 'Media/photo_vo.jpg', NULL, 'photo_vo.jpg', NULL),
                (12, 1536000, 14, NULL, NULL, 'Media/video_vo.mp4', NULL, 'video_vo.mp4', NULL);

            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZSTANZAID TEXT,
                ZISFROMME INTEGER,
                ZFROMJID TEXT,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER
            );
            INSERT INTO ZWAMESSAGE VALUES
                (101, 1, 'ios_loc', 0, '15551234@s.whatsapp.net', 1000.0, 5, 'Check location', 'Alice', 10, NULL),
                (102, 1, 'ios_vo_photo', 0, '15551234@s.whatsapp.net', 1001.0, 38, 'View once photo', 'Alice', 11, NULL),
                (103, 1, 'ios_vo_video', 0, '15551234@s.whatsapp.net', 1002.0, 39, 'View once video', 'Alice', 12, NULL);
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZSTANZAID TEXT,
                ZISFROMME INTEGER,
                ZFROMJID TEXT,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER
            );
        """)

    extracted = preservation.diff_and_extract_ios(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    by_key = {r["stanza_id"]: r for r in extracted}
    assert len(by_key) == 3

    loc = by_key["ios_loc"]
    assert loc["media_type"] == "location"
    assert loc["latitude"] == 40.7128
    assert loc["longitude"] == -74.0060
    assert loc["location_name"] == "Statue of Liberty"
    assert loc["location_url"] == "https://maps.apple.com/?q=40.7128,-74.0060"

    p_vo = by_key["ios_vo_photo"]
    assert p_vo["is_view_once"] == 1
    assert p_vo["media_type"] == "image"
    assert p_vo["media_size"] == 307200

    v_vo = by_key["ios_vo_video"]
    assert v_vo["is_view_once"] == 1
    assert v_vo["media_type"] == "video"
    assert v_vo["media_duration"] == 14
    assert v_vo["media_size"] == 1536000


def test_diff_and_extract_ios_quoted_placeholder(tmp_path: Path) -> None:
    old_db = tmp_path / "old_chatstorage.sqlite"
    new_db = tmp_path / "new_chatstorage.sqlite"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, '15555678@s.whatsapp.net', 'Bob Partner', NULL);

            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZSTANZAID TEXT,
                ZISFROMME INTEGER,
                ZFROMJID TEXT,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER,
                ZPARENTMESSAGE INTEGER
            );
            INSERT INTO ZWAMESSAGE VALUES
                (201, 1, 'parent_photo_stanza', 0, '15555678@s.whatsapp.net', 1000.0, 1, '', 'Bob', NULL, NULL, NULL),
                (202, 1, 'reply_stanza', 1, NULL, 1005.0, 0, 'Awesome photo!', 'You', NULL, NULL, 201);
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZSTANZAID TEXT,
                ZISFROMME INTEGER,
                ZFROMJID TEXT,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER,
                ZPARENTMESSAGE INTEGER
            );
        """)

    extracted = preservation.diff_and_extract_ios(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    by_key = {r["stanza_id"]: r for r in extracted}
    assert "reply_stanza" in by_key
    rep = by_key["reply_stanza"]
    assert rep["quoted_stanza_id"] == "parent_photo_stanza"
    assert rep["quoted_parent_type"] == "image"
    assert rep["quoted_text"] == "📷 Photo"


def test_diff_and_extract_ios_polls_and_reactions(tmp_path: Path) -> None:
    old_db = tmp_path / "old_chatstorage.sqlite"
    new_db = tmp_path / "new_chatstorage.sqlite"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    # Construct synthetic protobuf blobs for poll and reaction
    # Poll: question "Pizza?", options "Yes" / "No", 1 vote for "Yes"
    opt1 = bytes([0x0A, 3]) + b"Yes"
    opt2 = bytes([0x0A, 2]) + b"No"
    p_blob = (
        bytes([0x12, 6])
        + b"Pizza?"
        + bytes([0x1A, len(opt1)])
        + opt1
        + bytes([0x1A, len(opt2)])
        + opt2
        + bytes([0x2A, 2, 0x08, 0])
    )
    poll_blob = bytes([0x42, len(p_blob)]) + p_blob

    # Reaction: 👍 (F0 9F 91 8D)
    rx_blob = bytes([0x3A, 8, 0x0A, 6, 0x1A, 4, 0xF0, 0x9F, 0x91, 0x8D])

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, '15553333@s.whatsapp.net', 'Charlie', NULL);

            CREATE TABLE ZWAMESSAGEINFO (
                Z_PK INTEGER PRIMARY KEY,
                ZMESSAGE INTEGER,
                ZRECEIPTINFO BLOB
            );

            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZSTANZAID TEXT,
                ZISFROMME INTEGER,
                ZFROMJID TEXT,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER,
                ZMESSAGEINFO INTEGER
            );
            INSERT INTO ZWAMESSAGE VALUES
                (301, 1, 'ios_poll_msg', 0, '15553333@s.whatsapp.net', 1000.0, 46, 'Pizza?', 'Charlie', NULL, NULL, 501),
                (302, 1, 'ios_rx_msg', 0, '15553333@s.whatsapp.net', 1001.0, 0, 'Great idea', 'Charlie', NULL, NULL, 502);
        """)
        conn.execute("INSERT INTO ZWAMESSAGEINFO VALUES (501, 301, ?)", (poll_blob,))
        conn.execute("INSERT INTO ZWAMESSAGEINFO VALUES (502, 302, ?)", (rx_blob,))

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZSTANZAID TEXT,
                ZISFROMME INTEGER,
                ZFROMJID TEXT,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER,
                ZMESSAGEINFO INTEGER
            );
        """)

    extracted = preservation.diff_and_extract_ios(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    by_key = {r["stanza_id"]: r for r in extracted}
    assert len(by_key) == 2

    poll_msg = by_key["ios_poll_msg"]
    assert poll_msg["media_type"] == "poll"
    assert poll_msg["poll_data"] is not None

    import json

    p_dict = json.loads(poll_msg["poll_data"])
    assert p_dict["total_votes"] == 1
    assert p_dict["options"][0]["name"] == "Yes"
    assert p_dict["options"][0]["votes"] == 1
    assert p_dict["options"][1]["name"] == "No"
    assert p_dict["options"][1]["votes"] == 0

    rx_msg = by_key["ios_rx_msg"]
    assert rx_msg["reactions"] == "👍"


def test_diff_and_extract_ios_calls(tmp_path: Path) -> None:
    old_dir = tmp_path / "old_backup"
    new_dir = tmp_path / "new_backup"
    os.makedirs(old_dir, exist_ok=True)
    os.makedirs(new_dir, exist_ok=True)

    old_db = old_dir / "ChatStorage.sqlite"
    new_db = new_dir / "ChatStorage.sqlite"
    old_call_db = old_dir / "CallHistory.sqlite"
    new_call_db = new_dir / "CallHistory.sqlite"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    with sqlite3.connect(old_call_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWAAGGREGATECALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZINCOMING INTEGER,
                ZVIDEO INTEGER
            );
            INSERT INTO ZWAAGGREGATECALLEVENT VALUES
                (1, 1, 0),
                (2, 0, 1);

            CREATE TABLE ZWACDCALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZCALLIDSTRING TEXT,
                ZGROUPJIDSTRING TEXT,
                ZDATE REAL,
                ZDURATION INTEGER,
                ZOUTCOME INTEGER,
                ZBYTESRECEIVED INTEGER,
                ZBYTESSENT INTEGER,
                ZAGGREGATECALLEVENT INTEGER
            );
            INSERT INTO ZWACDCALLEVENT VALUES
                (10, 'call_ios_1', NULL, 1000.0, 75, 5, 100000, 50000, 1),
                (20, 'call_ios_grp', 'ios_group@g.us', 1010.0, 180, 5, 500000, 200000, 2);

            CREATE TABLE ZWACDCALLEVENTPARTICIPANT (
                Z_PK INTEGER PRIMARY KEY,
                Z1PARTICIPANTS INTEGER,
                ZJIDSTRING TEXT,
                ZOUTCOME INTEGER
            );
            INSERT INTO ZWACDCALLEVENTPARTICIPANT VALUES
                (1, 10, '15554444@s.whatsapp.net', 5),
                (2, 20, '15554444@s.whatsapp.net', 5),
                (3, 20, '15555555@s.whatsapp.net', 5);
        """)

    with sqlite3.connect(new_call_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACDCALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZCALLIDSTRING TEXT
            );
        """)

    extracted_calls = preservation.diff_and_extract_ios_calls(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    assert len(extracted_calls) == 2
    by_id = {c["call_id"]: c for c in extracted_calls}

    c1 = by_id["call_ios_1"]
    assert c1["caller_jid"] == "15554444@s.whatsapp.net"
    assert c1["duration_seconds"] == 75
    assert c1["from_me"] == 0
    assert c1["is_video"] == 0
    assert c1["is_group_call"] == 0

    c2 = by_id["call_ios_grp"]
    assert c2["is_group_call"] == 1
    assert c2["chat_jid"] == "ios_group@g.us"
    assert c2["duration_seconds"] == 180
    assert c2["from_me"] == 1
    assert c2["is_video"] == 1
    assert c2["participants"] is not None

    import json

    parts = json.loads(c2["participants"])
    assert len(parts) == 2
    assert parts[0]["jid"] == "15554444@s.whatsapp.net"


def test_diff_and_extract_ios_deleted_group_roster(tmp_path: Path) -> None:
    old_db = tmp_path / "old_chatstorage.sqlite"
    new_db = tmp_path / "new_chatstorage.sqlite"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWAGROUPINFO (
                Z_PK INTEGER PRIMARY KEY,
                ZCREATIONDATE REAL,
                ZCREATORJID TEXT
            );
            INSERT INTO ZWAGROUPINFO VALUES (1, 1000.0, '15556666@s.whatsapp.net');

            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (10, 'ios_team@g.us', 'iOS Dream Team', 1);

            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMEMBERJID TEXT,
                ZCONTACTNAME TEXT,
                ZFIRSTNAME TEXT,
                ZISACTIVE INTEGER,
                ZISADMIN INTEGER
            );
            INSERT INTO ZWAGROUPMEMBER VALUES
                (1, 10, '15556666@s.whatsapp.net', 'Captain Steve', 'Steve', 1, 2),
                (2, 10, '15557777@s.whatsapp.net', 'Tony Member', 'Tony', 1, 0),
                (3, 10, '15558888@s.whatsapp.net', 'Departed Member', 'Departed', 0, 0);

            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZSTANZAID TEXT,
                ZISFROMME INTEGER,
                ZFROMJID TEXT,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER
            );
            INSERT INTO ZWAMESSAGE VALUES
                (501, 10, 'ios_grp_m1', 0, '15556666@s.whatsapp.net', 1000.0, 0, 'Welcome Team', 'Steve', NULL, NULL);
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZSTANZAID TEXT,
                ZISFROMME INTEGER,
                ZFROMJID TEXT,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER
            );
        """)

    extracted = preservation.diff_and_extract_ios(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    assert len(extracted) == 1
    assert extracted[0]["preservation_reason"] == "chat_deleted"
    assert extracted[0].get("_chat_members_json") is not None

    import json

    members = json.loads(extracted[0]["_chat_members_json"])
    # Departed member (ZISACTIVE = 0) must be excluded
    assert len(members) == 2
    assert members[0]["jid"] == "15556666@s.whatsapp.net"
    assert members[0]["name"] == "Captain Steve"
    assert members[0]["rank"] == 2
    assert members[1]["jid"] == "15557777@s.whatsapp.net"
    assert members[1]["name"] == "Tony Member"
    assert members[1]["rank"] == 0

    # Verify recording populates preserved_chats with members_json
    output_dir = tmp_path / "output_ios_roster"
    os.makedirs(output_dir, exist_ok=True)
    with preservation.open_preservation_db(str(output_dir)) as pres_conn:
        res = preservation.record_preserved_messages(pres_conn, extracted, "ios", _TEST_LOGGER)
        assert res.messages == 1
        assert res.chats == 1

        chat_row = pres_conn.execute(
            "SELECT subject, members_json FROM preserved_chats WHERE chat_id = '10'"
        ).fetchone()
        assert chat_row is not None
        assert chat_row[0] == "iOS Dream Team"
        saved_members = json.loads(chat_row[1])
        assert len(saved_members) == 2


def test_diff_and_extract_ios_rowid_shift(tmp_path: Path) -> None:
    old_db = tmp_path / "old_ios_shift.db"
    new_db = tmp_path / "new_ios_shift.db"
    arch_db = tmp_path / "empty_arch_ios_shift.db"
    sqlite3.connect(arch_db).close()

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (10, 'partner@s.whatsapp.net', 'Partner', NULL);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZSTANZAID TEXT,
                ZISFROMME INTEGER, ZFROMJID TEXT, ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER, ZTEXT TEXT, ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER
            );
            INSERT INTO ZWAMESSAGE VALUES
                (1, 10, 'ios_shift_k1', 0, 'partner@s.whatsapp.net', 1000.0, 0, 'iOS Message', 'Partner', NULL, NULL);
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (88, 'partner@s.whatsapp.net', 'Partner', NULL);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZSTANZAID TEXT,
                ZISFROMME INTEGER, ZFROMJID TEXT, ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER, ZTEXT TEXT, ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER
            );
        """)

    extracted = preservation.diff_and_extract_ios(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    assert len(extracted) == 1
    assert extracted[0]["stanza_id"] == "ios_shift_k1"
    # chat_id is updated to 88
    assert extracted[0]["chat_id"] == "88"
    assert extracted[0]["preservation_reason"] == "deleted_on_phone"


def test_diff_and_extract_ios_fresh_archive_db(tmp_path: Path) -> None:
    old_db = tmp_path / "old_ios_fresh.db"
    new_db = tmp_path / "new_ios_fresh.db"
    arch_db = tmp_path / "empty_arch_ios.db"
    sqlite3.connect(arch_db).close()

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, 'partner@s.whatsapp.net', 'Partner', NULL);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZSTANZAID TEXT,
                ZISFROMME INTEGER, ZFROMJID TEXT, ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER, ZTEXT TEXT, ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER
            );
            INSERT INTO ZWAMESSAGE VALUES
                (1, 1, 'ios_k1', 0, 'partner@s.whatsapp.net', 1000.0, 0, 'iOS Deleted', 'Partner', NULL, NULL);
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, 'partner@s.whatsapp.net', 'Partner', NULL);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZSTANZAID TEXT,
                ZISFROMME INTEGER, ZFROMJID TEXT, ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER, ZTEXT TEXT, ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER
            );
        """)

    extracted = preservation.diff_and_extract_ios(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    assert len(extracted) == 1
    assert extracted[0]["stanza_id"] == "ios_k1"
    assert extracted[0]["text_body"] == "iOS Deleted"


def test_diff_and_extract_ios_video_caption_and_audio(tmp_path: Path) -> None:
    old_db = tmp_path / "old_ios_media.db"
    new_db = tmp_path / "new_ios_media.db"
    arch_db = tmp_path / "arch_ios_media.db"
    sqlite3.connect(arch_db).close()

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, 'partner@s.whatsapp.net', 'Partner', NULL);
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZMEDIALOCALPATH TEXT, ZVCARDSTRING TEXT
            );
            INSERT INTO ZWAMEDIAITEM VALUES
                (1, 'Video Caption', 'Media/v.mp4', 'video/mp4'),
                (2, NULL, 'Media/a.opus', 'audio/ogg; codecs=opus');
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZSTANZAID TEXT,
                ZISFROMME INTEGER, ZFROMJID TEXT, ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER, ZTEXT TEXT, ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER
            );
            INSERT INTO ZWAMESSAGE VALUES
                (1, 1, 'stanza_vid', 0, 'partner@s.whatsapp.net', 1000.0, 2, NULL, 'Partner', 1, NULL),
                (2, 1, 'stanza_aud', 0, 'partner@s.whatsapp.net', 1010.0, 3, NULL, 'Partner', 2, NULL);
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, 'partner@s.whatsapp.net', 'Partner', NULL);
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZMEDIALOCALPATH TEXT, ZVCARDSTRING TEXT
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZSTANZAID TEXT,
                ZISFROMME INTEGER, ZFROMJID TEXT, ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER, ZTEXT TEXT, ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER
            );
        """)

    extracted = preservation.diff_and_extract_ios(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    assert len(extracted) == 2
    vid_rec = next(r for r in extracted if r["stanza_id"] == "stanza_vid")
    aud_rec = next(r for r in extracted if r["stanza_id"] == "stanza_aud")

    assert vid_rec["media_type"] == "video"
    assert vid_rec["text_body"] == "Video Caption"

    assert aud_rec["media_type"] == "audio"


def test_diff_indexes_creation_and_chunked_progress_ios(tmp_path: Path) -> None:
    old_db = tmp_path / "old_chatstorage.sqlite"
    new_db = tmp_path / "new_chatstorage.sqlite"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, 'partner@s.whatsapp.net', 'Partner', NULL);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZSTANZAID TEXT,
                ZISFROMME INTEGER, ZFROMJID TEXT, ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER, ZTEXT TEXT, ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER
            );
        """)
        rows_old = [
            (
                i,
                1,
                f"ios_key_{i}",
                0,
                "partner@s.whatsapp.net",
                1000.0 + i,
                0,
                f"msg {i}",
                "Partner",
                None,
                None,
            )
            for i in range(1, 12001)
        ]
        conn.executemany(
            "INSERT INTO ZWAMESSAGE VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows_old
        )

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, 'partner@s.whatsapp.net', 'Partner', NULL);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZSTANZAID TEXT,
                ZISFROMME INTEGER, ZFROMJID TEXT, ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER, ZTEXT TEXT, ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER
            );
        """)
        rows_new = [
            (
                i,
                1,
                f"ios_key_{i}",
                0,
                "partner@s.whatsapp.net",
                1000.0 + i,
                14 if i == 700 else 0,
                "" if i == 700 else f"msg {i}",
                "Partner",
                None,
                None,
            )
            for i in range(1, 12001)
            if i != 500
        ]
        conn.executemany(
            "INSERT INTO ZWAMESSAGE VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows_new
        )

    extracted = preservation.diff_and_extract_ios(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    by_key = {r["stanza_id"]: r for r in extracted}
    assert "ios_key_500" in by_key
    assert by_key["ios_key_500"]["preservation_reason"] == "deleted_on_phone"
    assert "ios_key_700" in by_key
    assert by_key["ios_key_700"]["preservation_reason"] == "revoked_by_sender"

    # Verify that performance indexes were created
    with sqlite3.connect(new_db) as conn:
        indexes = {
            r[1] for r in conn.execute("SELECT * FROM sqlite_master WHERE type='index'").fetchall()
        }
        assert "idx_pres_cur_stanza" in indexes


def test_diff_and_extract_ios_link_preview(tmp_path: Path) -> None:
    """Verify iOS link preview metadata, on-disk thumbnails, and embedded ZMETADATA thumbnails are preserved."""
    old_db = tmp_path / "old_chatstorage.sqlite"
    new_db = tmp_path / "new_chatstorage.sqlite"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    # Prepare on-disk thumbnail file for message 1
    disk_thumb_bytes = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
    )
    thumb_dir = tmp_path / "Message" / "Thumbnail"
    thumb_dir.mkdir(parents=True)
    thumb_file = thumb_dir / "preview1.jpg"
    thumb_file.write_bytes(disk_thumb_bytes)

    # Message 2 embedded thumbnail bytes
    embedded_thumb_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF_EMBEDDED\xff\xd9"

    # Message 4 archive_copies thumbnail file
    ac_thumb_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF_ARCHIVE_COPIES\xff\xd9"
    ac_dir = tmp_path / "custom_archive"
    ac_dir.mkdir(parents=True)
    ac_file = ac_dir / "thumb_ac.jpg"
    ac_file.write_bytes(ac_thumb_bytes)
    with sqlite3.connect(arch_db) as aconn:
        aconn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES (?, ?)",
            ("Message/Thumbnail/ac.jpg", "custom_archive/thumb_ac.jpg"),
        )
        aconn.commit()

    for p in (old_db, new_db):
        with sqlite3.connect(p) as conn:
            conn.executescript("""
                CREATE TABLE ZWACHATSESSION (
                    Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZGROUPINFO INTEGER
                );
                CREATE TABLE ZWAMEDIAITEM (
                    Z_PK INTEGER PRIMARY KEY,
                    ZTITLE TEXT,
                    ZMEDIALOCALPATH TEXT,
                    ZMEDIAURL TEXT,
                    ZXMPPTHUMBPATH TEXT,
                    ZTHUMBNAILLOCALPATH TEXT,
                    ZMETADATA BLOB
                );
                CREATE TABLE ZWAMESSAGEDATAITEM (
                    Z_PK INTEGER PRIMARY KEY,
                    ZMESSAGE INTEGER,
                    ZINDEX INTEGER,
                    ZTITLE TEXT,
                    ZSUMMARY TEXT,
                    ZMATCHEDTEXT TEXT,
                    ZTHUMBNAILPATH TEXT
                );
                CREATE TABLE ZWAMESSAGE (
                    Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZSTANZAID TEXT,
                    ZISFROMME INTEGER, ZFROMJID TEXT, ZMESSAGEDATE REAL,
                    ZMESSAGETYPE INTEGER, ZTEXT TEXT, ZPUSHNAME TEXT,
                    ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER
                );
                INSERT INTO ZWACHATSESSION VALUES (1, 'partner@s.whatsapp.net', 'Partner', NULL);
            """)

    with sqlite3.connect(old_db) as conn:
        # Message 1: Disk thumbnail referenced via ZXMPPTHUMBPATH
        desc_bytes = b"iOS page snippet"
        metadata_blob1 = b"\x1a" + bytes([len(desc_bytes)]) + desc_bytes
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZTITLE, ZMEDIALOCALPATH, ZMEDIAURL, ZXMPPTHUMBPATH, ZTHUMBNAILLOCALPATH, ZMETADATA) VALUES (1, 'iOS Title', NULL, 'https://example.org', 'Thumbnail/preview1.jpg', NULL, ?)",
            (metadata_blob1,),
        )
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZSTANZAID, ZISFROMME, ZFROMJID, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT, ZPUSHNAME, ZMEDIAITEM, ZGROUPMEMBER)
            VALUES (1, 1, 'ios_link_disk', 0, 'partner@s.whatsapp.net', 1000.0, 7, 'https://example.org', 'Partner', 1, NULL)
        """)

        # Message 2: Embedded JPEG thumbnail inside ZMETADATA
        metadata_blob2 = b"\x1a\x07Snippet" + embedded_thumb_bytes
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZTITLE, ZMEDIALOCALPATH, ZMEDIAURL, ZXMPPTHUMBPATH, ZTHUMBNAILLOCALPATH, ZMETADATA) VALUES (2, 'Embedded Preview', NULL, 'https://embedded.org', NULL, NULL, ?)",
            (metadata_blob2,),
        )
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZSTANZAID, ZISFROMME, ZFROMJID, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT, ZPUSHNAME, ZMEDIAITEM, ZGROUPMEMBER)
            VALUES (2, 1, 'ios_link_embedded', 0, 'partner@s.whatsapp.net', 1001.0, 7, 'https://embedded.org', 'Partner', 2, NULL)
        """)

        # Message 3: Plain text type 0 with URL, populated via ZWAMESSAGEDATAITEM without thumbnail
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZSTANZAID, ZISFROMME, ZFROMJID, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT, ZPUSHNAME, ZMEDIAITEM, ZGROUPMEMBER)
            VALUES (3, 1, 'ios_link_mdi', 0, 'partner@s.whatsapp.net', 1002.0, 0, 'Check https://apple.com', 'Partner', 3, NULL)
        """)
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZTITLE, ZMEDIALOCALPATH, ZMEDIAURL, ZXMPPTHUMBPATH, ZTHUMBNAILLOCALPATH, ZMETADATA) VALUES (3, NULL, NULL, NULL, NULL, NULL, NULL)"
        )
        conn.execute("""
            INSERT INTO ZWAMESSAGEDATAITEM (Z_PK, ZMESSAGE, ZINDEX, ZTITLE, ZSUMMARY, ZMATCHEDTEXT, ZTHUMBNAILPATH)
            VALUES (1, 3, 0, 'Apple', 'Think Different', 'https://apple.com', NULL)
        """)

        # Message 4: Resolved via archive_copies mapping
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZTITLE, ZMEDIALOCALPATH, ZMEDIAURL, ZXMPPTHUMBPATH, ZTHUMBNAILLOCALPATH, ZMETADATA) VALUES (4, 'AC Title', NULL, 'https://archive.org', 'Thumbnail/ac.jpg', NULL, NULL)"
        )
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZSTANZAID, ZISFROMME, ZFROMJID, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT, ZPUSHNAME, ZMEDIAITEM, ZGROUPMEMBER)
            VALUES (4, 1, 'ios_link_ac', 0, 'partner@s.whatsapp.net', 1003.0, 7, 'https://archive.org', 'Partner', 4, NULL)
        """)
        conn.commit()

    extracted = preservation.diff_and_extract_ios(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    assert len(extracted) == 4
    by_key = {r["stanza_id"]: r for r in extracted}

    # Verify Message 1 (disk thumbnail)
    meta1 = json.loads(by_key["ios_link_disk"]["service_metadata"])
    assert "link_preview" in meta1
    assert meta1["link_preview"]["title"] == "iOS Title"
    assert meta1["link_preview"]["desc"] == "iOS page snippet"
    assert meta1["link_preview"]["url"] == "https://example.org"
    assert "thumbnail_b64" in meta1["link_preview"]
    assert base64.b64decode(meta1["link_preview"]["thumbnail_b64"]) == disk_thumb_bytes

    # Verify Message 2 (embedded protobuf thumbnail)
    meta2 = json.loads(by_key["ios_link_embedded"]["service_metadata"])
    assert "link_preview" in meta2
    assert meta2["link_preview"]["title"] == "Embedded Preview"
    assert meta2["link_preview"]["url"] == "https://embedded.org"
    assert "thumbnail_b64" in meta2["link_preview"]
    assert base64.b64decode(meta2["link_preview"]["thumbnail_b64"]) == embedded_thumb_bytes

    # Verify Message 3 (ZWAMESSAGEDATAITEM fallback without thumbnail)
    meta3 = json.loads(by_key["ios_link_mdi"]["service_metadata"])
    assert "link_preview" in meta3
    assert meta3["link_preview"]["title"] == "Apple"
    assert meta3["link_preview"]["desc"] == "Think Different"
    assert meta3["link_preview"]["url"] == "https://apple.com"
    assert "thumbnail_b64" not in meta3["link_preview"]

    # Verify Message 4 (archive_copies thumbnail resolution)
    meta4 = json.loads(by_key["ios_link_ac"]["service_metadata"])
    assert "link_preview" in meta4
    assert meta4["link_preview"]["title"] == "AC Title"
    assert meta4["link_preview"]["url"] == "https://archive.org"
    assert "thumbnail_b64" in meta4["link_preview"]
    assert base64.b64decode(meta4["link_preview"]["thumbnail_b64"]) == ac_thumb_bytes
