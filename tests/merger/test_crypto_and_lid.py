"""Unit tests for crypto key resolution, unpack validation, and post-restore LID migration.

Tests resolve_e2e_key and migrate_restored_backup_to_lid across split chats,
account_jid_row_id linking, and companion device sender re-pointing with synthetic data.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from wab_archiver import increment_merger

logger = logging.getLogger(__name__)


class TestKeyResolution:
    """Tests for resolve_e2e_key across various key file formats."""

    @pytest.fixture(autouse=True)
    def mock_key_factory(self):
        class MockKeyFactory:
            @staticmethod
            def new(candidate):
                if isinstance(candidate, str) and os.path.isfile(candidate):
                    try:
                        data = Path(candidate).read_bytes()
                        if len(data) == 32:
                            return "MOCK_KEY_32"
                    except OSError:
                        pass
                if isinstance(candidate, (bytes, str)) and len(candidate) in (32, 64):
                    return f"MOCK_KEY_{len(candidate)}"
                return None

        mock_module = MagicMock()
        mock_module.KeyFactory = MockKeyFactory
        with patch.dict(
            "sys.modules",
            {
                "wa_crypt_tools": MagicMock(),
                "wa_crypt_tools.lib": MagicMock(),
                "wa_crypt_tools.lib.key": MagicMock(),
                "wa_crypt_tools.lib.key.keyfactory": mock_module,
            },
        ):
            yield

    def test_resolve_key_from_binary_file(self, tmp_path) -> None:
        key_bytes = b"\x01\x23\x45\x67" * 8  # 32 bytes
        key_file = tmp_path / "msgstore.key"
        key_file.write_bytes(key_bytes)

        resolved = increment_merger.resolve_e2e_key(str(key_file), logger)
        assert resolved == "MOCK_KEY_32"

    def test_resolve_key_from_hex_text_file(self, tmp_path) -> None:
        hex_str = "0123456789abcdef" * 4  # 64 hex chars = 32 bytes
        key_file = tmp_path / "key.txt"
        key_file.write_text(f"  \n{hex_str}\n  ", encoding="utf-8")

        resolved = increment_merger.resolve_e2e_key(str(key_file), logger)
        assert resolved == "MOCK_KEY_64"

    def test_resolve_key_invalid_lengths_raise_or_return_none(self, tmp_path) -> None:
        # 16 bytes is invalid for WhatsApp 256-bit AES GCM key
        short_file = tmp_path / "short.key"
        short_file.write_bytes(b"\x00" * 16)
        with pytest.raises(ValueError, match="Could not load decryption key"):
            increment_merger.resolve_e2e_key(str(short_file), logger)

        # Invalid hex characters or short string
        bad_hex_file = tmp_path / "bad_hex.txt"
        bad_hex_file.write_text("ZZZZ", encoding="utf-8")
        with pytest.raises(ValueError, match="Could not load decryption key"):
            increment_merger.resolve_e2e_key(str(bad_hex_file), logger)

        # Non-existent file
        with pytest.raises(ValueError, match="Could not load decryption key"):
            increment_merger.resolve_e2e_key(str(tmp_path / "non_existent.key"), logger)


class TestPostRestoreLidMigration:
    """Tests for migrate_restored_backup_to_lid Step A, B, C, D."""

    def _setup_migration_db(self, db_path) -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE jid (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT NOT NULL,
                server TEXT NOT NULL,
                agent INTEGER DEFAULT 0,
                device INTEGER DEFAULT 0,
                type INTEGER DEFAULT 0,
                raw_string TEXT
            );

            CREATE TABLE jid_map (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                jid_row_id INTEGER NOT NULL,
                lid_row_id INTEGER NOT NULL
            );

            CREATE TABLE chat (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                jid_row_id INTEGER NOT NULL,
                hidden INTEGER DEFAULT 0,
                account_jid_row_id INTEGER DEFAULT 0,
                subject TEXT,
                created_timestamp INTEGER DEFAULT 0,
                sort_timestamp INTEGER DEFAULT 0,
                last_message_row_id INTEGER,
                display_message_row_id INTEGER
            );

            CREATE TABLE message (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL,
                key_id TEXT NOT NULL,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                message_type INTEGER DEFAULT 0,
                text_data TEXT
            );

            CREATE TABLE message_quoted (
                message_row_id INTEGER PRIMARY KEY,
                chat_row_id INTEGER NOT NULL,
                parent_message_chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL,
                sender_jid_row_id INTEGER,
                key_id TEXT NOT NULL,
                timestamp INTEGER,
                message_type INTEGER,
                text_data TEXT
            );

            CREATE TABLE receipt_user (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_row_id INTEGER NOT NULL,
                receipt_user_jid_row_id INTEGER NOT NULL,
                receipt_timestamp INTEGER,
                read_timestamp INTEGER,
                played_timestamp INTEGER
            );

            CREATE TABLE message_add_on (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_row_id INTEGER NOT NULL,
                chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL,
                sender_jid_row_id INTEGER,
                key_id TEXT NOT NULL
            );
        """)
        return conn

    def test_step_a_links_account_jid_row_id(self, tmp_path) -> None:
        conn = self._setup_migration_db(tmp_path / "test_step_a.db")
        cur = conn.cursor()

        # Insert phone JID and LID JID
        cur.execute(
            "INSERT INTO jid (user, server, raw_string) VALUES ('5550100', 's.whatsapp.net', '5550100@s.whatsapp.net')"
        )
        phone_jid_id = cur.lastrowid
        cur.execute(
            "INSERT INTO jid (user, server, raw_string) VALUES ('9990100', 'lid', '9990100@lid')"
        )
        lid_jid_id = cur.lastrowid

        # Insert mapping
        cur.execute(
            "INSERT INTO jid_map (jid_row_id, lid_row_id) VALUES (?, ?)", (phone_jid_id, lid_jid_id)
        )

        # Insert chat with account_jid_row_id = 0
        cur.execute(
            "INSERT INTO chat (jid_row_id, account_jid_row_id) VALUES (?, 0)", (phone_jid_id,)
        )
        chat_id = cur.lastrowid
        conn.commit()

        increment_merger.migrate_restored_backup_to_lid(conn, logger)
        conn.commit()

        updated_chat = cur.execute(
            "SELECT account_jid_row_id FROM chat WHERE _id = ?", (chat_id,)
        ).fetchone()
        assert updated_chat[0] == lid_jid_id
        conn.close()

    def test_step_b_consolidates_split_phone_and_lid_chats(self, tmp_path) -> None:
        conn = self._setup_migration_db(tmp_path / "test_step_b.db")
        cur = conn.cursor()

        cur.execute(
            "INSERT INTO jid (user, server, raw_string) VALUES ('5550100', 's.whatsapp.net', '5550100@s.whatsapp.net')"
        )
        phone_jid_id = cur.lastrowid
        cur.execute(
            "INSERT INTO jid (user, server, raw_string) VALUES ('9990100', 'lid', '9990100@lid')"
        )
        lid_jid_id = cur.lastrowid

        cur.execute(
            "INSERT INTO jid_map (jid_row_id, lid_row_id) VALUES (?, ?)", (phone_jid_id, lid_jid_id)
        )

        cur.execute(
            "INSERT INTO chat (jid_row_id, sort_timestamp) VALUES (?, 1000)", (phone_jid_id,)
        )
        phone_chat_id = cur.lastrowid

        cur.execute("INSERT INTO chat (jid_row_id, sort_timestamp) VALUES (?, 2000)", (lid_jid_id,))
        lid_chat_id = cur.lastrowid

        # Put a message in the LID chat
        cur.execute(
            "INSERT INTO message (chat_row_id, from_me, key_id, text_data, timestamp) VALUES (?, 0, 'SPLIT_MSG_1', 'Hello in LID chat', 2000)",
            (lid_chat_id,),
        )
        msg_id = cur.lastrowid
        conn.commit()

        increment_merger.migrate_restored_backup_to_lid(conn, logger)
        conn.commit()

        # The message should be repointed to phone_chat_id
        msg_row = cur.execute("SELECT chat_row_id FROM message WHERE _id = ?", (msg_id,)).fetchone()
        assert msg_row[0] == phone_chat_id

        # The duplicate LID chat should be hidden
        lid_chat_row = cur.execute(
            "SELECT hidden FROM chat WHERE _id = ?", (lid_chat_id,)
        ).fetchone()
        assert lid_chat_row[0] == 1
        conn.close()

    def test_step_d_repoints_companion_senders_to_canonical_primary_jid(self, tmp_path) -> None:
        conn = self._setup_migration_db(tmp_path / "test_step_d.db")
        cur = conn.cursor()

        # Primary JID (device = 0)
        cur.execute(
            "INSERT INTO jid (user, server, device, raw_string) VALUES ('5550999', 's.whatsapp.net', 0, '5550999@s.whatsapp.net')"
        )
        pri_jid_id = cur.lastrowid

        # Companion JID (device = 1)
        cur.execute(
            "INSERT INTO jid (user, server, device, raw_string) VALUES ('5550999', 's.whatsapp.net', 1, '5550999.1:0@s.whatsapp.net')"
        )
        comp_jid_id = cur.lastrowid

        cur.execute("INSERT INTO chat (jid_row_id) VALUES (?)", (pri_jid_id,))
        chat_id = cur.lastrowid

        # Message with companion sender_jid_row_id
        cur.execute(
            "INSERT INTO message (chat_row_id, from_me, key_id, sender_jid_row_id, text_data, timestamp) "
            "VALUES (?, 0, 'COMP_MSG_1', ?, 'Sent from desktop companion', 1729000100)",
            (chat_id, comp_jid_id),
        )
        msg_id = cur.lastrowid

        # Add-on with companion sender
        cur.execute(
            "INSERT INTO message_add_on (message_row_id, chat_row_id, from_me, sender_jid_row_id, key_id) "
            "VALUES (?, ?, 0, ?, 'ADDON_COMP_1')",
            (msg_id, chat_id, comp_jid_id),
        )

        # Receipt with companion user
        cur.execute(
            "INSERT INTO receipt_user (message_row_id, receipt_user_jid_row_id, read_timestamp) VALUES (?, ?, 1729000200)",
            (msg_id, comp_jid_id),
        )
        conn.commit()

        increment_merger.migrate_restored_backup_to_lid(conn, logger)
        conn.commit()

        # Message sender should now be primary JID
        msg_res = cur.execute(
            "SELECT sender_jid_row_id FROM message WHERE _id = ?", (msg_id,)
        ).fetchone()
        assert msg_res[0] == pri_jid_id

        # Add-on sender should now be primary JID
        ao_res = cur.execute(
            "SELECT sender_jid_row_id FROM message_add_on WHERE message_row_id = ?", (msg_id,)
        ).fetchone()
        assert ao_res[0] == pri_jid_id

        # Receipt user should now be primary JID
        rcpt_res = cur.execute(
            "SELECT receipt_user_jid_row_id FROM receipt_user WHERE message_row_id = ?", (msg_id,)
        ).fetchone()
        assert rcpt_res[0] == pri_jid_id

        conn.close()
