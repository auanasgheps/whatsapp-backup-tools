import json
import sqlite3
from pathlib import Path

from wab_viewer.main import (
    _hydrate_link_preview_rows,
    _stream_fts_rows,
    create_app,
)


def _encode_varint(value: int) -> bytes:
    buf = bytearray()
    while value > 0x7F:
        buf.append((value & 0x7F) | 0x80)
        value >>= 7
    buf.append(value)
    return bytes(buf)


def _encode_field(field_num: int, wire_type: int, payload: bytes) -> bytes:
    tag = (field_num << 3) | wire_type
    return _encode_varint(tag) + payload


def _make_ios_poll_blob(question: str, options: list[str]) -> bytes:
    # Field 8: PollCreationMessage
    # Subfield 2: Question string
    # Subfield 3 (repeated): Options with subfield 1 = option text
    # Subfield 4: selectable count (0 = multiple)
    poll_payload = bytearray()
    poll_payload += _encode_field(2, 2, _encode_varint(len(question.encode())) + question.encode())
    for opt in options:
        opt_bytes = _encode_field(1, 2, _encode_varint(len(opt.encode())) + opt.encode())
        poll_payload += _encode_field(3, 2, _encode_varint(len(opt_bytes)) + opt_bytes)
    poll_payload += _encode_field(4, 0, _encode_varint(0))

    return bytes(_encode_field(8, 2, _encode_varint(len(poll_payload)) + bytes(poll_payload)))


def test_ios_link_preview_embedded_thumbnail(tmp_path: Path):
    """Test that iOS links with embedded JPEG in ZMETADATA are flagged with has_thumbnail."""
    jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
    meta_blob = b"preamble" + jpeg_bytes + b"postamble"

    rows = [
        {
            "msg_id": 42,
            "media_type": "link",
            "text_body": "Check out https://example.com/article",
            "_raw_metadata": meta_blob,
            "has_thumbnail": 0,
        }
    ]

    _hydrate_link_preview_rows(rows, "ios")
    assert rows[0]["has_thumbnail"] == 1
    assert rows[0]["link_url"] == "https://example.com/article"


def test_ios_poll_fts_indexes_options_even_when_question_in_text(tmp_path: Path):
    """Test that _stream_fts_rows indexes all poll options on iOS even when text_body is non-empty."""
    poll_blob = _make_ios_poll_blob("Where to eat?", ["Pizza Place", "Sushi Bar", "Burger Joint"])

    cursor = [
        {
            "rowid": 101,
            "chat_id": "1",
            "chat_type": "group",
            "timestamp_ms": 1700000000000,
            "text_body": "Where to eat?",
            "message_type": 46,
            "poll_receipt_info": poll_blob,
            "media_file": None,
        }
    ]

    cache_db = tmp_path / "cache.db"
    conn = sqlite3.connect(cache_db)
    conn.execute(
        "CREATE TABLE message_index (rowid INTEGER, chat_id TEXT, chat_type TEXT, timestamp_ms INTEGER)"
    )
    conn.execute("CREATE TABLE message_index_fts (rowid INTEGER, text_body TEXT)")

    _stream_fts_rows(cursor, conn)

    fts_rows = conn.execute("SELECT rowid, text_body FROM message_index_fts").fetchall()
    assert len(fts_rows) == 1
    indexed_text = fts_rows[0][1]
    assert "Where to eat?" in indexed_text
    assert "Pizza Place" in indexed_text
    assert "Sushi Bar" in indexed_text
    assert "Burger Joint" in indexed_text
    conn.close()


def test_reaction_details_preserved_message(tmp_path: Path):
    """Test that /api/reaction_details/<message_id> resolves negative preserved message IDs."""
    output_root = tmp_path / "output"
    output_root.mkdir()
    db_dir = output_root / "Whatsapp Databases"
    db_dir.mkdir()

    # Create dummy wa db (iOS)
    wa_db = db_dir / "ChatStorage.sqlite"
    wa_conn = sqlite3.connect(wa_db)
    wa_conn.execute("CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZMESSAGETYPE INTEGER)")
    wa_conn.commit()
    wa_conn.close()

    # Create preserved db
    pres_db = db_dir / "preserved_messages.db"
    p_conn = sqlite3.connect(pres_db)
    p_conn.execute(
        """CREATE TABLE preserved_messages (
            rowid INTEGER PRIMARY KEY,
            chat_id TEXT NOT NULL,
            stanza_id TEXT NOT NULL,
            reactions TEXT,
            from_me INTEGER,
            sender TEXT
        )"""
    )
    p_conn.execute(
        "INSERT INTO preserved_messages (rowid, chat_id, stanza_id, reactions, from_me, sender) VALUES (5, '123@s.whatsapp.net', 'STANZA_99', '❤️,👍', 0, 'Alice')"
    )
    p_conn.commit()
    p_conn.close()

    app = create_app(output_root=output_root)
    client = app.test_client()

    resp = client.get("/api/reaction_details/-5")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["available"] is True
    assert data["total"] == 2
    assert data["reactors"] == [
        {"name": "Alice", "emoji": "❤️", "from_me": 0},
        {"name": "Alice", "emoji": "👍", "from_me": 0},
    ]


def test_thumbnail_preserved_message(tmp_path: Path):
    """Test that /api/thumbnail/<msg_id> serves archived image bytes for negative preserved message IDs."""
    output_root = tmp_path / "output"
    output_root.mkdir()
    db_dir = output_root / "Whatsapp Databases"
    db_dir.mkdir()

    media_file = output_root / "Media" / "preserved_thumb.jpg"
    media_file.parent.mkdir(parents=True, exist_ok=True)
    fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
    media_file.write_bytes(fake_jpeg)

    wa_db = db_dir / "ChatStorage.sqlite"
    wa_conn = sqlite3.connect(wa_db)
    wa_conn.execute("CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZMESSAGETYPE INTEGER)")
    wa_conn.commit()
    wa_conn.close()

    pres_db = db_dir / "preserved_messages.db"
    p_conn = sqlite3.connect(pres_db)
    p_conn.execute(
        """CREATE TABLE preserved_messages (
            rowid INTEGER PRIMARY KEY,
            chat_id TEXT NOT NULL,
            stanza_id TEXT NOT NULL,
            archive_path TEXT
        )"""
    )
    p_conn.execute(
        "INSERT INTO preserved_messages (rowid, chat_id, stanza_id, archive_path) VALUES (12, '123@s.whatsapp.net', 'STANZA_12', 'Media/preserved_thumb.jpg')"
    )
    p_conn.commit()
    p_conn.close()

    app = create_app(output_root=output_root)
    client = app.test_client()

    resp = client.get("/api/thumbnail/-12")
    assert resp.status_code == 200
    assert resp.data == fake_jpeg
