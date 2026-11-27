"""Tests for Phase 1 offline parity features (pinned chats, unread, disappearing, business)."""

from __future__ import annotations

import base64
import sqlite3

from tests.conftest import make_android_db, make_archive_db, make_ios_db
from tests.fixtures.protobuf_factory import encode_varint
from wab_viewer import main as viewer
from wab_viewer.protobuf import _parse_ios_chat_session_meta


def _pb_varint_field(field_num: int, val: int) -> bytes:
    return encode_varint((field_num << 3) | 0) + encode_varint(val)


def test_parse_ios_chat_session_meta():
    blob = _pb_varint_field(2, 604800)
    b64 = base64.b64encode(blob).decode("ascii")
    ephem, is_pinned, pinned_time = _parse_ios_chat_session_meta(b64)
    assert ephem == 604800
    assert is_pinned is False
    assert pinned_time == 0

    assert _parse_ios_chat_session_meta("") == (0, False, 0)
    assert _parse_ios_chat_session_meta("!!!not-base64!!!") == (0, False, 0)


def test_android_phase1_chats_and_chat_info(tmp_path):
    db_dir = tmp_path / "Whatsapp Databases"
    db_dir.mkdir(parents=True, exist_ok=True)
    wa_path = db_dir / "msgstore.db"
    archive_path = tmp_path / ".wa_media_archiver.db"
    cs_path = db_dir / "chatsettingsbackup.db"

    wa_conn = make_android_db(wa_path)
    wa_conn.execute("ALTER TABLE chat ADD COLUMN unseen_message_count INTEGER")
    wa_conn.execute("ALTER TABLE chat ADD COLUMN ephemeral_expiration INTEGER")
    wa_conn.execute("ALTER TABLE chat ADD COLUMN business_chat_state INTEGER")
    wa_conn.execute(
        "CREATE TABLE message_system_business_state ("
        "message_row_id INTEGER PRIMARY KEY, privacy_message_type INTEGER, "
        "business_name TEXT, is_deprecated INTEGER)"
    )

    wa_conn.execute(
        "INSERT INTO jid (_id, user, raw_string) VALUES (1, '15550101', '15550101@s.whatsapp.net')"
    )
    wa_conn.execute(
        "INSERT INTO jid (_id, user, raw_string) VALUES (2, '15550102', '15550102@s.whatsapp.net')"
    )
    # Chat 1: older timestamp, but pinned + unread + disappearing
    wa_conn.execute(
        "INSERT INTO chat (_id, jid_row_id, hidden, sort_timestamp, unseen_message_count, ephemeral_expiration) "
        "VALUES (1, 1, 0, 1000, 3, 86400)"
    )
    wa_conn.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data) "
        "VALUES (10, 1, 0, 'K1', 0, 1000, 0, 'Hello')"
    )
    # Chat 2: newer timestamp, not pinned, business account
    wa_conn.execute(
        "INSERT INTO chat (_id, jid_row_id, hidden, sort_timestamp, unseen_message_count, ephemeral_expiration, business_chat_state) "
        "VALUES (2, 2, 0, 5000, 0, 0, 0)"
    )
    wa_conn.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data) "
        "VALUES (20, 2, 0, 'K2', 0, 5000, 0, 'Order ready')"
    )
    wa_conn.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data) "
        "VALUES (21, 2, 0, 'K3', 0, 4000, 7, '')"
    )
    wa_conn.execute(
        "INSERT INTO message_system_business_state (message_row_id, privacy_message_type, business_name, is_deprecated) "
        "VALUES (21, 1, 'Acme Corp', 0)"
    )
    wa_conn.commit()
    wa_conn.close()

    cs_conn = sqlite3.connect(str(cs_path))
    cs_conn.execute(
        "CREATE TABLE settings (_id INTEGER PRIMARY KEY, jid TEXT, pinned INTEGER, pinned_time INTEGER)"
    )
    cs_conn.execute(
        "INSERT INTO settings (_id, jid, pinned, pinned_time) VALUES (1, '15550101@s.whatsapp.net', 1, 9000)"
    )
    cs_conn.execute(
        "INSERT INTO settings (_id, jid, pinned, pinned_time) VALUES (2, '15550102@s.whatsapp.net', 0, 0)"
    )
    cs_conn.commit()
    cs_conn.close()

    arch_conn = make_archive_db(archive_path)
    arch_conn.execute(
        "INSERT INTO contacts (number, display_name, folder) VALUES ('15550101', 'Alice', 'Alice (+15550101)')"
    )
    arch_conn.execute(
        "INSERT INTO contacts (number, display_name, folder) VALUES ('15550102', 'Acme Support', 'Acme Support (+15550102)')"
    )
    arch_conn.commit()
    arch_conn.close()

    app = viewer.create_app(tmp_path, rescan=False)
    app.config["TESTING"] = True
    with app.test_client() as client:
        chats = client.get("/api/chats").get_json()
        assert len(chats) == 2
        # Pinned chat sorts first despite older timestamp
        assert chats[0]["id"] == "15550101"
        assert chats[0]["is_pinned"] is True
        assert chats[0]["unread_count"] == 3
        assert chats[0]["ephemeral_duration"] == 86400
        assert chats[0]["has_preservation"] is False

        assert chats[1]["id"] == "15550102"
        assert chats[1]["is_pinned"] is False
        assert chats[1]["unread_count"] == 0

        info_biz = client.get("/api/chat-info?chat_id=15550102&chat_type=contact").get_json()
        assert info_biz["is_business"] is True
        assert info_biz["business_name"] == "Acme Corp"
        assert info_biz["ephemeral_duration"] == 0

        info_ephem = client.get("/api/chat-info?chat_id=15550101&chat_type=contact").get_json()
        assert info_ephem["is_business"] is False
        assert info_ephem["ephemeral_duration"] == 86400


def test_ios_phase1_chats_and_chat_info(tmp_path):
    db_dir = tmp_path / "Whatsapp Databases"
    db_dir.mkdir(parents=True, exist_ok=True)
    wa_path = db_dir / "ChatStorage.sqlite"
    cv_path = db_dir / "ContactsV2.sqlite"
    archive_path = tmp_path / ".wa_media_archiver.db"

    wa_conn = make_ios_db(wa_path)
    wa_conn.execute("ALTER TABLE ZWACHATSESSION ADD COLUMN ZUNREADCOUNT INTEGER DEFAULT 0")

    pb_b64 = base64.b64encode(_pb_varint_field(2, 7776000)).decode("ascii")

    wa_conn.execute(
        "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN, ZUNREADCOUNT, ZLASTMESSAGETEXT) "
        "VALUES (1, '15550103@s.whatsapp.net', 'Bob', 0, 2, ?)",
        (pb_b64,),
    )
    wa_conn.execute(
        "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT, ZSTANZAID) "
        "VALUES (1, 1, 0, 700000000.0, 0, 'Hi Bob', 'ST1')"
    )
    wa_conn.commit()
    wa_conn.close()

    cv_conn = sqlite3.connect(str(cv_path))
    cv_conn.execute(
        "CREATE TABLE ZWAADDRESSBOOKCONTACT ("
        "Z_PK INTEGER PRIMARY KEY, ZWHATSAPPID TEXT, ZLID TEXT, "
        "ZFULLNAME TEXT, ZBUSINESSNAME TEXT, ZDISAPPEARINGMODEDURATION INTEGER)"
    )
    cv_conn.execute(
        "INSERT INTO ZWAADDRESSBOOKCONTACT (Z_PK, ZWHATSAPPID, ZFULLNAME, ZBUSINESSNAME, ZDISAPPEARINGMODEDURATION) "
        "VALUES (1, '15550103@s.whatsapp.net', 'Bob Bakery', 'Bob Bakery LLC', 0)"
    )
    cv_conn.commit()
    cv_conn.close()

    arch_conn = make_archive_db(archive_path)
    arch_conn.execute(
        "INSERT INTO contacts (number, display_name, folder) VALUES ('15550103', 'Bob Bakery', 'Bob Bakery (+15550103)')"
    )
    arch_conn.commit()
    arch_conn.close()

    app = viewer.create_app(tmp_path, rescan=False)
    app.config["TESTING"] = True
    with app.test_client() as client:
        chats = client.get("/api/chats").get_json()
        assert len(chats) == 1
        assert chats[0]["unread_count"] == 2
        assert chats[0]["ephemeral_duration"] == 7776000
        assert chats[0]["is_pinned"] is False

        info = client.get("/api/chat-info?chat_id=1&chat_type=contact").get_json()
        assert info["is_business"] is True
        assert info["business_name"] == "Bob Bakery LLC"
        assert info["ephemeral_duration"] == 7776000
