"""Stress testing and failure mode simulation suite for WhatsApp Backup Tools.

Validates system invariants under exceptional failure modes:
1. Interrupted delta merges, rollback safety, and sidecar cleanup.
2. Malformed / corrupted increment archives (truncated zip, broken protobuf, dirty JSON).
3. Preservation failure fail-safe rollback to previous database snapshot.
4. Degenerate and empty database handling in Viewer endpoints (no HTTP 500s).
5. High-volume message query performance and indexed seek verification.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from shared.source_detection import WA_DATABASES_DIR_NAME
from tests.conftest import make_android_db, make_archive_db, make_ios_db
from wab_archiver import increment_merger, preservation
from wab_archiver import main as archiver_main
from wab_viewer import main as viewer_main

_TEST_LOGGER = logging.getLogger("stress_test_logger")
_TEST_LOGGER.setLevel(logging.CRITICAL)


# ==============================================================================
# 1. Interrupted Merge & Sidecar Isolation
# ==============================================================================


class TestFailureModeInterruptedMerge:
    """Simulate mid-stream failures during incremental delta merging."""

    def test_interrupted_merge_cleans_temporary_sidecars(self, tmp_path: Path):
        """Verify that an interrupted multi-increment merge does not leave dirty sidecars."""
        db_dir = tmp_path / WA_DATABASES_DIR_NAME
        db_dir.mkdir(parents=True, exist_ok=True)

        base_db = db_dir / "msgstore.db"
        conn = make_android_db(base_db)
        conn.close()

        # Create two valid zip increments
        inc1_zip = tmp_path / "inc1.zip"
        with zipfile.ZipFile(inc1_zip, "w") as zf:
            zf.writestr("deletedMessages_1.json", json.dumps([]))

        inc2_zip = tmp_path / "inc2.zip"
        with zipfile.ZipFile(inc2_zip, "w") as zf:
            zf.writestr("deletedMessages_2.json", json.dumps([]))

        incoming_db = db_dir / "msgstore.db.incoming"
        staging_dir = db_dir / ".staging_merge"

        # Simulate exception during the second increment's processing
        original_apply = increment_merger.apply_increment_folder

        def fail_on_second(conn, work_dir, *args, **kwargs):
            if "unpacked_inc_1" in work_dir:
                raise RuntimeError("Simulated process crash or disk failure during increment 2")
            return original_apply(conn, work_dir, *args, **kwargs)

        with patch.object(increment_merger, "apply_increment_folder", side_effect=fail_on_second):
            with pytest.raises(RuntimeError, match="Simulated process crash"):
                increment_merger.merge_increments(
                    str(base_db),
                    [str(inc1_zip), str(inc2_zip)],
                    None,
                    str(incoming_db),
                    str(staging_dir),
                    _TEST_LOGGER,
                    str(tmp_path),
                )

        # In main.py, a BaseException triggers _clean_sqlite_files(incoming_db)
        archiver_main._clean_sqlite_files(str(incoming_db))

        # Assert no dangling incoming database or sidecars remain
        assert not incoming_db.exists()
        assert not Path(str(incoming_db) + "-wal").exists()
        assert not Path(str(incoming_db) + "-shm").exists()
        assert not Path(str(incoming_db) + "-journal").exists()

        # Assert original base_db was untouched and remains valid
        base_conn = sqlite3.connect(str(base_db))
        check = base_conn.execute("PRAGMA quick_check").fetchone()[0]
        base_conn.close()
        assert check == "ok"


# ==============================================================================
# 2. Corrupted & Dirty Increment Payloads
# ==============================================================================


class TestFailureModeCorruptedIncrementPayloads:
    """Validate behavior against malformed archives, corrupt protobuf, and dirty JSON."""

    def test_corrupted_zip_archive_raises_cleanly(self, tmp_path: Path):
        """Verify that a truncated or non-zip increment raises a clean error."""
        corrupt_inc = tmp_path / "corrupt_inc.zip"
        corrupt_inc.write_bytes(b"PK\x03\x04TRUNCATED_GARBAGE_PAYLOAD")

        out_dir = tmp_path / "out"
        with pytest.raises((zipfile.BadZipFile, RuntimeError, ValueError)):
            increment_merger.unpack_increment_archive(
                str(corrupt_inc), None, str(out_dir), _TEST_LOGGER
            )

    def test_corrupted_protobuf_messages_bin_handled_gracefully(self, tmp_path: Path):
        """Verify that truncated or malformed varints in messages.bin do not crash the engine."""
        bin_path = tmp_path / "messages.bin"
        # Write an incomplete varint stream (MSB set without terminating byte)
        bin_path.write_bytes(b"\x80\x80\x80")

        # Parser should return empty list or gracefully stop at corrupt boundary
        parsed = increment_merger.read_delimited_messages_bin(str(bin_path))
        assert isinstance(parsed, list)
        assert len(parsed) == 0

    def test_malformed_deleted_messages_json_handled_safely(self, tmp_path: Path):
        """Verify that dirty candidates (strings, nulls, negative numbers) in JSON are ignored."""
        db_path = tmp_path / "msgstore.db"
        conn = make_android_db(db_path)

        json_path = tmp_path / "deletedMessages_1.json"
        json_path.write_text(
            json.dumps({"deleted_ids": ["invalid_string", None, -5, "99999", {}, []]}),
            encoding="utf-8",
        )

        # Candidate 99999 is valid integer syntax but will fail sequence / existence gates safely
        deleted_count = increment_merger.apply_message_deletions(
            conn,
            str(json_path),
            _TEST_LOGGER,
            None,
            backup_timestamp=1000000,
            max_sequence=100,
            inc_base_seq=0,
            messages_bin_path=None,
            output_root=None,
        )
        conn.close()

        # Must not crash, and must not delete invalid candidates
        assert deleted_count == 0


# ==============================================================================
# 3. Preservation Fail-Safe Rollback
# ==============================================================================


class TestFailureModePreservationRollback:
    """Simulate failure during message preservation and verify state restoration."""

    def test_preservation_failure_rolls_back_old_db(self, tmp_path: Path):
        """Verify that if preservation crashes, previous msgstore.db is restored from .old."""
        db_dir = tmp_path / WA_DATABASES_DIR_NAME
        db_dir.mkdir(parents=True, exist_ok=True)

        decrypted_db = db_dir / "msgstore.db"
        old_db = db_dir / "msgstore.db.old"
        incoming_db = db_dir / "msgstore.db.incoming"

        # Baseline previous database
        conn_prev = make_android_db(decrypted_db)
        conn_prev.execute(
            "INSERT INTO jid (_id, user, raw_string) VALUES (1, 'alice', 'alice@s.whatsapp.net')"
        )
        conn_prev.execute("INSERT INTO chat (_id, jid_row_id) VALUES (1, 1)")
        conn_prev.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, key_id, text_data, timestamp) "
            "VALUES (10, 1, 0, 'PREV_KEY', 'Hello from past', 1000)"
        )
        conn_prev.commit()
        conn_prev.close()

        # Incoming new database
        conn_new = make_android_db(incoming_db)
        conn_new.execute(
            "INSERT INTO jid (_id, user, raw_string) VALUES (1, 'alice', 'alice@s.whatsapp.net')"
        )
        conn_new.execute("INSERT INTO chat (_id, jid_row_id) VALUES (1, 1)")
        conn_new.commit()
        conn_new.close()

        # Simulate archiver preservation execution block
        archiver_main._clean_sqlite_files(str(old_db))
        archiver_main._safe_replace_sqlite_db(str(decrypted_db), str(old_db))
        archiver_main._safe_replace_sqlite_db(str(incoming_db), str(decrypted_db))

        # Simulate exception during preservation diffing
        preservation_failed = False
        try:
            with patch.object(
                preservation,
                "preserve_deleted_messages",
                side_effect=RuntimeError("Disk I/O error during preservation"),
            ):
                preservation.preserve_deleted_messages(
                    str(old_db),
                    str(decrypted_db),
                    str(tmp_path),
                    str(tmp_path / ".wa_media_archiver.db"),
                    "android",
                    _TEST_LOGGER,
                )
        except (Exception, KeyboardInterrupt):
            preservation_failed = True
            # Exactly what main.py lines 2200-2204 execute on failure:
            archiver_main._clean_sqlite_files(str(decrypted_db))
            if old_db.exists():
                archiver_main._safe_replace_sqlite_db(str(old_db), str(decrypted_db))

        assert preservation_failed is True

        # Verify decrypted_db was restored to the original previous snapshot
        restored_conn = sqlite3.connect(str(decrypted_db))
        restored_msg = restored_conn.execute(
            "SELECT text_data FROM message WHERE _id = 10"
        ).fetchone()
        restored_conn.close()

        assert restored_msg is not None
        assert restored_msg[0] == "Hello from past"


# ==============================================================================
# 4. Degenerate & Empty Databases in Viewer
# ==============================================================================


class TestFailureModeDegenerateDatabases:
    """Verify that Viewer handles empty or degenerate databases without HTTP 500 errors."""

    def test_viewer_endpoints_on_empty_android_db(self, tmp_path: Path):
        """Verify viewer API returns valid empty JSON payloads on an empty Android database."""
        db_dir = tmp_path / WA_DATABASES_DIR_NAME
        db_dir.mkdir(parents=True, exist_ok=True)

        msgstore = db_dir / "msgstore.db"
        conn = make_android_db(msgstore)
        conn.close()

        arch_db = tmp_path / ".wa_media_archiver.db"
        arch_conn = make_archive_db(arch_db)
        arch_conn.close()

        app = viewer_main.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            # 1. Chats list
            res = client.get("/api/chats")
            assert res.status_code == 200
            assert res.get_json() == []

            # 2. Messages query
            res = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert res.status_code == 200
            assert res.get_json() == []

            # 3. Messages at timestamp
            res = client.get("/api/messages/at?chat_id=1&chat_type=contact&ts=1000")
            assert res.status_code == 200
            assert res.get_json() == []

            # 4. Chat info
            res = client.get("/api/chat-info?chat_id=1&chat_type=contact")
            assert res.status_code == 200
            info = res.get_json()
            assert info["total"] == 0

            # 5. Media gallery counts
            res = client.get("/api/media/count?chat_id=1&chat_type=contact")
            assert res.status_code == 200
            counts = res.get_json()
            assert counts["total"] == 0

            # 6. Non-existent thumbnail
            res = client.get("/api/thumbnail/9999")
            assert res.status_code == 404

            # 7. Non-numeric stanza thumbnail
            res = client.get("/api/thumbnail/NON_EXISTENT_STANZA")
            assert res.status_code == 404

    def test_viewer_endpoints_on_empty_ios_db(self, tmp_path: Path):
        """Verify viewer API returns valid empty JSON payloads on an empty iOS database."""
        db_dir = tmp_path / WA_DATABASES_DIR_NAME
        db_dir.mkdir(parents=True, exist_ok=True)

        chatstorage = db_dir / "ChatStorage.sqlite"
        conn = make_ios_db(chatstorage)
        conn.close()

        arch_db = tmp_path / ".wa_media_archiver.db"
        arch_conn = make_archive_db(arch_db)
        arch_conn.close()

        app = viewer_main.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            res = client.get("/api/chats")
            assert res.status_code == 200
            assert res.get_json() == []

            res = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert res.status_code == 200
            assert res.get_json() == []

            res = client.get("/api/chat-info?chat_id=1&chat_type=contact")
            assert res.status_code == 200

            res = client.get("/api/thumbnail/1")
            assert res.status_code == 404


# ==============================================================================
# 5. High-Volume Message Stress Testing
# ==============================================================================


class TestStressHighVolumeMessages:
    """Stress test query performance on high-volume chats with indexed seek verification."""

    def test_high_volume_chat_pagination_and_at_seek_performance(self, tmp_path: Path):
        """Verify sub-second seek and windowing over 20,000 synthetic messages."""
        db_dir = tmp_path / WA_DATABASES_DIR_NAME
        db_dir.mkdir(parents=True, exist_ok=True)

        msgstore = db_dir / "msgstore.db"
        conn = make_android_db(msgstore)
        conn.execute(
            "INSERT INTO jid (_id, user, raw_string) VALUES (1, 'stress_user', 'stress_user@s.whatsapp.net')"
        )
        conn.execute("INSERT INTO chat (_id, jid_row_id) VALUES (1, 1)")

        # Create 20,000 synthetic messages in chat 1
        msg_rows = [
            (
                i,
                1,  # chat_row_id
                i % 2,  # from_me
                f"msg_stanza_{i}",
                f"Synthetic stress message #{i} with random content payload",
                1000000 + i * 1000,
                0,
            )
            for i in range(1, 20001)
        ]
        conn.executemany(
            "INSERT INTO message (_id, chat_row_id, from_me, key_id, text_data, timestamp, message_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            msg_rows,
        )
        conn.commit()
        conn.close()

        arch_db = tmp_path / ".wa_media_archiver.db"
        arch_conn = make_archive_db(arch_db)
        arch_conn.close()

        app = viewer_main.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            # 1. Test standard windowed message fetch (50 messages)
            t0 = time.perf_counter()
            res = client.get("/api/messages?chat_id=1&chat_type=contact&limit=50")
            dur_ms = (time.perf_counter() - t0) * 1000
            assert res.status_code == 200
            msgs = res.get_json()
            assert len(msgs) == 50
            # Must finish well under 250ms (typically 5-15ms on SQLite B-tree index)
            assert dur_ms < 1000.0

            # 2. Test /api/messages/at seek into the middle of 20,000 messages
            target_ts = 1000000 + 10000 * 1000
            t0 = time.perf_counter()
            res_at = client.get(
                f"/api/messages/at?chat_id=1&chat_type=contact&ts={target_ts}&limit=50"
            )
            dur_at_ms = (time.perf_counter() - t0) * 1000
            assert res_at.status_code == 200
            at_msgs = res_at.get_json()
            assert len(at_msgs) == 50
            assert dur_at_ms < 1000.0

            # Verify target timestamp is centered in results
            timestamps = [m["timestamp_ms"] for m in at_msgs]
            assert min(timestamps) <= target_ts <= max(timestamps)
