"""Shared fixtures and test helpers for wab_viewer test suites."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tests.conftest import (
    make_android_db,
    make_archive_db,
    seed_android_db,
)
from wab_viewer import db_cache as viewer_db_cache
from wab_viewer import main as viewer


def _get_app_js() -> str:
    from wab_viewer.chat_viewer import builder

    return builder.assemble_app_js()


def make_cache_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(viewer_db_cache.CACHE_SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def _make_android_app(tmp_path: Path, seed_fn=None):
    wa_path = tmp_path / "msgstore.db"
    archive_path = tmp_path / ".wa_media_archiver.db"
    wa_conn = make_android_db(wa_path)
    archive_conn = make_archive_db(archive_path)
    seed_android_db(wa_conn, archive_conn)
    if seed_fn:
        seed_fn(wa_conn, archive_conn, tmp_path)
    wa_conn.close()
    archive_conn.close()
    app = viewer.create_app(tmp_path, rescan=False)
    app.config["TESTING"] = True
    return app


def _seed_android_db_large(wa_conn: sqlite3.Connection):
    """Insert 10 chats × 50 messages each for performance and routing tests.

    Timestamps are Unix epoch milliseconds (like WhatsApp's message.timestamp column).
    For chat_id=1 the range is 1700000000000..170000049000.
    """
    for chat_id in range(1, 11):
        jid_row_id = chat_id
        wa_conn.execute(
            "INSERT OR IGNORE INTO jid (_id, user) VALUES (?, ?)",
            (jid_row_id, str(chat_id * 111111111)),
        )
        wa_conn.execute(
            "INSERT OR IGNORE INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) "
            "VALUES (?, ?, NULL, 0, ?, ?)",
            (chat_id, jid_row_id, 1700000000000 + chat_id * 1000, chat_id * 50),
        )
        for i in range(50):
            msg_id = chat_id * 100 + i
            ts = 1700000000000 + (chat_id - 1) * 50 * 1000 + i * 1000
            wa_conn.execute(
                "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                (msg_id, chat_id, i % 2, jid_row_id, ts, f"msg {i}"),
            )
    wa_conn.commit()


def seed_android_db_with_media(wa_conn, archive_conn, tmp_path: Path) -> str:
    """Add one archived image message to the seeded chat (jid 1, chat 10)."""
    wa_conn.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
        "VALUES (2, 10, 1, NULL, 1700000001000, NULL, 1)"
    )
    wa_conn.execute(
        "INSERT INTO message_media (message_row_id, file_path, media_name) "
        "VALUES (2, 'Media/Images/photo.jpg', 'photo.jpg')"
    )
    wa_conn.commit()

    media_file = tmp_path / "Alice (00123456789)" / "photo.jpg"
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(b"\xff\xd8\xff")

    archive_path = "Alice (00123456789)/photo.jpg"
    archive_conn.execute(
        "INSERT INTO files (original_path, md5) VALUES ('Media/Images/photo.jpg', x'deadbeef')"
    )
    archive_conn.execute(
        "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Media/Images/photo.jpg', ?)",
        (archive_path,),
    )
    archive_conn.commit()
    return archive_path
