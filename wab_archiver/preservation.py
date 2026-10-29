import contextlib
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import UTC, datetime
from typing import NamedTuple

from shared.db import extract_ios_link_description
from shared.source_detection import WA_DATABASES_DIR_NAME

from .progress import ProgressReporter

# ==============================================================================
# preservation.py — Preserving Deleted Messages Between Archival Runs
# Compares a previous database snapshot against an incoming database snapshot,
# extracts deleted and revoked messages into preserved_messages.db,
# and updates rollup summary tables.
# ==============================================================================

PRESERVED_DB_NAME = "preserved_messages.db"


class PreservationResult(NamedTuple):
    messages: int
    chats: int


PRESERVED_SCHEMA_TABLES = """
    CREATE TABLE IF NOT EXISTS preserved_messages (
        chat_id                      TEXT NOT NULL,
        chat_jid                     TEXT,
        chat_type                    TEXT NOT NULL,
        stanza_id                    TEXT NOT NULL,
        msg_id                       INTEGER NOT NULL,
        timestamp_ms                 INTEGER NOT NULL,
        sender                       TEXT NOT NULL,
        sender_jid                   TEXT,
        sender_phone                 TEXT,
        from_me                      INTEGER NOT NULL,
        text_body                    TEXT NOT NULL DEFAULT '',
        original_text_body           TEXT,
        media_type                   TEXT NOT NULL DEFAULT 'text',
        media_name                   TEXT,
        archive_path                 TEXT,
        original_path                TEXT,
        vcard_data                   TEXT,
        media_width                  INTEGER,
        media_height                 INTEGER,
        media_duration               INTEGER,
        media_size                   INTEGER,
        is_view_once                 INTEGER NOT NULL DEFAULT 0,
        latitude                     REAL,
        longitude                    REAL,
        location_name                TEXT,
        location_address             TEXT,
        location_url                 TEXT,
        live_location_share_duration INTEGER,
        poll_data                    TEXT,
        quoted_text                  TEXT,
        quoted_sender                TEXT,
        quoted_ts                    INTEGER,
        quoted_stanza_id             TEXT,
        quoted_parent_type           TEXT,
        reactions                    TEXT,
        is_edited                    INTEGER NOT NULL DEFAULT 0,
        edited_ts                    INTEGER,
        is_starred                   INTEGER NOT NULL DEFAULT 0,
        is_pinned                    INTEGER NOT NULL DEFAULT 0,
        pin_expiry_ts                INTEGER,
        action_type                  INTEGER,
        participant_name             TEXT,
        service_metadata             TEXT,
        preserved_at                 TEXT NOT NULL,
        preservation_reason          TEXT NOT NULL,
        source_platform              TEXT NOT NULL,
        PRIMARY KEY (chat_id, stanza_id)
    );

    CREATE TABLE IF NOT EXISTS preserved_chats (
        chat_id                      TEXT NOT NULL,
        chat_type                    TEXT NOT NULL,
        chat_jid                     TEXT,
        subject                      TEXT,
        description                  TEXT,
        created_ts                   INTEGER,
        creator_jid                  TEXT,
        oldest_ts                    INTEGER,
        newest_ts                    INTEGER NOT NULL,
        msg_count                    INTEGER NOT NULL,
        members_json                 TEXT,
        PRIMARY KEY (chat_id, chat_type)
    );

    CREATE TABLE IF NOT EXISTS preserved_calls (
        call_id                      TEXT PRIMARY KEY,
        chat_id                      TEXT,
        chat_jid                     TEXT,
        caller_jid                   TEXT,
        caller_name                  TEXT,
        from_me                      INTEGER NOT NULL,
        timestamp_ms                 INTEGER NOT NULL,
        duration_seconds             INTEGER NOT NULL DEFAULT 0,
        is_video                     INTEGER NOT NULL DEFAULT 0,
        call_result                  INTEGER NOT NULL DEFAULT 0,
        is_group_call                INTEGER NOT NULL DEFAULT 0,
        participants                 TEXT,
        bytes_transferred            INTEGER,
        preserved_at                 TEXT NOT NULL,
        source_platform              TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS preservation_runs (
        run_id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        run_at                       TEXT NOT NULL,
        source_platform              TEXT NOT NULL,
        messages_preserved           INTEGER NOT NULL,
        chats_affected               INTEGER NOT NULL,
        calls_preserved              INTEGER NOT NULL DEFAULT 0,
        notes                        TEXT
    );
"""

PRESERVED_SCHEMA_INDEXES = """
    CREATE INDEX IF NOT EXISTS idx_pres_chat_ts
        ON preserved_messages(chat_id, timestamp_ms DESC);

    CREATE INDEX IF NOT EXISTS idx_pres_chat_type_ts
        ON preserved_messages(chat_type, chat_id, timestamp_ms DESC);

    CREATE INDEX IF NOT EXISTS idx_pres_stanza
        ON preserved_messages(stanza_id);

    CREATE INDEX IF NOT EXISTS idx_pres_chat_jid
        ON preserved_messages(chat_jid);

    CREATE INDEX IF NOT EXISTS idx_pres_quoted_stanza
        ON preserved_messages(quoted_stanza_id)
        WHERE quoted_stanza_id IS NOT NULL;

    CREATE INDEX IF NOT EXISTS idx_pres_calls_chat_ts
        ON preserved_calls(chat_jid, timestamp_ms DESC);

    CREATE INDEX IF NOT EXISTS idx_pres_calls_ts
        ON preserved_calls(timestamp_ms DESC);
"""

PRESERVED_SCHEMA = PRESERVED_SCHEMA_TABLES + PRESERVED_SCHEMA_INDEXES

NEW_V2_MESSAGE_COLUMNS: list[tuple[str, str]] = [
    ("chat_jid", "TEXT"),
    ("sender_jid", "TEXT"),
    ("sender_phone", "TEXT"),
    ("original_text_body", "TEXT"),
    ("original_path", "TEXT"),
    ("vcard_data", "TEXT"),
    ("media_width", "INTEGER"),
    ("media_height", "INTEGER"),
    ("media_duration", "INTEGER"),
    ("media_size", "INTEGER"),
    ("is_view_once", "INTEGER NOT NULL DEFAULT 0"),
    ("latitude", "REAL"),
    ("longitude", "REAL"),
    ("location_name", "TEXT"),
    ("location_address", "TEXT"),
    ("location_url", "TEXT"),
    ("live_location_share_duration", "INTEGER"),
    ("poll_data", "TEXT"),
    ("quoted_stanza_id", "TEXT"),
    ("quoted_parent_type", "TEXT"),
    ("is_starred", "INTEGER NOT NULL DEFAULT 0"),
    ("is_pinned", "INTEGER NOT NULL DEFAULT 0"),
    ("pin_expiry_ts", "INTEGER"),
    ("service_metadata", "TEXT"),
]

NEW_V2_CHAT_COLUMNS: list[tuple[str, str]] = [
    ("chat_jid", "TEXT"),
    ("subject", "TEXT"),
    ("description", "TEXT"),
    ("created_ts", "INTEGER"),
    ("creator_jid", "TEXT"),
    ("oldest_ts", "INTEGER"),
    ("members_json", "TEXT"),
]

NEW_V2_RUN_COLUMNS: list[tuple[str, str]] = [
    ("calls_preserved", "INTEGER NOT NULL DEFAULT 0"),
]


def get_preserved_db_path(output_root: str) -> str:
    """Return the canonical path to preserved_messages.db under output_root."""
    db_dir = os.path.join(output_root, WA_DATABASES_DIR_NAME)
    return os.path.join(db_dir, PRESERVED_DB_NAME)


def open_preservation_db(output_root: str) -> sqlite3.Connection:
    """Open or initialize preserved_messages.db with WAL mode and incremental vacuum."""
    db_path = get_preserved_db_path(output_root)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(PRESERVED_SCHEMA_TABLES)

    # Automated schema migration check
    user_ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if user_ver < 2:
        existing_msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(preserved_messages)")}
        if "participant_name" not in existing_msg_cols:
            conn.execute("ALTER TABLE preserved_messages ADD COLUMN participant_name TEXT")
            existing_msg_cols.add("participant_name")
        for col_name, col_def in NEW_V2_MESSAGE_COLUMNS:
            if col_name not in existing_msg_cols:
                conn.execute(f"ALTER TABLE preserved_messages ADD COLUMN {col_name} {col_def}")
                existing_msg_cols.add(col_name)

        existing_chat_cols = {r[1] for r in conn.execute("PRAGMA table_info(preserved_chats)")}
        for col_name, col_def in NEW_V2_CHAT_COLUMNS:
            if col_name not in existing_chat_cols:
                conn.execute(f"ALTER TABLE preserved_chats ADD COLUMN {col_name} {col_def}")
                existing_chat_cols.add(col_name)

        existing_run_cols = {r[1] for r in conn.execute("PRAGMA table_info(preservation_runs)")}
        for col_name, col_def in NEW_V2_RUN_COLUMNS:
            if col_name not in existing_run_cols:
                conn.execute(f"ALTER TABLE preservation_runs ADD COLUMN {col_name} {col_def}")
                existing_run_cols.add(col_name)

        # Backfill oldest_ts for any existing chats missing it
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("""
                UPDATE preserved_chats
                SET oldest_ts = (
                    SELECT MIN(timestamp_ms)
                    FROM preserved_messages
                    WHERE preserved_messages.chat_id = preserved_chats.chat_id
                      AND preserved_messages.chat_type = preserved_chats.chat_type
                )
                WHERE oldest_ts IS NULL
            """)

        conn.execute("PRAGMA user_version = 2")
        conn.commit()

    conn.executescript(PRESERVED_SCHEMA_INDEXES)
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def verify_account_match(
    old_db_path: str, new_db_path: str, platform: str, logger: logging.Logger
) -> bool:
    """
    Verify that old_db and new_db share common chats / account identifiers.
    Returns True if compatible or if either database has too few chats to evaluate.
    Returns False if both have multiple chats and zero overlap.
    """
    try:
        with (
            contextlib.closing(sqlite3.connect(old_db_path)) as old_c,
            contextlib.closing(sqlite3.connect(new_db_path)) as new_c,
        ):
            if platform == "android":
                old_tables = {
                    r[0]
                    for r in old_c.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                new_tables = {
                    r[0]
                    for r in new_c.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if "jid" not in old_tables or "jid" not in new_tables:
                    return True
                old_users = {
                    r[0]
                    for r in old_c.execute(
                        "SELECT user FROM jid WHERE user IS NOT NULL AND user != '0'"
                    ).fetchall()
                }
                new_users = {
                    r[0]
                    for r in new_c.execute(
                        "SELECT user FROM jid WHERE user IS NOT NULL AND user != '0'"
                    ).fetchall()
                }
                if len(old_users) >= 5 and len(new_users) >= 5:
                    overlap = len(old_users.intersection(new_users))
                    if overlap == 0:
                        logger.warning(
                            "Account verification warning: Zero common contacts detected between "
                            "previous database and incoming database. Preserving skipped to prevent cross-account merge."
                        )
                        return False
            elif platform == "ios":
                old_tables = {
                    r[0]
                    for r in old_c.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                new_tables = {
                    r[0]
                    for r in new_c.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if "ZWACHATSESSION" not in old_tables or "ZWACHATSESSION" not in new_tables:
                    return True
                old_jids = {
                    r[0]
                    for r in old_c.execute(
                        "SELECT ZCONTACTJID FROM ZWACHATSESSION WHERE ZCONTACTJID IS NOT NULL"
                    ).fetchall()
                }
                new_jids = {
                    r[0]
                    for r in new_c.execute(
                        "SELECT ZCONTACTJID FROM ZWACHATSESSION WHERE ZCONTACTJID IS NOT NULL"
                    ).fetchall()
                }
                if len(old_jids) >= 5 and len(new_jids) >= 5:
                    overlap = len(old_jids.intersection(new_jids))
                    if overlap == 0:
                        logger.warning(
                            "Account verification warning: Zero common chat sessions detected between "
                            "previous database and incoming database. Preserving skipped to prevent cross-account merge."
                        )
                        return False
    except Exception as e:
        logger.warning(f"Account verification check encountered error ({e}); proceeding safely.")
        return True
    return True


def _ensure_android_diff_indexes(conn: sqlite3.Connection) -> None:
    """Ensure indexes on key_id and fallback columns exist for fast diff performance."""
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("DROP INDEX IF EXISTS idx_pres_cur_key_id")
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_message_key_id ON message(key_id)")
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pres_old_key_id ON old_db.message(key_id)")
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pres_cur_fallback ON message(chat_row_id, timestamp, from_me)"
        )
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("DROP INDEX IF EXISTS idx_pres_cur_revoked")
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pres_cur_revoked ON message(key_id) WHERE message_type IN (15, 64)"
        )


def _ensure_ios_diff_indexes(conn: sqlite3.Connection) -> None:
    """Ensure indexes on ZSTANZAID and fallback columns exist for fast diff performance."""
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pres_cur_stanza ON ZWAMESSAGE(ZSTANZAID)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pres_old_stanza ON old_db.ZWAMESSAGE(ZSTANZAID)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pres_cur_fallback ON ZWAMESSAGE(ZCHATSESSION, ZMESSAGEDATE, ZISFROMME)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pres_cur_revoked ON ZWAMESSAGE(ZSTANZAID) WHERE ZMESSAGETYPE = 14"
        )


def diff_and_extract_android(
    old_db_path: str, new_db_path: str, archive_db_path: str, logger: logging.Logger
) -> list[dict]:
    """
    Diff an Android msgstore.db against incoming new_db and extract deleted/revoked messages.
    Returns a list of normalized message dicts ready for insertion into preserved_messages.
    """
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    extracted_rows: list[dict] = []

    with contextlib.closing(sqlite3.connect(new_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA cache_size = -64000")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA mmap_size = 268435456")
        conn.execute("ATTACH DATABASE ? AS old_db", (old_db_path,))
        conn.execute("ATTACH DATABASE ? AS arch", (archive_db_path,))
        _ensure_android_diff_indexes(conn)

        # Inspect table schema in old_db, new_db, and arch to adapt queries gracefully
        old_tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM old_db.sqlite_master WHERE type='table'"
            ).fetchall()
        }
        arch_tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM arch.sqlite_master WHERE type='table'"
            ).fetchall()
        }
        cur_tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

        has_media = "message_media" in old_tables
        has_quoted = "message_quoted" in old_tables
        has_system = "message_system" in old_tables
        has_system_participant = "message_system_chat_participant" in old_tables
        has_edit_info = "message_edit_info" in old_tables
        has_add_on = "message_add_on" in old_tables
        has_pin = "message_add_on_pin_in_chat" in old_tables
        has_location = "message_location" in old_tables
        has_vcard = "message_vcard" in old_tables
        has_poll = "message_poll" in old_tables
        has_poll_option = "message_poll_option" in old_tables
        has_view_once = "message_view_once_media" in old_tables
        has_group_participants = (
            "group_participant_user" in old_tables or "group_participants" in old_tables
        )
        has_jid_map = "jid_map" in old_tables
        has_chat = "chat" in old_tables
        has_jid = "jid" in old_tables
        has_cur_revoked = "message_revoked" in cur_tables
        has_arch_contacts = "contacts" in arch_tables
        has_arch_copies = "archive_copies" in arch_tables

        msg_cols = (
            {r[1] for r in conn.execute("PRAGMA old_db.table_info(message)").fetchall()}
            if "message" in old_tables
            else set()
        )
        has_starred = "starred" in msg_cols
        starred_expr = "COALESCE(m.starred, 0)" if has_starred else "0"

        jid_cols = (
            {r[1] for r in conn.execute("PRAGMA old_db.table_info(jid)").fetchall()}
            if has_jid
            else set()
        )
        has_jid_server = "server" in jid_cols
        has_jid_raw = "raw_string" in jid_cols

        if has_jid_raw and has_jid_server:
            chat_jid_sql = "COALESCE(j_chat.raw_string, CASE WHEN j_chat.server IS NOT NULL THEN j_chat.user || '@' || j_chat.server ELSE j_chat.user END)"
            sender_jid_sql = "COALESCE(j2.raw_string, j.raw_string, CASE WHEN j2.server IS NOT NULL THEN j2.user || '@' || j2.server END, CASE WHEN j.server IS NOT NULL THEN j.user || '@' || j.server END, j2.user, j.user)"
            sender_phone_sql = "COALESCE(j2.user, CASE WHEN j.server = 's.whatsapp.net' OR j.server IS NULL THEN j.user END)"
        elif has_jid_raw:
            chat_jid_sql = "COALESCE(j_chat.raw_string, j_chat.user)"
            sender_jid_sql = "COALESCE(j2.raw_string, j.raw_string, j2.user, j.user)"
            sender_phone_sql = "COALESCE(j2.user, j.user)"
        elif has_jid_server:
            chat_jid_sql = "CASE WHEN j_chat.server IS NOT NULL THEN j_chat.user || '@' || j_chat.server ELSE j_chat.user END"
            sender_jid_sql = "COALESCE(CASE WHEN j2.server IS NOT NULL THEN j2.user || '@' || j2.server END, CASE WHEN j.server IS NOT NULL THEN j.user || '@' || j.server END, j2.user, j.user)"
            sender_phone_sql = "COALESCE(j2.user, CASE WHEN j.server = 's.whatsapp.net' OR j.server IS NULL THEN j.user END)"
        else:
            chat_jid_sql = "j_chat.user"
            sender_jid_sql = "COALESCE(j2.user, j.user)"
            sender_phone_sql = "COALESCE(j2.user, j.user)"

        mm_cols = (
            {r[1] for r in conn.execute("PRAGMA old_db.table_info(message_media)").fetchall()}
            if has_media
            else set()
        )
        mm_width = "mm.width" if "width" in mm_cols else "NULL"
        mm_height = "mm.height" if "height" in mm_cols else "NULL"
        mm_duration = "mm.media_duration" if "media_duration" in mm_cols else "NULL"
        if "file_length" in mm_cols and "file_size" in mm_cols:
            mm_size = "COALESCE(mm.file_length, mm.file_size)"
        elif "file_length" in mm_cols:
            mm_size = "mm.file_length"
        elif "file_size" in mm_cols:
            mm_size = "mm.file_size"
        else:
            mm_size = "NULL"
        file_path_col = "mm.file_path" if "file_path" in mm_cols else "NULL"
        media_name_col = "mm.media_name" if "media_name" in mm_cols else "NULL"

        ml_cols = (
            {r[1] for r in conn.execute("PRAGMA old_db.table_info(message_location)").fetchall()}
            if has_location
            else set()
        )
        ml_lat = "ml.latitude" if "latitude" in ml_cols else "NULL"
        ml_lng = "ml.longitude" if "longitude" in ml_cols else "NULL"
        ml_name = "ml.place_name" if "place_name" in ml_cols else "NULL"
        ml_address = "ml.place_address" if "place_address" in ml_cols else "NULL"
        ml_url = "ml.url" if "url" in ml_cols else "NULL"
        ml_dur = (
            "ml.live_location_share_duration"
            if "live_location_share_duration" in ml_cols
            else "NULL"
        )

        mq_cols = (
            {r[1] for r in conn.execute("PRAGMA old_db.table_info(message_quoted)").fetchall()}
            if has_quoted
            else set()
        )
        mq_key_id_expr = "mq.key_id" if "key_id" in mq_cols else "NULL"
        if "message_type" in mq_cols:
            mq_parent_type_expr = """CASE
                WHEN mq.message_type = 1 THEN 'image'
                WHEN mq.message_type = 2 THEN 'audio'
                WHEN mq.message_type = 3 THEN 'video'
                WHEN mq.message_type IN (4, 14) THEN 'vcard'
                WHEN mq.message_type IN (5, 18) THEN 'location'
                WHEN mq.message_type = 9 THEN 'document'
                WHEN mq.message_type = 13 THEN 'gif'
                WHEN mq.message_type = 20 THEN 'sticker'
                WHEN mq.message_type = 66 THEN 'poll'
                WHEN mq.message_type = 0 THEN 'text'
                ELSE NULL
            END"""
            mq_text_expr = """CASE
                WHEN mq.text_data IS NOT NULL AND mq.text_data != '' THEN mq.text_data
                WHEN mq.message_type = 1 THEN '📷 Photo'
                WHEN mq.message_type = 2 THEN '🎵 Audio'
                WHEN mq.message_type = 3 THEN '🎥 Video'
                WHEN mq.message_type IN (4, 14) THEN '👤 Contact card'
                WHEN mq.message_type IN (5, 18) THEN '📍 Location'
                WHEN mq.message_type = 20 THEN 'Sticker'
                WHEN mq.message_type = 66 THEN '📊 Poll'
                ELSE ''
            END"""
        elif "text_data" in mq_cols:
            mq_parent_type_expr = "NULL"
            mq_text_expr = "COALESCE(mq.text_data, '')"
        else:
            mq_parent_type_expr = "NULL"
            mq_text_expr = "''"
        quoted_ts_col = "mq.timestamp" if "timestamp" in mq_cols else "0"
        quoted_from_me_col = "mq.from_me" if "from_me" in mq_cols else "0"

        chat_cols = (
            {r[1] for r in conn.execute("PRAGMA old_db.table_info(chat)").fetchall()}
            if has_chat
            else set()
        )
        chat_created_ts_expr = "c.created_timestamp" if "created_timestamp" in chat_cols else "NULL"
        chat_desc_expr = "c.description" if "description" in chat_cols else "NULL"
        chat_subject_expr = "c.subject" if "subject" in chat_cols else "NULL"
        chat_gjid_expr = "c.jid_row_id" if "jid_row_id" in chat_cols else "NULL"
        chat_creator_jid_expr = (
            "(SELECT j_cr.raw_string FROM old_db.jid j_cr WHERE j_cr._id = c.creator_jid_row_id)"
            if ("creator_jid_row_id" in chat_cols and has_jid)
            else "NULL"
        )

        mm_join = (
            "LEFT JOIN old_db.message_media mm ON mm.message_row_id = m._id" if has_media else ""
        )
        mq_join = (
            "LEFT JOIN old_db.message_quoted mq ON mq.message_row_id = m._id" if has_quoted else ""
        )
        ms_join = (
            "LEFT JOIN old_db.message_system ms ON ms.message_row_id = m._id" if has_system else ""
        )
        mscp_join = (
            "LEFT JOIN old_db.message_system_chat_participant mscp ON mscp.message_row_id = m._id\n"
            "LEFT JOIN old_db.jid j_mscp ON j_mscp._id = mscp.user_jid_row_id"
            if (has_system and has_system_participant and has_jid)
            else ""
        )
        ml_join = (
            "LEFT JOIN old_db.message_location ml ON ml.message_row_id = m._id"
            if has_location
            else ""
        )
        mv_join = (
            "LEFT JOIN old_db.message_vcard mv ON mv.message_row_id = m._id" if has_vcard else ""
        )
        mvom_join = (
            "LEFT JOIN old_db.message_view_once_media mvom ON mvom.message_row_id = m._id"
            if has_view_once
            else ""
        )
        edit_join = (
            "LEFT JOIN old_db.message_edit_info mei ON mei.message_row_id = m._id"
            if has_edit_info
            else ""
        )
        jm_join = (
            "LEFT JOIN old_db.jid_map jm ON jm.lid_row_id = m.sender_jid_row_id\n"
            "LEFT JOIN old_db.jid j2 ON j2._id = jm.jid_row_id"
            if has_jid_map
            else "LEFT JOIN old_db.jid j2 ON 0 = 1"
        )
        jmq_join = (
            "LEFT JOIN old_db.jid_map jmq ON jmq.lid_row_id = mq.sender_jid_row_id\n"
            "LEFT JOIN old_db.jid jq2 ON jq2._id = jmq.jid_row_id"
            if (has_jid_map and has_quoted)
            else "LEFT JOIN old_db.jid jq2 ON 0 = 1"
        )
        jq_join = (
            "LEFT JOIN old_db.jid jq ON jq._id = mq.sender_jid_row_id"
            if has_quoted
            else "LEFT JOIN old_db.jid jq ON 0 = 1"
        )
        con_s_join = (
            "LEFT JOIN arch.contacts con_s ON con_s.number = COALESCE(j2.user, j.user)"
            if has_arch_contacts
            else ""
        )
        con_sq_join = (
            "LEFT JOIN arch.contacts con_sq ON con_sq.number = COALESCE(jq2.user, jq.user)"
            if has_arch_contacts
            else ""
        )
        con_mscp_join = (
            "LEFT JOIN arch.contacts con_mscp ON con_mscp.number = j_mscp.user"
            if (has_system and has_system_participant and has_jid and has_arch_contacts)
            else ""
        )
        con_s_arm = "NULLIF(con_s.display_name, '')," if has_arch_contacts else ""
        con_sq_arm = "NULLIF(con_sq.display_name, '')," if has_arch_contacts else ""
        con_mscp_arm = (
            "NULLIF(con_mscp.display_name, ''),"
            if (has_system and has_system_participant and has_jid and has_arch_contacts)
            else ""
        )

        ac_join = (
            f"""LEFT JOIN arch.archive_copies ac ON ac.original_path IN (
                COALESCE({file_path_col}, 'vcard:' || m._id),
                REPLACE(COALESCE({file_path_col}, ''), '\\\\', '/'),
                REPLACE(COALESCE({file_path_col}, ''), '/', '\\\\'),
                CASE WHEN {file_path_col} LIKE 'WhatsApp/%' OR {file_path_col} LIKE 'WhatsApp\\\\%'
                     THEN SUBSTR(REPLACE({file_path_col}, '\\\\', '/'), 10) END,
                CASE WHEN {file_path_col} LIKE 'WhatsApp/%' OR {file_path_col} LIKE 'WhatsApp\\\\%'
                     THEN REPLACE(SUBSTR(REPLACE({file_path_col}, '\\\\', '/'), 10), '/', '\\\\') END,
                CASE WHEN {file_path_col} NOT LIKE 'WhatsApp/%' AND {file_path_col} NOT LIKE 'WhatsApp\\\\%' AND {file_path_col} IS NOT NULL
                     THEN 'WhatsApp/' || REPLACE({file_path_col}, '\\\\', '/') END,
                CASE WHEN {file_path_col} NOT LIKE 'WhatsApp/%' AND {file_path_col} NOT LIKE 'WhatsApp\\\\%' AND {file_path_col} IS NOT NULL
                     THEN 'WhatsApp\\\\' || REPLACE({file_path_col}, '/', '\\\\') END
            )"""
            if has_arch_copies
            else ""
        )
        archive_path_expr = "ac.archive_path" if has_arch_copies else "NULL"
        vcard_data_col = "mv.vcard" if has_vcard else "NULL"
        mvom_or = "OR mvom.message_row_id IS NOT NULL" if has_view_once else ""
        is_view_once_expr = f"""CASE
            WHEN m.message_type IN (42, 43, 82) {mvom_or} THEN 1
            ELSE 0
        END"""
        edit_is_edited_col = (
            "CASE WHEN mei.message_row_id IS NOT NULL THEN 1 ELSE 0 END" if has_edit_info else "0"
        )
        edit_ts_col = "mei.edited_timestamp" if has_edit_info else "NULL"
        ms_action_col = "ms.action_type" if has_system else "NULL"
        part_name_expr = (
            f"""COALESCE(
                {con_mscp_arm}
                CASE WHEN j_mscp.user IS NOT NULL THEN '+' || j_mscp.user END,
                ''
            )"""
            if (has_system and has_system_participant and has_jid)
            else "''"
        )

        poll_pred = (
            "(m.message_type = 66 OR (EXISTS (SELECT 1 FROM old_db.message_poll mp WHERE mp.message_row_id = m._id)))"
            if has_poll
            else "(m.message_type = 66)"
        )

        media_type_expr = f"""CASE
            WHEN m.message_type = 7 THEN 'service'
            WHEN m.message_type IN (4, 14) THEN 'vcard'
            WHEN m.message_type IN (5, 18) THEN 'location'
            WHEN {poll_pred} THEN 'poll'
            WHEN m.message_type = 20 THEN 'sticker'
            WHEN m.message_type = 1 THEN 'image'
            WHEN m.message_type = 2 THEN 'audio'
            WHEN m.message_type = 3 THEN 'video'
            WHEN m.message_type = 9 THEN 'document'
            WHEN m.message_type = 13 THEN 'gif'
            WHEN m.message_type = 42 THEN 'image'
            WHEN m.message_type = 43 THEN 'video'
            WHEN m.message_type = 82 THEN 'audio'
            WHEN m.message_type IS NULL OR m.message_type = 0 THEN
                CASE WHEN m.text_data IS NOT NULL
                      AND (INSTR(LOWER(m.text_data), 'http://') > 0
                           OR INSTR(LOWER(m.text_data), 'https://') > 0)
                     THEN 'link' ELSE 'text' END
            ELSE
                CASE
                    WHEN LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.vcf' THEN 'vcard'
                    WHEN LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%sticker%'
                      OR LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.webp'
                      OR LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.was' THEN 'sticker'
                    WHEN LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.jpg'
                      OR LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.jpeg'
                      OR LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.png' THEN 'image'
                    WHEN LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.mp4'
                      OR LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.mov'
                      OR LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.3gp' THEN 'video'
                    WHEN LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.opus'
                      OR LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.m4a'
                      OR LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.ogg'
                      OR LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.mp3' THEN 'audio'
                    ELSE 'document'
                END
        END"""

        # Check total messages in old vs new to detect device reset scenario
        row_span = conn.execute(
            "SELECT MIN(_id), MAX(_id), COUNT(*) FROM old_db.message"
        ).fetchone()
        min_id, max_id, old_count = row_span[0], row_span[1], row_span[2]
        new_count = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        is_device_reset = (old_count >= 100) and (new_count < old_count * 0.1)

        if old_count == 0 or min_id is None or max_id is None:
            return []

        # 1. Identify missing messages (deleted on device)
        sql_check_deleted = """
            SELECT m._id
            FROM old_db.message m
            WHERE m._id >= ? AND m._id < ? AND (
                (
                    m.key_id IS NOT NULL AND m.key_id != ''
                    AND NOT EXISTS (SELECT 1 FROM message cur WHERE cur.key_id = m.key_id)
                ) OR (
                    (m.key_id IS NULL OR m.key_id = '')
                    AND NOT EXISTS (
                        SELECT 1 FROM message cur
                        WHERE cur.chat_row_id = m.chat_row_id
                          AND cur.timestamp = m.timestamp
                          AND cur.from_me = m.from_me
                    )
                )
            )
        """

        sql_deleted_template = f"""
            SELECT
                m._id AS msg_id,
                COALESCE(NULLIF(m.key_id, ''), 'msg_' || m._id) AS stanza_id,
                CASE
                    WHEN {chat_subject_expr} IS NOT NULL THEN
                        COALESCE(
                            (SELECT CAST(cur_c._id AS TEXT) FROM chat cur_c JOIN jid cur_j ON cur_j._id = cur_c.jid_row_id WHERE cur_j.user = j_chat.user),
                            CAST(m.chat_row_id AS TEXT)
                        )
                    ELSE COALESCE(j_chat.user, CAST(m.chat_row_id AS TEXT))
                END AS chat_id,
                CASE WHEN {chat_subject_expr} IS NOT NULL THEN 'group' ELSE 'contact' END AS chat_type,
                {chat_jid_sql} AS chat_jid,
                COALESCE(m.timestamp, 0) AS timestamp_ms,
                COALESCE(
                    {con_s_arm}
                    CASE WHEN COALESCE(j2.user, j.user) = '0' THEN 'WhatsApp' END,
                    CASE WHEN j2.user IS NOT NULL THEN '+' || j2.user END,
                    CASE WHEN j.user IS NOT NULL THEN '+' || j.user END,
                    ''
                ) AS sender,
                {sender_jid_sql} AS sender_jid,
                {sender_phone_sql} AS sender_phone,
                m.from_me AS from_me,
                COALESCE(NULLIF(m.text_data, ''), CASE WHEN {poll_pred} THEN 'Poll' END, '') AS text_body,
                {media_type_expr} AS media_type,
                COALESCE({media_name_col}, CASE WHEN m.message_type IN (4, 14) THEN m.text_data END, '') AS media_name,
                {archive_path_expr} AS archive_path,
                {file_path_col} AS original_path,
                {vcard_data_col} AS vcard_data,
                {mm_width} AS media_width,
                {mm_height} AS media_height,
                {mm_duration} AS media_duration,
                {mm_size} AS media_size,
                {is_view_once_expr} AS is_view_once,
                {ml_lat} AS latitude,
                {ml_lng} AS longitude,
                {ml_name} AS location_name,
                {ml_address} AS location_address,
                {ml_url} AS location_url,
                {ml_dur} AS live_location_share_duration,
                {mq_text_expr} AS quoted_text,
                CASE WHEN {quoted_from_me_col} = 1 THEN 'You'
                     ELSE COALESCE(
                         {con_sq_arm}
                         CASE WHEN jq2.user IS NOT NULL THEN '+' || jq2.user END,
                         CASE WHEN jq.user IS NOT NULL THEN '+' || jq.user END,
                         '')
                END AS quoted_sender,
                COALESCE({quoted_ts_col}, 0) AS quoted_ts,
                {mq_key_id_expr} AS quoted_stanza_id,
                {mq_parent_type_expr} AS quoted_parent_type,
                {edit_is_edited_col} AS is_edited,
                {edit_ts_col} AS edited_ts,
                {starred_expr} AS is_starred,
                {ms_action_col} AS action_type,
                {part_name_expr} AS participant_name,
                {chat_subject_expr} AS _chat_subject,
                {chat_desc_expr} AS _chat_description,
                {chat_created_ts_expr} AS _chat_created_ts,
                {chat_creator_jid_expr} AS _chat_creator_jid,
                {chat_gjid_expr} AS _chat_group_jid_id,
                CASE
                    WHEN {is_device_reset} THEN 'device_reset'
                    WHEN NOT EXISTS (
                        SELECT 1 FROM chat cur_c
                        JOIN jid cur_j ON cur_j._id = cur_c.jid_row_id
                        WHERE cur_j.user = j_chat.user
                    ) AND NOT EXISTS (
                        SELECT 1 FROM chat cur_c WHERE cur_c._id = m.chat_row_id
                    ) THEN 'chat_deleted'
                    ELSE 'deleted_on_phone'
                END AS preservation_reason
            FROM old_db.message m
            LEFT JOIN old_db.chat c ON c._id = m.chat_row_id
            LEFT JOIN old_db.jid j_chat ON j_chat._id = c.jid_row_id
            LEFT JOIN old_db.jid j ON j._id = m.sender_jid_row_id
            {jm_join}
            {mm_join}
            {mq_join}
            {jq_join}
            {jmq_join}
            {ms_join}
            {mscp_join}
            {ml_join}
            {mv_join}
            {mvom_join}
            {edit_join}
            {con_s_join}
            {con_sq_join}
            {con_mscp_join}
            {ac_join}
            WHERE m._id IN ({{placeholders}})
        """

        # 2. Identify revoked messages ("Delete for everyone", by sender or admin)
        sql_revoked = f"""
            SELECT
                m._id AS msg_id,
                COALESCE(NULLIF(m.key_id, ''), 'msg_' || m._id) AS stanza_id,
                CASE
                    WHEN {chat_subject_expr} IS NOT NULL THEN
                        COALESCE(
                            (SELECT CAST(cur_c._id AS TEXT) FROM chat cur_c JOIN jid cur_j ON cur_j._id = cur_c.jid_row_id WHERE cur_j.user = j_chat.user),
                            CAST(m.chat_row_id AS TEXT)
                        )
                    ELSE COALESCE(j_chat.user, CAST(m.chat_row_id AS TEXT))
                END AS chat_id,
                CASE WHEN {chat_subject_expr} IS NOT NULL THEN 'group' ELSE 'contact' END AS chat_type,
                {chat_jid_sql} AS chat_jid,
                COALESCE(m.timestamp, 0) AS timestamp_ms,
                COALESCE(
                    {con_s_arm}
                    CASE WHEN COALESCE(j2.user, j.user) = '0' THEN 'WhatsApp' END,
                    CASE WHEN j2.user IS NOT NULL THEN '+' || j2.user END,
                    CASE WHEN j.user IS NOT NULL THEN '+' || j.user END,
                    ''
                ) AS sender,
                {sender_jid_sql} AS sender_jid,
                {sender_phone_sql} AS sender_phone,
                m.from_me AS from_me,
                COALESCE(NULLIF(m.text_data, ''), CASE WHEN {poll_pred} THEN 'Poll' END, '') AS text_body,
                {media_type_expr} AS media_type,
                COALESCE({media_name_col}, CASE WHEN m.message_type IN (4, 14) THEN m.text_data END, '') AS media_name,
                {archive_path_expr} AS archive_path,
                {file_path_col} AS original_path,
                {vcard_data_col} AS vcard_data,
                {mm_width} AS media_width,
                {mm_height} AS media_height,
                {mm_duration} AS media_duration,
                {mm_size} AS media_size,
                {is_view_once_expr} AS is_view_once,
                {ml_lat} AS latitude,
                {ml_lng} AS longitude,
                {ml_name} AS location_name,
                {ml_address} AS location_address,
                {ml_url} AS location_url,
                {ml_dur} AS live_location_share_duration,
                {mq_text_expr} AS quoted_text,
                CASE WHEN {quoted_from_me_col} = 1 THEN 'You'
                     ELSE COALESCE(
                         {con_sq_arm}
                         CASE WHEN jq2.user IS NOT NULL THEN '+' || jq2.user END,
                         CASE WHEN jq.user IS NOT NULL THEN '+' || jq.user END,
                         '')
                END AS quoted_sender,
                COALESCE({quoted_ts_col}, 0) AS quoted_ts,
                {mq_key_id_expr} AS quoted_stanza_id,
                {mq_parent_type_expr} AS quoted_parent_type,
                {edit_is_edited_col} AS is_edited,
                {edit_ts_col} AS edited_ts,
                {starred_expr} AS is_starred,
                {ms_action_col} AS action_type,
                {part_name_expr} AS participant_name,
                {chat_subject_expr} AS _chat_subject,
                {chat_desc_expr} AS _chat_description,
                {chat_created_ts_expr} AS _chat_created_ts,
                {chat_creator_jid_expr} AS _chat_creator_jid,
                {chat_gjid_expr} AS _chat_group_jid_id,
                CASE
                    WHEN cur.message_type = 64 THEN 'revoked_by_admin'
                    ELSE 'revoked_by_sender'
                END AS preservation_reason
            FROM old_db.message m
            JOIN message cur ON cur.key_id = m.key_id
            LEFT JOIN old_db.chat c ON c._id = m.chat_row_id
            LEFT JOIN old_db.jid j_chat ON j_chat._id = c.jid_row_id
            LEFT JOIN old_db.jid j ON j._id = m.sender_jid_row_id
            {jm_join}
            {mm_join}
            {mq_join}
            {jq_join}
            {jmq_join}
            {ms_join}
            {mscp_join}
            {ml_join}
            {mv_join}
            {mvom_join}
            {edit_join}
            {con_s_join}
            {con_sq_join}
            {con_mscp_join}
            {ac_join}
            WHERE m.message_type NOT IN (15, 64) AND cur.message_type IN (15, 64)
        """

        span = max_id - min_id + 1
        chunk_size = max(5000, span // 50)
        progress = ProgressReporter("Preservation", old_count, logger, sys.stderr, 0.2)

        try:
            for start_id in range(min_id, max_id + 1, chunk_size):
                end_id = start_id + chunk_size
                chunk_deleted_ids = [
                    r[0] for r in conn.execute(sql_check_deleted, (start_id, end_id)).fetchall()
                ]
                if chunk_deleted_ids:
                    sub_chunk_size = 500
                    for sc_idx in range(0, len(chunk_deleted_ids), sub_chunk_size):
                        sub_ids = chunk_deleted_ids[sc_idx : sc_idx + sub_chunk_size]
                        ph = ", ".join("?" for _ in sub_ids)
                        hydrate_query = sql_deleted_template.format(placeholders=ph)
                        for r in conn.execute(hydrate_query, sub_ids):
                            row_dict = dict(r)
                            row_dict["preserved_at"] = now_iso
                            row_dict["source_platform"] = "android"
                            row_dict["reactions"] = None
                            row_dict["poll_data"] = None
                            row_dict["service_metadata"] = None
                            extracted_rows.append(row_dict)

                curr_progress = min(
                    old_count, int((min(end_id, max_id + 1) - min_id) / span * old_count)
                )
                progress.update(curr_progress, {"deleted": len(extracted_rows)})

            progress.update(old_count, {"deleted": len(extracted_rows)})
            deleted_count = len(extracted_rows)

            for r in conn.execute(sql_revoked):
                row_dict = dict(r)
                row_dict["preserved_at"] = now_iso
                row_dict["source_platform"] = "android"
                row_dict["reactions"] = None
                row_dict["poll_data"] = None
                row_dict["service_metadata"] = None
                extracted_rows.append(row_dict)

            revoked_count = len(extracted_rows) - deleted_count

            # 3. Hydrate reactions from old_db if available
            if has_add_on and extracted_rows:
                msg_ids = [r["msg_id"] for r in extracted_rows]
                chunk_size_rx = 900
                for i in range(0, len(msg_ids), chunk_size_rx):
                    chunk = msg_ids[i : i + chunk_size_rx]
                    ph = ",".join("?" * len(chunk))
                    rx_sql = f"""
                        SELECT ao.parent_message_row_id AS msg_id,
                               GROUP_CONCAT(r.reaction) AS reactions
                        FROM old_db.message_add_on ao
                        JOIN old_db.message_add_on_reaction r ON r.message_add_on_row_id = ao._id
                        WHERE ao.parent_message_row_id IN ({ph})
                        GROUP BY ao.parent_message_row_id
                    """
                    with contextlib.suppress(sqlite3.OperationalError):
                        rx_map = dict(conn.execute(rx_sql, chunk).fetchall())
                        for row in extracted_rows:
                            if row["msg_id"] in rx_map:
                                row["reactions"] = rx_map[row["msg_id"]]

            # 4. Hydrate interactive poll options and vote counts
            if has_poll_option and extracted_rows:
                poll_rows = [r for r in extracted_rows if r.get("media_type") == "poll"]
                if poll_rows:
                    poll_msg_ids = [r["msg_id"] for r in poll_rows]
                    has_add_on_votes = (
                        "message_add_on_poll_vote_selected_option" in old_tables
                        and "message_add_on" in old_tables
                    )
                    for i in range(0, len(poll_msg_ids), 900):
                        chunk = poll_msg_ids[i : i + 900]
                        ph = ",".join("?" * len(chunk))
                        opt_sql = f"""
                            SELECT _id, message_row_id, option_name, COALESCE(vote_total, 0) AS votes
                            FROM old_db.message_poll_option
                            WHERE message_row_id IN ({ph})
                            ORDER BY message_row_id, _id ASC
                        """
                        opts_by_msg: dict[int, list[dict]] = {}
                        opt_ids: list[int] = []
                        with contextlib.suppress(sqlite3.OperationalError):
                            for opt_r in conn.execute(opt_sql, chunk).fetchall():
                                opt_ids.append(opt_r[0])
                                opts_by_msg.setdefault(opt_r[1], []).append(
                                    {
                                        "id": opt_r[0],
                                        "name": opt_r[2] or "",
                                        "votes": int(opt_r[3] or 0),
                                    }
                                )

                        addon_votes_by_opt: dict[int, int] = {}
                        voter_counts: dict[int, int] = {}
                        if has_add_on_votes and opt_ids:
                            for j in range(0, len(opt_ids), 900):
                                opt_chunk = opt_ids[j : j + 900]
                                opt_ph = ",".join("?" * len(opt_chunk))
                                with contextlib.suppress(sqlite3.OperationalError):
                                    v_rows = conn.execute(
                                        f"""
                                        SELECT ps.message_poll_option_id, COUNT(DISTINCT ps.message_add_on_row_id)
                                        FROM old_db.message_add_on_poll_vote_selected_option ps
                                        WHERE ps.message_poll_option_id IN ({opt_ph})
                                        GROUP BY ps.message_poll_option_id
                                        """,
                                        opt_chunk,
                                    ).fetchall()
                                    for vr in v_rows:
                                        addon_votes_by_opt[vr[0]] = vr[1]

                            with contextlib.suppress(sqlite3.OperationalError):
                                vc_rows = conn.execute(
                                    f"""
                                    SELECT parent_message_row_id, COUNT(DISTINCT _id)
                                    FROM old_db.message_add_on
                                    WHERE parent_message_row_id IN ({ph}) AND message_add_on_type = 67
                                    GROUP BY parent_message_row_id
                                    """,
                                    chunk,
                                ).fetchall()
                                for vcr in vc_rows:
                                    voter_counts[vcr[0]] = vcr[1]

                        poll_meta: dict[int, int] = {}
                        if has_poll:
                            p_sql = f"""
                                SELECT message_row_id, selectable_options_count
                                FROM old_db.message_poll
                                WHERE message_row_id IN ({ph})
                            """
                            with contextlib.suppress(sqlite3.OperationalError):
                                for p_r in conn.execute(p_sql, chunk).fetchall():
                                    poll_meta[p_r[0]] = int(p_r[1] or 1)

                        for r in poll_rows:
                            mid = r["msg_id"]
                            if mid in opts_by_msg:
                                options = opts_by_msg[mid]
                                for o in options:
                                    actual_cnt = max(
                                        o["votes"], addon_votes_by_opt.get(o["id"], 0)
                                    )
                                    o["votes"] = actual_cnt
                                total_votes = sum(o["votes"] for o in options)
                                sel_count = poll_meta.get(mid, 1)
                                poll_payload = {
                                    "selectable_options_count": sel_count,
                                    "options": [
                                        {"name": o["name"], "votes": o["votes"]}
                                        for o in options
                                    ],
                                    "total_votes": total_votes,
                                }
                                r["poll_data"] = json.dumps(
                                    poll_payload, ensure_ascii=False, separators=(",", ":")
                                )

            # 5. Hydrate pinned status and expiry
            if has_pin and has_add_on and extracted_rows:
                msg_ids = [r["msg_id"] for r in extracted_rows]
                for i in range(0, len(msg_ids), 900):
                    chunk = msg_ids[i : i + 900]
                    ph = ",".join("?" * len(chunk))
                    pin_sql = f"""
                        SELECT ao.parent_message_row_id, pic.pin_in_chat_state, ao.expiry_timestamp
                        FROM old_db.message_add_on ao
                        JOIN old_db.message_add_on_pin_in_chat pic ON pic.message_add_on_row_id = ao._id
                        WHERE ao.parent_message_row_id IN ({ph})
                          AND ao.message_add_on_type = 79
                        ORDER BY ao._id DESC
                    """
                    with contextlib.suppress(sqlite3.OperationalError):
                        for pr in conn.execute(pin_sql, chunk).fetchall():
                            mid, pin_state, exp_ts = pr[0], pr[1], pr[2]
                            for r in extracted_rows:
                                if r["msg_id"] == mid and r.get("is_pinned") == 0:
                                    r["is_pinned"] = 1 if pin_state == 1 else 0
                                    r["pin_expiry_ts"] = exp_ts

            # 6. Hydrate admin revocation metadata
            if has_cur_revoked and extracted_rows:
                admin_revoked_rows = [
                    r for r in extracted_rows if r.get("preservation_reason") == "revoked_by_admin"
                ]
                for r in admin_revoked_rows:
                    with contextlib.suppress(sqlite3.OperationalError):
                        rev_info = conn.execute(
                            """
                            SELECT mr.admin_jid_row_id, mr.revoke_timestamp,
                                   COALESCE(j.raw_string, j.user || '@' || j.server, j.user) AS admin_jid,
                                   j.user AS admin_phone
                            FROM message_revoked mr
                            JOIN message cur ON cur._id = mr.message_row_id
                            LEFT JOIN jid j ON j._id = mr.admin_jid_row_id
                            WHERE cur.key_id = ?
                            LIMIT 1
                        """,
                            (r["stanza_id"],),
                        ).fetchone()
                        if rev_info:
                            admin_jid = rev_info[2]
                            admin_phone = rev_info[3]
                            admin_name = ""
                            if has_arch_contacts and admin_phone:
                                c_match = conn.execute(
                                    "SELECT display_name FROM arch.contacts WHERE number = ?",
                                    (admin_phone,),
                                ).fetchone()
                                if c_match and c_match[0]:
                                    admin_name = c_match[0]
                            if not admin_name and admin_phone:
                                admin_name = f"+{admin_phone}"

                            meta_dict = {
                                "revoked_by": "admin",
                                "admin_jid": admin_jid,
                                "admin_name": admin_name,
                                "revoke_timestamp": rev_info[1],
                            }
                            r["service_metadata"] = json.dumps(
                                meta_dict, ensure_ascii=False, separators=(",", ":")
                            )

            # 7. Hydrate system action metadata
            system_rows = [
                r
                for r in extracted_rows
                if r.get("media_type") == "service" or r.get("action_type") is not None
            ]
            for r in system_rows:
                if not r.get("service_metadata") and r.get("action_type") is not None:
                    meta_dict = {
                        "action_type": r.get("action_type"),
                        "participant_name": r.get("participant_name") or "",
                    }
                    r["service_metadata"] = json.dumps(
                        meta_dict, ensure_ascii=False, separators=(",", ":")
                    )

            # Hydrate link preview metadata from old_db.message_text
            if "message_text" in old_tables and extracted_rows:
                mt_msg_ids = [
                    r["msg_id"] for r in extracted_rows if r.get("media_type") in ("link", "text")
                ]
                if mt_msg_ids:
                    chunk_size_mt = 900
                    mt_map = {}
                    for i in range(0, len(mt_msg_ids), chunk_size_mt):
                        chunk = mt_msg_ids[i : i + chunk_size_mt]
                        ph = ",".join("?" * len(chunk))
                        mt_rows = conn.execute(
                            f"""
                            SELECT message_row_id, page_title, description, url, preview_type
                            FROM old_db.message_text
                            WHERE message_row_id IN ({ph})
                            """,
                            chunk,
                        ).fetchall()
                        for mtr in mt_rows:
                            mt_map[mtr[0]] = {
                                "title": mtr[1],
                                "desc": mtr[2],
                                "url": mtr[3],
                                "preview_type": mtr[4] or 0,
                            }
                    for r in extracted_rows:
                        if r["msg_id"] in mt_map:
                            existing_meta = {}
                            if r.get("service_metadata"):
                                try:
                                    existing_meta = json.loads(r["service_metadata"])
                                except Exception:
                                    pass
                            existing_meta["link_preview"] = mt_map[r["msg_id"]]
                            r["service_metadata"] = json.dumps(
                                existing_meta, ensure_ascii=False, separators=(",", ":")
                            )

            # 8. Hydrate deleted group member roster
            if has_group_participants and extracted_rows:
                group_chat_jids: dict[str, int] = {}
                for r in extracted_rows:
                    if (
                        r.get("chat_type") == "group"
                        and r.get("_chat_group_jid_id")
                        and r.get("preservation_reason") == "chat_deleted"
                    ):
                        group_chat_jids[r["chat_id"]] = r["_chat_group_jid_id"]

                for cid, gjid_id in group_chat_jids.items():
                    members = []
                    if "group_participant_user" in old_tables:
                        gpu_cols = {
                            c[1]
                            for c in conn.execute(
                                "PRAGMA old_db.table_info(group_participant_user)"
                            ).fetchall()
                        }
                        rank_expr = "gpu.rank" if "rank" in gpu_cols else "0"
                        gpu_sql = f"""
                            SELECT {sender_jid_sql.replace("j2.", "j_part.").replace("j.", "j_part.")} AS jid,
                                   j_part.user,
                                   {rank_expr} AS rank,
                                   COALESCE(con_p.display_name, '') AS name
                            FROM old_db.group_participant_user gpu
                            JOIN old_db.jid j_part ON j_part._id = gpu.user_jid_row_id
                            LEFT JOIN arch.contacts con_p ON con_p.number = j_part.user
                            WHERE gpu.group_jid_row_id = ?
                        """
                        with contextlib.suppress(sqlite3.OperationalError):
                            for m_row in conn.execute(gpu_sql, (gjid_id,)).fetchall():
                                p_jid = m_row[0] or (
                                    f"{m_row[1]}@s.whatsapp.net" if m_row[1] else ""
                                )
                                p_phone = f"+{m_row[1]}" if m_row[1] else ""
                                members.append(
                                    {
                                        "jid": p_jid,
                                        "phone": p_phone,
                                        "name": m_row[3] or "",
                                        "rank": int(m_row[2] or 0),
                                    }
                                )
                    elif "group_participants" in old_tables:
                        gp_cols = {
                            c[1]
                            for c in conn.execute(
                                "PRAGMA old_db.table_info(group_participants)"
                            ).fetchall()
                        }
                        admin_expr = "gp.admin" if "admin" in gp_cols else "0"
                        gjid_col = "gp.gjid" if "gjid" in gp_cols else "gp.group_jid_row_id"
                        gp_sql = f"""
                            SELECT {sender_jid_sql.replace("j2.", "j_part.").replace("j.", "j_part.")} AS jid,
                                   j_part.user,
                                   {admin_expr} AS rank,
                                   COALESCE(con_p.display_name, '') AS name
                            FROM old_db.group_participants gp
                            JOIN old_db.jid j_part ON j_part._id = gp.jid_row_id
                            LEFT JOIN arch.contacts con_p ON con_p.number = j_part.user
                            WHERE {gjid_col} = ?
                        """
                        with contextlib.suppress(sqlite3.OperationalError):
                            for m_row in conn.execute(gp_sql, (gjid_id,)).fetchall():
                                p_jid = m_row[0] or (
                                    f"{m_row[1]}@s.whatsapp.net" if m_row[1] else ""
                                )
                                p_phone = f"+{m_row[1]}" if m_row[1] else ""
                                members.append(
                                    {
                                        "jid": p_jid,
                                        "phone": p_phone,
                                        "name": m_row[3] or "",
                                        "rank": int(m_row[2] or 0),
                                    }
                                )
                    if members:
                        members_json = json.dumps(
                            members, ensure_ascii=False, separators=(",", ":")
                        )
                        for r in extracted_rows:
                            if r.get("chat_id") == cid:
                                r["_chat_members_json"] = members_json

            progress.finish({"deleted": deleted_count, "revoked": revoked_count})
        except Exception:
            progress.finish({"deleted": len(extracted_rows), "revoked": 0})
            raise

    logger.debug(f"Android diff completed: {len(extracted_rows)} candidates for preservation.")
    return extracted_rows


def diff_and_extract_android_calls(
    old_db_path: str, new_db_path: str, archive_db_path: str, logger: logging.Logger
) -> list[dict]:
    """
    Diff call logs between old_db and new_db for Android.
    Extracts call records present in old_db but absent from new_db.
    """
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    extracted_calls: list[dict] = []

    with contextlib.closing(sqlite3.connect(new_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA cache_size = -32000")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("ATTACH DATABASE ? AS old_db", (old_db_path,))
        conn.execute("ATTACH DATABASE ? AS arch", (archive_db_path,))

        old_tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM old_db.sqlite_master WHERE type='table'"
            ).fetchall()
        }
        new_tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        arch_tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM arch.sqlite_master WHERE type='table'"
            ).fetchall()
        }

        if "call_log" not in old_tables:
            return []

        has_cur_call_log = "call_log" in new_tables
        has_p2 = "call_log_participant_v2" in old_tables
        has_arch_contacts = "contacts" in arch_tables
        has_jid = "jid" in old_tables

        cl_cols = {r[1] for r in conn.execute("PRAGMA old_db.table_info(call_log)").fetchall()}
        if "call_id" not in cl_cols:
            return []

        has_group = "group_jid_row_id" in cl_cols
        has_duration = "duration" in cl_cols
        has_video = "video_call" in cl_cols
        has_result = "call_result" in cl_cols
        has_bytes = "bytes_transferred" in cl_cols
        has_jid_row_id = "jid_row_id" in cl_cols
        has_from_me = "from_me" in cl_cols
        has_ts = "timestamp" in cl_cols

        jid_cols = (
            {r[1] for r in conn.execute("PRAGMA old_db.table_info(jid)").fetchall()}
            if has_jid
            else set()
        )
        has_jid_raw = "raw_string" in jid_cols
        has_jid_server = "server" in jid_cols

        if has_jid_raw and has_jid_server:
            caller_jid_sql = "COALESCE(j_caller.raw_string, CASE WHEN j_caller.server IS NOT NULL THEN j_caller.user || '@' || j_caller.server ELSE j_caller.user END)"
            grp_jid_sql = "COALESCE(j_grp.raw_string, CASE WHEN j_grp.server IS NOT NULL THEN j_grp.user || '@' || j_grp.server ELSE j_grp.user END)"
        elif has_jid_raw:
            caller_jid_sql = "COALESCE(j_caller.raw_string, j_caller.user)"
            grp_jid_sql = "COALESCE(j_grp.raw_string, j_grp.user)"
        elif has_jid_server:
            caller_jid_sql = "CASE WHEN j_caller.server IS NOT NULL THEN j_caller.user || '@' || j_caller.server ELSE j_caller.user END"
            grp_jid_sql = "CASE WHEN j_grp.server IS NOT NULL THEN j_grp.user || '@' || j_grp.server ELSE j_grp.user END"
        else:
            caller_jid_sql = "j_caller.user"
            grp_jid_sql = "j_grp.user"

        j_caller_join = (
            "LEFT JOIN old_db.jid j_caller ON j_caller._id = cl.jid_row_id"
            if (has_jid and has_jid_row_id)
            else "LEFT JOIN old_db.jid j_caller ON 0 = 1"
        )
        j_grp_join = (
            "LEFT JOIN old_db.jid j_grp ON j_grp._id = cl.group_jid_row_id"
            if (has_jid and has_group)
            else "LEFT JOIN old_db.jid j_grp ON 0 = 1"
        )
        con_join = (
            "LEFT JOIN arch.contacts con ON con.number = j_caller.user"
            if (has_arch_contacts and has_jid and has_jid_row_id)
            else ""
        )
        con_name_arm = (
            "NULLIF(con.display_name, ''),"
            if (has_arch_contacts and has_jid and has_jid_row_id)
            else ""
        )

        from_me_expr = "cl.from_me" if has_from_me else "0"
        ts_expr = "COALESCE(cl.timestamp, 0)" if has_ts else "0"
        dur_expr = "COALESCE(cl.duration, 0)" if has_duration else "0"
        video_expr = "COALESCE(cl.video_call, 0)" if has_video else "0"
        result_expr = "COALESCE(cl.call_result, 0)" if has_result else "0"
        bytes_expr = "cl.bytes_transferred" if has_bytes else "NULL"
        is_grp_expr = (
            "CASE WHEN cl.group_jid_row_id IS NOT NULL AND cl.group_jid_row_id > 0 THEN 1 ELSE 0 END"
            if has_group
            else "0"
        )

        chat_id_expr = (
            f"CASE WHEN {is_grp_expr} = 1 THEN COALESCE(j_grp.user, CAST(cl.group_jid_row_id AS TEXT)) ELSE COALESCE(j_caller.user, CAST(cl.jid_row_id AS TEXT)) END"
            if has_jid_row_id
            else "'call'"
        )
        chat_jid_expr = (
            f"CASE WHEN {is_grp_expr} = 1 THEN {grp_jid_sql} ELSE {caller_jid_sql} END"
            if has_jid_row_id
            else "NULL"
        )

        filter_clause = (
            "WHERE cl.call_id IS NOT NULL AND cl.call_id != '' AND NOT EXISTS (SELECT 1 FROM call_log cur_cl WHERE cur_cl.call_id = cl.call_id)"
            if has_cur_call_log
            else "WHERE cl.call_id IS NOT NULL AND cl.call_id != ''"
        )

        sql = f"""
            SELECT
                cl._id AS call_log_row_id,
                cl.call_id AS call_id,
                {chat_id_expr} AS chat_id,
                {chat_jid_expr} AS chat_jid,
                {caller_jid_sql} AS caller_jid,
                COALESCE(
                    {con_name_arm}
                    CASE WHEN j_caller.user IS NOT NULL THEN '+' || j_caller.user END,
                    ''
                ) AS caller_name,
                {from_me_expr} AS from_me,
                {ts_expr} AS timestamp_ms,
                {dur_expr} AS duration_seconds,
                {video_expr} AS is_video,
                {result_expr} AS call_result,
                {is_grp_expr} AS is_group_call,
                {bytes_expr} AS bytes_transferred
            FROM old_db.call_log cl
            {j_caller_join}
            {j_grp_join}
            {con_join}
            {filter_clause}
        """

        try:
            call_rows = conn.execute(sql).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning(f"Call log diff query failed ({e}); skipping call preservation.")
            return []

        if not call_rows:
            return []

        participants_by_call: dict[int, list[dict]] = {}
        if has_p2:
            row_ids = [r["call_log_row_id"] for r in call_rows]
            for i in range(0, len(row_ids), 900):
                chunk = row_ids[i : i + 900]
                ph = ",".join("?" * len(chunk))
                p_sql = f"""
                    SELECT
                        p.call_log_row_id,
                        {caller_jid_sql.replace("j_caller", "j_part")} AS jid,
                        j_part.user AS phone,
                        p.call_result,
                        COALESCE(con_p.display_name, '') AS name
                    FROM old_db.call_log_participant_v2 p
                    LEFT JOIN old_db.jid j_part ON j_part._id = p.jid_row_id
                    LEFT JOIN arch.contacts con_p ON con_p.number = j_part.user
                    WHERE p.call_log_row_id IN ({ph})
                """
                with contextlib.suppress(sqlite3.OperationalError):
                    for pr in conn.execute(p_sql, chunk).fetchall():
                        cid = pr[0]
                        p_jid = pr[1] or (f"{pr[2]}@s.whatsapp.net" if pr[2] else "")
                        p_phone = f"+{pr[2]}" if pr[2] else ""
                        participants_by_call.setdefault(cid, []).append(
                            {
                                "jid": p_jid,
                                "phone": p_phone,
                                "name": pr[4] or "",
                                "call_result": int(pr[3] or 0),
                            }
                        )

        for r in call_rows:
            c_dict = dict(r)
            c_dict["preserved_at"] = now_iso
            c_dict["source_platform"] = "android"
            call_row_id = c_dict.pop("call_log_row_id", None)
            if call_row_id in participants_by_call:
                c_dict["participants"] = json.dumps(
                    participants_by_call[call_row_id],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            else:
                c_dict["participants"] = None
            extracted_calls.append(c_dict)

    logger.debug(
        f"Android call diff completed: {len(extracted_calls)} call candidate(s) for preservation."
    )
    return extracted_calls


def _read_protobuf_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Read a varint from protobuf byte data starting at offset."""
    res = 0
    shift = 0
    while offset < len(data):
        b = data[offset]
        offset += 1
        res |= (b & 0x7F) << shift
        if not (b & 0x80):
            return res, offset
        shift += 7
        if shift > 64:
            raise ValueError("Varint overflow")
    raise ValueError("Truncated varint")


def _parse_ios_receipt_reactions(data: bytes) -> str | None:
    """Extract comma-separated reactions string from iOS ZRECEIPTINFO protobuf blob."""
    if not data:
        return None
    try:
        offset = 0
        emojis: list[str] = []
        while offset < len(data):
            tag, offset = _read_protobuf_varint(data, offset)
            field_num = tag >> 3
            wire_type = tag & 0x07
            if wire_type == 0:
                _, offset = _read_protobuf_varint(data, offset)
            elif wire_type == 1:
                offset += 8
            elif wire_type == 2:
                length, offset = _read_protobuf_varint(data, offset)
                val = data[offset : offset + length]
                offset += length
                if field_num == 7:  # Field 7: repeated reactions group
                    r_offset = 0
                    while r_offset < len(val):
                        r_tag, r_offset = _read_protobuf_varint(val, r_offset)
                        r_wire = r_tag & 0x07
                        if r_wire == 0:
                            _, r_offset = _read_protobuf_varint(val, r_offset)
                        elif r_wire == 1:
                            r_offset += 8
                        elif r_wire == 2:
                            r_len, r_offset = _read_protobuf_varint(val, r_offset)
                            r_bytes = val[r_offset : r_offset + r_len]
                            r_offset += r_len
                            sub_offset = 0
                            while sub_offset < len(r_bytes):
                                s_tag, sub_offset = _read_protobuf_varint(r_bytes, sub_offset)
                                s_num = s_tag >> 3
                                s_wire = s_tag & 0x07
                                if s_wire == 0:
                                    _, sub_offset = _read_protobuf_varint(r_bytes, sub_offset)
                                elif s_wire == 1:
                                    sub_offset += 8
                                elif s_wire == 2:
                                    s_len, sub_offset = _read_protobuf_varint(r_bytes, sub_offset)
                                    s_bytes = r_bytes[sub_offset : sub_offset + s_len]
                                    sub_offset += s_len
                                    if s_num in (2, 3):
                                        emoji_cand = s_bytes.decode(
                                            "utf-8", errors="ignore"
                                        ).strip()
                                        if emoji_cand and not emoji_cand.endswith(
                                            ("@s.whatsapp.net", "@lid")
                                        ):
                                            emojis.append(emoji_cand)
                                elif s_wire == 5:
                                    sub_offset += 4
                                else:
                                    break
                        elif r_wire == 5:
                            r_offset += 4
                        else:
                            break
            elif wire_type == 5:
                offset += 4
            else:
                break
        return ",".join(emojis) if emojis else None
    except Exception:
        return None


def _parse_ios_receipt_poll(data: bytes) -> str | None:
    """Extract poll_data JSON string from iOS ZRECEIPTINFO protobuf blob."""
    if not data:
        return None
    try:
        offset = 0
        poll_blob = None
        while offset < len(data):
            tag, offset = _read_protobuf_varint(data, offset)
            field_num = tag >> 3
            wire_type = tag & 0x07
            if wire_type == 0:
                _, offset = _read_protobuf_varint(data, offset)
            elif wire_type == 1:
                offset += 8
            elif wire_type == 2:
                length, offset = _read_protobuf_varint(data, offset)
                val = data[offset : offset + length]
                offset += length
                if field_num == 8:  # Field 8: poll envelope
                    poll_blob = val
            elif wire_type == 5:
                offset += 4
            else:
                break

        if not poll_blob:
            return None

        p_offset = 0
        options: list[str] = []
        selectable_count = 1
        votes: list[list[int]] = []

        while p_offset < len(poll_blob):
            p_tag, p_offset = _read_protobuf_varint(poll_blob, p_offset)
            p_num = p_tag >> 3
            p_wire = p_tag & 0x07
            if p_wire == 0:
                val, p_offset = _read_protobuf_varint(poll_blob, p_offset)
                if p_num == 4:
                    selectable_count = val
            elif p_wire == 1:
                p_offset += 8
            elif p_wire == 2:
                p_len, p_offset = _read_protobuf_varint(poll_blob, p_offset)
                p_data = poll_blob[p_offset : p_offset + p_len]
                p_offset += p_len
                if p_num == 3:  # Option submessage
                    opt_offset = 0
                    opt_name = ""
                    while opt_offset < len(p_data):
                        o_tag, opt_offset = _read_protobuf_varint(p_data, opt_offset)
                        o_num = o_tag >> 3
                        o_wire = o_tag & 0x07
                        if o_wire == 0:
                            _, opt_offset = _read_protobuf_varint(p_data, opt_offset)
                        elif o_wire == 1:
                            opt_offset += 8
                        elif o_wire == 2:
                            o_len, opt_offset = _read_protobuf_varint(p_data, opt_offset)
                            o_bytes = p_data[opt_offset : opt_offset + o_len]
                            opt_offset += o_len
                            if o_num == 1:
                                opt_name = o_bytes.decode("utf-8", errors="replace")
                        elif o_wire == 5:
                            opt_offset += 4
                        else:
                            break
                    if opt_name:
                        options.append(opt_name)
                elif p_num == 5:  # Vote submessage
                    v_offset = 0
                    v_opt_indices: list[int] = []
                    while v_offset < len(p_data):
                        v_tag, v_offset = _read_protobuf_varint(p_data, v_offset)
                        v_num = v_tag >> 3
                        v_wire = v_tag & 0x07
                        if v_wire == 0:
                            v_val, v_offset = _read_protobuf_varint(p_data, v_offset)
                            if v_num == 1:
                                v_opt_indices.append(v_val)
                        elif v_wire == 1:
                            v_offset += 8
                        elif v_wire == 2:
                            v_len, v_offset = _read_protobuf_varint(p_data, v_offset)
                            v_bytes = p_data[v_offset : v_offset + v_len]
                            v_offset += v_len
                            if v_num == 1:
                                p_idx = 0
                                while p_idx < len(v_bytes):
                                    idx_val, p_idx = _read_protobuf_varint(v_bytes, p_idx)
                                    v_opt_indices.append(idx_val)
                        elif v_wire == 5:
                            v_offset += 4
                        else:
                            break
                    votes.append(v_opt_indices)
            elif p_wire == 5:
                p_offset += 4
            else:
                break

        if not options:
            return None

        option_votes = [0] * len(options)
        for voter_choices in votes:
            for idx in voter_choices:
                if 0 <= idx < len(options):
                    option_votes[idx] += 1

        poll_payload = {
            "selectable_options_count": selectable_count,
            "multiple_answers": (selectable_count == 0),
            "options": [
                {
                    "id": i,
                    "name": opt,
                    "votes": option_votes[i],
                    "vote_count": option_votes[i],
                }
                for i, opt in enumerate(options)
            ],
            "total_votes": sum(option_votes),
            "total_voters": len(votes),
        }
        return json.dumps(poll_payload, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return None


def diff_and_extract_ios(
    old_db_path: str, new_db_path: str, archive_db_path: str, logger: logging.Logger
) -> list[dict]:
    """
    Diff an iOS ChatStorage.sqlite against incoming new_db and extract deleted/revoked messages.
    Returns a list of normalized message dicts ready for insertion into preserved_messages.
    """
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    extracted_rows: list[dict] = []

    with contextlib.closing(sqlite3.connect(new_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA cache_size = -64000")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA mmap_size = 268435456")
        conn.execute("ATTACH DATABASE ? AS old_db", (old_db_path,))
        conn.execute("ATTACH DATABASE ? AS arch", (archive_db_path,))
        _ensure_ios_diff_indexes(conn)

        old_tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM old_db.sqlite_master WHERE type='table'"
            ).fetchall()
        }
        arch_tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM arch.sqlite_master WHERE type='table'"
            ).fetchall()
        }
        old_cols = {r[1] for r in conn.execute("PRAGMA old_db.table_info(ZWAMESSAGE)").fetchall()}
        has_parent = "ZPARENTMESSAGE" in old_cols
        has_media = "ZWAMEDIAITEM" in old_tables
        has_profile = "ZWAPROFILEPUSHNAME" in old_tables
        has_chat_session = "ZWACHATSESSION" in old_tables
        has_group_info = "ZWAGROUPINFO" in old_tables
        has_group_member = "ZWAGROUPMEMBER" in old_tables
        has_message_info = "ZWAMESSAGEINFO" in old_tables
        has_arch_contacts = "contacts" in arch_tables
        has_arch_copies = "archive_copies" in arch_tables

        mi_cols = (
            {r[1] for r in conn.execute("PRAGMA old_db.table_info(ZWAMEDIAITEM)").fetchall()}
            if has_media
            else set()
        )
        duration_col = "mi.ZMOVIEDURATION" if "ZMOVIEDURATION" in mi_cols else "NULL"
        filesize_col = "mi.ZFILESIZE" if "ZFILESIZE" in mi_cols else "NULL"
        lat_col = "mi.ZLATITUDE" if "ZLATITUDE" in mi_cols else "NULL"
        lng_col = "mi.ZLONGITUDE" if "ZLONGITUDE" in mi_cols else "NULL"
        title_col = "mi.ZTITLE" if "ZTITLE" in mi_cols else "NULL"
        url_col = "mi.ZMEDIAURL" if "ZMEDIAURL" in mi_cols else "NULL"
        vcard_data_col = "mi.ZVCARDSTRING" if "ZVCARDSTRING" in mi_cols else "NULL"
        media_path_col = (
            "COALESCE('Message/' || mi.ZMEDIALOCALPATH, 'vcard:' || m.Z_PK)"
            if "ZMEDIALOCALPATH" in mi_cols
            else "('vcard:' || m.Z_PK)"
        )
        media_name_col = (
            """COALESCE(
                CASE WHEN m.ZMESSAGETYPE = 8 THEN NULLIF(m.ZTEXT, '') END,
                mi.ZMEDIALOCALPATH,
                ''
            )"""
            if (has_media and "ZMEDIALOCALPATH" in mi_cols)
            else "CASE WHEN m.ZMESSAGETYPE = 8 THEN COALESCE(m.ZTEXT, '') ELSE '' END"
        )
        if "ZMEDIALOCALPATH" in mi_cols and "ZTITLE" in mi_cols:
            media_local_expr = "COALESCE(mi.ZMEDIALOCALPATH, mi.ZTITLE, '')"
        elif "ZMEDIALOCALPATH" in mi_cols:
            media_local_expr = "COALESCE(mi.ZMEDIALOCALPATH, '')"
        elif "ZTITLE" in mi_cols:
            media_local_expr = "COALESCE(mi.ZTITLE, '')"
        else:
            media_local_expr = "''"

        cs_cols = (
            {r[1] for r in conn.execute("PRAGMA old_db.table_info(ZWACHATSESSION)").fetchall()}
            if has_chat_session
            else set()
        )
        chat_partner_expr = "NULLIF(cs.ZPARTNERNAME, '')" if "ZPARTNERNAME" in cs_cols else "NULL"
        chat_contact_jid_expr = "cs.ZCONTACTJID" if "ZCONTACTJID" in cs_cols else "NULL"
        chat_group_info_expr = "cs.ZGROUPINFO" if "ZGROUPINFO" in cs_cols else "NULL"

        gi_cols = (
            {r[1] for r in conn.execute("PRAGMA old_db.table_info(ZWAGROUPINFO)").fetchall()}
            if has_group_info
            else set()
        )
        if has_group_info:
            if "ZCHATSESSION" in gi_cols:
                gi_join = "LEFT JOIN old_db.ZWAGROUPINFO gi ON gi.ZCHATSESSION = m.ZCHATSESSION"
            elif "ZGROUPINFO" in cs_cols:
                gi_join = "LEFT JOIN old_db.ZWAGROUPINFO gi ON gi.Z_PK = cs.ZGROUPINFO"
            else:
                gi_join = ""
            created_ts_col = (
                "CAST((gi.ZCREATIONDATE + 978307200) * 1000 AS INTEGER)"
                if "ZCREATIONDATE" in gi_cols
                else "NULL"
            )
            creator_jid_col = "gi.ZCREATORJID" if "ZCREATORJID" in gi_cols else "NULL"
        else:
            gi_join = ""
            created_ts_col = "NULL"
            creator_jid_col = "NULL"

        starred_col = "COALESCE(m.ZSTARRED, 0)" if "ZSTARRED" in old_cols else "0"
        action_type_col = "m.ZGROUPEVENTTYPE" if "ZGROUPEVENTTYPE" in old_cols else "NULL"
        msg_info_col = "m.ZMESSAGEINFO" if "ZMESSAGEINFO" in old_cols else "NULL"

        mi_join = "LEFT JOIN old_db.ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM" if has_media else ""
        pp_join = (
            "LEFT JOIN old_db.ZWAPROFILEPUSHNAME pp_phone ON pp_phone.ZJID = m.ZFROMJID"
            if has_profile
            else ""
        )
        qm_join = (
            "LEFT JOIN old_db.ZWAMESSAGE qm ON qm.Z_PK = m.ZPARENTMESSAGE" if has_parent else ""
        )
        con_sq_join = (
            "LEFT JOIN arch.contacts con_sq ON con_sq.number = SUBSTR(COALESCE(qm.ZFROMJID,''), 1, "
            "INSTR(COALESCE(qm.ZFROMJID,'') || '@', '@') - 1)"
            if (has_parent and has_arch_contacts)
            else ""
        )
        con_s_join = (
            "LEFT JOIN arch.contacts con_s ON con_s.number = SUBSTR(COALESCE(m.ZFROMJID,''), 1, "
            "INSTR(COALESCE(m.ZFROMJID,'') || '@', '@') - 1)"
            if has_arch_contacts
            else ""
        )
        con_s_arm = "NULLIF(con_s.display_name, '')," if has_arch_contacts else ""
        con_sq_arm = (
            "NULLIF(con_sq.display_name, '')," if (has_parent and has_arch_contacts) else ""
        )

        if has_parent:
            quoted_stanza_col = "qm.ZSTANZAID" if "ZSTANZAID" in old_cols else "NULL"
            if "ZMESSAGETYPE" in old_cols:
                quoted_parent_type_col = """CASE
                    WHEN qm.ZMESSAGETYPE IN (1, 38) THEN 'image'
                    WHEN qm.ZMESSAGETYPE = 2 THEN 'audio'
                    WHEN qm.ZMESSAGETYPE IN (3, 39, 54) THEN 'video'
                    WHEN qm.ZMESSAGETYPE IN (4, 14) THEN 'vcard'
                    WHEN qm.ZMESSAGETYPE = 5 THEN 'location'
                    WHEN qm.ZMESSAGETYPE = 8 THEN 'document'
                    WHEN qm.ZMESSAGETYPE = 11 THEN 'gif'
                    WHEN qm.ZMESSAGETYPE = 15 THEN 'sticker'
                    WHEN qm.ZMESSAGETYPE = 46 THEN 'poll'
                    WHEN qm.ZMESSAGETYPE = 0 THEN 'text'
                    ELSE NULL
                END"""
                quoted_text_col = """CASE
                    WHEN qm.ZTEXT IS NOT NULL AND qm.ZTEXT != '' THEN qm.ZTEXT
                    WHEN qm.ZMESSAGETYPE IN (1, 38) THEN '📷 Photo'
                    WHEN qm.ZMESSAGETYPE = 2 THEN '🎵 Audio'
                    WHEN qm.ZMESSAGETYPE IN (3, 39, 54) THEN '🎥 Video'
                    WHEN qm.ZMESSAGETYPE IN (4, 14) THEN '👤 Contact card'
                    WHEN qm.ZMESSAGETYPE = 5 THEN '📍 Location'
                    WHEN qm.ZMESSAGETYPE = 15 THEN 'Sticker'
                    WHEN qm.ZMESSAGETYPE = 46 THEN '📊 Poll'
                    ELSE ''
                END"""
            elif "ZTEXT" in old_cols:
                quoted_parent_type_col = "NULL"
                quoted_text_col = "COALESCE(qm.ZTEXT, '')"
            else:
                quoted_parent_type_col = "NULL"
                quoted_text_col = "''"
            quoted_sender_col = (
                "CASE WHEN qm.ZISFROMME = 1 THEN 'You' "
                f"ELSE COALESCE({con_sq_arm} NULLIF(qm.ZPUSHNAME, ''), '') END"
            )
            quoted_ts_col = "COALESCE(CAST((qm.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER), 0)"
        else:
            quoted_stanza_col = "NULL"
            quoted_parent_type_col = "NULL"
            quoted_text_col = "''"
            quoted_sender_col = "''"
            quoted_ts_col = "0"

        pp_arm = "NULLIF(pp_phone.ZPUSHNAME, '')," if has_profile else ""
        ac_join = (
            f"""LEFT JOIN arch.archive_copies ac ON ac.original_path IN (
                {media_path_col},
                CASE WHEN {media_local_expr} != '' THEN {media_local_expr} END,
                'vcard:' || m.Z_PK
            )"""
            if has_arch_copies
            else ""
        )
        ac_path_col = "ac.archive_path" if has_arch_copies else "NULL"

        media_type_expr = f"""CASE
            WHEN m.ZMESSAGETYPE IN (6, 10) THEN 'service'
            WHEN m.ZMESSAGETYPE IN (4, 14) THEN 'vcard'
            WHEN m.ZMESSAGETYPE = 5 THEN 'location'
            WHEN m.ZMESSAGETYPE = 7 THEN 'link'
            WHEN m.ZMESSAGETYPE = 46 THEN 'poll'
            WHEN m.ZMESSAGETYPE = 15 THEN 'sticker'
            WHEN m.ZMESSAGETYPE IN (1, 38) THEN 'image'
            WHEN m.ZMESSAGETYPE = 2 THEN 'audio'
            WHEN m.ZMESSAGETYPE IN (3, 39, 54) THEN 'video'
            WHEN m.ZMESSAGETYPE = 8 THEN 'document'
            WHEN m.ZMESSAGETYPE = 11 THEN 'gif'
            WHEN m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0 THEN
                CASE WHEN m.ZTEXT IS NOT NULL
                      AND (INSTR(LOWER(m.ZTEXT), 'http://') > 0
                           OR INSTR(LOWER(m.ZTEXT), 'https://') > 0)
                     THEN 'link' ELSE 'text' END
            ELSE
                CASE
                    WHEN LOWER({media_local_expr}) LIKE '%.vcf' THEN 'vcard'
                    WHEN LOWER({media_local_expr}) LIKE '%sticker%'
                      OR LOWER({media_local_expr}) LIKE '%.webp'
                      OR LOWER({media_local_expr}) LIKE '%.was' THEN 'sticker'
                    WHEN LOWER({media_local_expr}) LIKE '%.jpg'
                      OR LOWER({media_local_expr}) LIKE '%.jpeg'
                      OR LOWER({media_local_expr}) LIKE '%.png' THEN 'image'
                    WHEN LOWER({media_local_expr}) LIKE '%.mp4'
                      OR LOWER({media_local_expr}) LIKE '%.mov' THEN 'video'
                    WHEN LOWER({media_local_expr}) LIKE '%.opus'
                      OR LOWER({media_local_expr}) LIKE '%.m4a'
                      OR LOWER({media_local_expr}) LIKE '%.mp3' THEN 'audio'
                    ELSE 'document'
                END
        END"""

        is_view_once_expr = """CASE
            WHEN m.ZMESSAGETYPE IN (38, 39, 53) THEN 1
            ELSE 0
        END"""

        row_span = conn.execute(
            "SELECT MIN(Z_PK), MAX(Z_PK), COUNT(*) FROM old_db.ZWAMESSAGE"
        ).fetchone()
        min_id, max_id, old_count = row_span[0], row_span[1], row_span[2]
        new_count = conn.execute("SELECT COUNT(*) FROM ZWAMESSAGE").fetchone()[0]
        is_device_reset = (old_count >= 100) and (new_count < old_count * 0.1)

        if old_count == 0 or min_id is None or max_id is None:
            return []

        sql_deleted = f"""
            SELECT
                m.Z_PK AS msg_id,
                COALESCE(NULLIF(m.ZSTANZAID, ''), 'msg_' || m.Z_PK) AS stanza_id,
                COALESCE(
                    (SELECT CAST(cur_cs.Z_PK AS TEXT) FROM ZWACHATSESSION cur_cs WHERE cur_cs.ZCONTACTJID = cs.ZCONTACTJID),
                    CAST(m.ZCHATSESSION AS TEXT)
                ) AS chat_id,
                CASE WHEN {chat_group_info_expr} IS NOT NULL THEN 'group' ELSE 'contact' END AS chat_type,
                {chat_contact_jid_expr} AS chat_jid,
                CAST((m.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER) AS timestamp_ms,
                COALESCE(
                    {con_s_arm}
                    {pp_arm}
                    {chat_partner_expr},
                    CASE WHEN m.ZFROMJID NOT LIKE '%@g.us' THEN NULLIF(m.ZPUSHNAME, '') END,
                    CASE WHEN m.ZFROMJID IS NOT NULL THEN '+' || SUBSTR(m.ZFROMJID, 1, INSTR(m.ZFROMJID || '@', '@') - 1) END,
                    ''
                ) AS sender,
                COALESCE(m.ZFROMJID, {chat_contact_jid_expr}) AS sender_jid,
                SUBSTR(COALESCE(m.ZFROMJID, {chat_contact_jid_expr}, ''), 1, INSTR(COALESCE(m.ZFROMJID, {chat_contact_jid_expr}, '') || '@', '@') - 1) AS sender_phone,
                m.ZISFROMME AS from_me,
                COALESCE(
                    CASE
                        WHEN m.ZMESSAGETYPE = 8 THEN NULLIF({title_col}, '')
                        WHEN m.ZMESSAGETYPE IN (1, 3, 11, 38, 39, 54) THEN COALESCE(NULLIF(m.ZTEXT, ''), NULLIF({title_col}, ''))
                        ELSE m.ZTEXT
                    END,
                    ''
                ) AS text_body,
                {media_type_expr} AS media_type,
                {media_name_col} AS media_name,
                {ac_path_col} AS archive_path,
                {media_path_col} AS original_path,
                {vcard_data_col} AS vcard_data,
                NULL AS media_width,
                NULL AS media_height,
                {duration_col} AS media_duration,
                {filesize_col} AS media_size,
                {is_view_once_expr} AS is_view_once,
                {lat_col} AS latitude,
                {lng_col} AS longitude,
                {title_col} AS location_name,
                NULL AS location_address,
                {url_col} AS location_url,
                NULL AS live_location_share_duration,
                {quoted_text_col} AS quoted_text,
                {quoted_sender_col} AS quoted_sender,
                {quoted_ts_col} AS quoted_ts,
                {quoted_stanza_col} AS quoted_stanza_id,
                {quoted_parent_type_col} AS quoted_parent_type,
                0 AS is_edited,
                NULL AS edited_ts,
                {starred_col} AS is_starred,
                {action_type_col} AS action_type,
                '' AS participant_name,
                {chat_partner_expr} AS _chat_subject,
                NULL AS _chat_description,
                {created_ts_col} AS _chat_created_ts,
                {creator_jid_col} AS _chat_creator_jid,
                m.ZCHATSESSION AS _chat_session_pk,
                {msg_info_col} AS _msg_info_pk,
                CASE
                    WHEN {is_device_reset} THEN 'device_reset'
                    WHEN NOT EXISTS (
                        SELECT 1 FROM ZWACHATSESSION cur_cs WHERE cur_cs.ZCONTACTJID = cs.ZCONTACTJID
                    ) AND NOT EXISTS (
                        SELECT 1 FROM ZWACHATSESSION cur_cs WHERE cur_cs.Z_PK = m.ZCHATSESSION
                    ) THEN 'chat_deleted'
                    ELSE 'deleted_on_phone'
                END AS preservation_reason
            FROM old_db.ZWAMESSAGE m
            {mi_join}
            LEFT JOIN old_db.ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
            {gi_join}
            {pp_join}
            {qm_join}
            {con_s_join}
            {con_sq_join}
            {ac_join}
            WHERE m.Z_PK >= ? AND m.Z_PK < ? AND (
                (
                    m.ZSTANZAID IS NOT NULL AND m.ZSTANZAID != ''
                    AND NOT EXISTS (SELECT 1 FROM ZWAMESSAGE cur WHERE cur.ZSTANZAID = m.ZSTANZAID)
                ) OR (
                    (m.ZSTANZAID IS NULL OR m.ZSTANZAID = '')
                    AND NOT EXISTS (
                        SELECT 1 FROM ZWAMESSAGE cur
                        WHERE cur.ZCHATSESSION = m.ZCHATSESSION
                          AND cur.ZMESSAGEDATE = m.ZMESSAGEDATE
                          AND cur.ZISFROMME = m.ZISFROMME
                    )
                )
            )
        """

        sql_revoked = f"""
            SELECT
                m.Z_PK AS msg_id,
                COALESCE(NULLIF(m.ZSTANZAID, ''), 'msg_' || m.Z_PK) AS stanza_id,
                COALESCE(
                    (SELECT CAST(cur_cs.Z_PK AS TEXT) FROM ZWACHATSESSION cur_cs WHERE cur_cs.ZCONTACTJID = cs.ZCONTACTJID),
                    CAST(m.ZCHATSESSION AS TEXT)
                ) AS chat_id,
                CASE WHEN {chat_group_info_expr} IS NOT NULL THEN 'group' ELSE 'contact' END AS chat_type,
                {chat_contact_jid_expr} AS chat_jid,
                CAST((m.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER) AS timestamp_ms,
                COALESCE(
                    {con_s_arm}
                    {pp_arm}
                    {chat_partner_expr},
                    CASE WHEN m.ZFROMJID NOT LIKE '%@g.us' THEN NULLIF(m.ZPUSHNAME, '') END,
                    CASE WHEN m.ZFROMJID IS NOT NULL THEN '+' || SUBSTR(m.ZFROMJID, 1, INSTR(m.ZFROMJID || '@', '@') - 1) END,
                    ''
                ) AS sender,
                COALESCE(m.ZFROMJID, {chat_contact_jid_expr}) AS sender_jid,
                SUBSTR(COALESCE(m.ZFROMJID, {chat_contact_jid_expr}, ''), 1, INSTR(COALESCE(m.ZFROMJID, {chat_contact_jid_expr}, '') || '@', '@') - 1) AS sender_phone,
                m.ZISFROMME AS from_me,
                COALESCE(
                    CASE
                        WHEN m.ZMESSAGETYPE = 8 THEN NULLIF({title_col}, '')
                        WHEN m.ZMESSAGETYPE IN (1, 3, 11, 38, 39, 54) THEN COALESCE(NULLIF(m.ZTEXT, ''), NULLIF({title_col}, ''))
                        ELSE m.ZTEXT
                    END,
                    ''
                ) AS text_body,
                {media_type_expr} AS media_type,
                {media_name_col} AS media_name,
                {ac_path_col} AS archive_path,
                {media_path_col} AS original_path,
                {vcard_data_col} AS vcard_data,
                NULL AS media_width,
                NULL AS media_height,
                {duration_col} AS media_duration,
                {filesize_col} AS media_size,
                {is_view_once_expr} AS is_view_once,
                {lat_col} AS latitude,
                {lng_col} AS longitude,
                {title_col} AS location_name,
                NULL AS location_address,
                {url_col} AS location_url,
                NULL AS live_location_share_duration,
                {quoted_text_col} AS quoted_text,
                {quoted_sender_col} AS quoted_sender,
                {quoted_ts_col} AS quoted_ts,
                {quoted_stanza_col} AS quoted_stanza_id,
                {quoted_parent_type_col} AS quoted_parent_type,
                0 AS is_edited,
                NULL AS edited_ts,
                {starred_col} AS is_starred,
                {action_type_col} AS action_type,
                '' AS participant_name,
                {chat_partner_expr} AS _chat_subject,
                NULL AS _chat_description,
                {created_ts_col} AS _chat_created_ts,
                {creator_jid_col} AS _chat_creator_jid,
                m.ZCHATSESSION AS _chat_session_pk,
                {msg_info_col} AS _msg_info_pk,
                'revoked_by_sender' AS preservation_reason
            FROM old_db.ZWAMESSAGE m
            JOIN ZWAMESSAGE cur ON cur.ZSTANZAID = m.ZSTANZAID
            {mi_join}
            LEFT JOIN old_db.ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
            {gi_join}
            {pp_join}
            {qm_join}
            {con_s_join}
            {con_sq_join}
            {ac_join}
            WHERE m.ZMESSAGETYPE != 14 AND cur.ZMESSAGETYPE = 14
        """

        span = max_id - min_id + 1
        chunk_size = max(5000, span // 50)
        progress = ProgressReporter("Preservation", old_count, logger, sys.stderr, 0.2)

        try:
            for start_id in range(min_id, max_id + 1, chunk_size):
                end_id = start_id + chunk_size
                for r in conn.execute(sql_deleted, (start_id, end_id)):
                    row_dict = dict(r)
                    row_dict["preserved_at"] = now_iso
                    row_dict["source_platform"] = "ios"
                    row_dict["reactions"] = None
                    row_dict["poll_data"] = None
                    row_dict["service_metadata"] = None
                    extracted_rows.append(row_dict)

                curr_progress = min(
                    old_count, int((min(end_id, max_id + 1) - min_id) / span * old_count)
                )
                progress.update(curr_progress, {"deleted": len(extracted_rows)})

            progress.update(old_count, {"deleted": len(extracted_rows)})
            deleted_count = len(extracted_rows)

            for r in conn.execute(sql_revoked):
                row_dict = dict(r)
                row_dict["preserved_at"] = now_iso
                row_dict["source_platform"] = "ios"
                row_dict["reactions"] = None
                row_dict["poll_data"] = None
                row_dict["service_metadata"] = None
                extracted_rows.append(row_dict)

            revoked_count = len(extracted_rows) - deleted_count

            # 3. Hydrate reactions and polls from ZWAMESSAGEINFO.ZRECEIPTINFO if available
            if has_message_info and extracted_rows:
                minfo_cols = {
                    r[1]
                    for r in conn.execute("PRAGMA old_db.table_info(ZWAMESSAGEINFO)").fetchall()
                }
                if "ZRECEIPTINFO" in minfo_cols:
                    info_pks = [r["_msg_info_pk"] for r in extracted_rows if r.get("_msg_info_pk")]
                    info_map: dict[int, bytes] = {}
                    if info_pks:
                        for i in range(0, len(info_pks), 900):
                            chunk = info_pks[i : i + 900]
                            ph = ",".join("?" * len(chunk))
                            sql = f"SELECT Z_PK, ZRECEIPTINFO FROM old_db.ZWAMESSAGEINFO WHERE Z_PK IN ({ph}) AND ZRECEIPTINFO IS NOT NULL"
                            with contextlib.suppress(sqlite3.OperationalError):
                                info_map.update(dict(conn.execute(sql, chunk).fetchall()))

                    if "ZMESSAGE" in minfo_cols:
                        m_pks = [r["msg_id"] for r in extracted_rows if not r.get("reactions")]
                        if m_pks:
                            for i in range(0, len(m_pks), 900):
                                chunk = m_pks[i : i + 900]
                                ph = ",".join("?" * len(chunk))
                                sql = f"SELECT ZMESSAGE, ZRECEIPTINFO FROM old_db.ZWAMESSAGEINFO WHERE ZMESSAGE IN ({ph}) AND ZRECEIPTINFO IS NOT NULL"
                                with contextlib.suppress(sqlite3.OperationalError):
                                    for row in conn.execute(sql, chunk).fetchall():
                                        info_map[row[0]] = row[1]

                    for r in extracted_rows:
                        blob = info_map.get(r.get("_msg_info_pk")) or info_map.get(r.get("msg_id"))
                        if blob:
                            if not r.get("reactions"):
                                r["reactions"] = _parse_ios_receipt_reactions(blob)
                            if r.get("media_type") == "poll" and not r.get("poll_data"):
                                r["poll_data"] = _parse_ios_receipt_poll(blob)

            # 4. Hydrate deleted group member roster
            if has_group_member and extracted_rows:
                del_group_sessions = {
                    r["chat_id"]: r["_chat_session_pk"]
                    for r in extracted_rows
                    if r.get("chat_type") == "group"
                    and r.get("preservation_reason") == "chat_deleted"
                    and r.get("_chat_session_pk")
                }
                for cid, cs_pk in del_group_sessions.items():
                    with contextlib.suppress(sqlite3.OperationalError):
                        members_sql = """
                            SELECT gm.ZMEMBERJID,
                                   COALESCE(con.display_name, gm.ZCONTACTNAME, gm.ZFIRSTNAME, '') AS name,
                                   COALESCE(gm.ZISADMIN, 0) AS rank
                            FROM old_db.ZWAGROUPMEMBER gm
                            LEFT JOIN arch.contacts con ON con.number = SUBSTR(gm.ZMEMBERJID, 1, INSTR(gm.ZMEMBERJID || '@', '@') - 1)
                            WHERE gm.ZCHATSESSION = ? AND gm.ZISACTIVE = 1
                        """
                        members = []
                        for m_row in conn.execute(members_sql, (cs_pk,)).fetchall():
                            m_jid = m_row[0] or ""
                            m_phone = m_jid.split("@")[0] if "@" in m_jid else ""
                            members.append(
                                {
                                    "jid": m_jid,
                                    "phone": f"+{m_phone}" if m_phone else "",
                                    "name": m_row[1] or "",
                                    "rank": int(m_row[2] or 0),
                                }
                            )
                        if members:
                            members_json = json.dumps(
                                members, ensure_ascii=False, separators=(",", ":")
                            )
                            for r in extracted_rows:
                                if r.get("chat_id") == cid:
                                    r["_chat_members_json"] = members_json

            # 5. Hydrate system action service metadata
            for r in extracted_rows:
                if r.get("action_type") is not None and not r.get("service_metadata"):
                    r["service_metadata"] = json.dumps(
                        {"action_type": r["action_type"]},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )

            # Hydrate iOS link preview service metadata
            if "ZWAMEDIAITEM" in old_tables and extracted_rows:
                link_candidates = [
                    r
                    for r in extracted_rows
                    if r.get("media_type") == "link" or r.get("raw_msg_type") == 7
                ]
                if link_candidates:
                    pks = [r["msg_id"] for r in link_candidates]
                    chunk_size_lp = 900
                    lp_map = {}
                    lp_title_col = "mi.ZTITLE" if "ZTITLE" in mi_cols else "NULL"
                    lp_url_col = "mi.ZMEDIAURL" if "ZMEDIAURL" in mi_cols else "NULL"
                    lp_meta_col = "mi.ZMETADATA" if "ZMETADATA" in mi_cols else "NULL"
                    for i in range(0, len(pks), chunk_size_lp):
                        chunk = pks[i : i + chunk_size_lp]
                        ph = ",".join("?" * len(chunk))
                        lp_rows = conn.execute(
                            f"""
                            SELECT m.Z_PK, {lp_title_col}, {lp_url_col}, {lp_meta_col}
                            FROM old_db.ZWAMESSAGE m
                            JOIN old_db.ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
                            WHERE m.Z_PK IN ({ph})
                            """,
                            chunk,
                        ).fetchall()
                        for lpr in lp_rows:
                            lp_map[lpr[0]] = {
                                "title": lpr[1],
                                "desc": extract_ios_link_description(lpr[3]),
                                "url": lpr[2],
                                "preview_type": 0,
                            }
                    for r in link_candidates:
                        if r["msg_id"] in lp_map:
                            lp_data = lp_map[r["msg_id"]]
                            if not lp_data.get("url") and r.get("text_body"):
                                m_url = re.search(r"https?://[^\s]+", r["text_body"])
                                if m_url:
                                    lp_data["url"] = m_url.group(0)
                            existing_meta = {}
                            if r.get("service_metadata"):
                                try:
                                    existing_meta = json.loads(r["service_metadata"])
                                except Exception:
                                    pass
                            existing_meta["link_preview"] = lp_data
                            r["service_metadata"] = json.dumps(
                                existing_meta, ensure_ascii=False, separators=(",", ":")
                            )

            progress.finish({"deleted": deleted_count, "revoked": revoked_count})
        except Exception:
            progress.finish({"deleted": len(extracted_rows), "revoked": 0})
            raise

    logger.debug(f"iOS diff completed: {len(extracted_rows)} candidates for preservation.")
    return extracted_rows


def diff_and_extract_ios_calls(
    old_db_path: str, new_db_path: str, archive_db_path: str, logger: logging.Logger
) -> list[dict]:
    """
    Diff call logs between old_db and new_db for iOS using CallHistory.sqlite.
    Extracts call records present in old_db but absent from new_db.
    """
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    extracted_calls: list[dict] = []

    old_dir = os.path.dirname(old_db_path)
    new_dir = os.path.dirname(new_db_path)
    old_call_db = os.path.join(old_dir, "CallHistory.sqlite")
    new_call_db = os.path.join(new_dir, "CallHistory.sqlite")

    if not os.path.isfile(old_call_db):
        return []

    with contextlib.closing(sqlite3.connect(old_call_db)) as conn:
        conn.row_factory = sqlite3.Row
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "ZWACDCALLEVENT" not in tables:
            return []

        has_new_call_db = os.path.isfile(new_call_db)
        if has_new_call_db:
            conn.execute("ATTACH DATABASE ? AS new_call_db", (new_call_db,))

        has_arch = os.path.isfile(archive_db_path)
        if has_arch:
            conn.execute("ATTACH DATABASE ? AS arch", (archive_db_path,))

        cl_cols = {r[1] for r in conn.execute("PRAGMA table_info(ZWACDCALLEVENT)").fetchall()}
        has_call_id = "ZCALLIDSTRING" in cl_cols
        has_group_jid = "ZGROUPJIDSTRING" in cl_cols
        has_duration = "ZDURATION" in cl_cols
        has_outcome = "ZOUTCOME" in cl_cols
        has_date = "ZDATE" in cl_cols
        has_bytes_rec = "ZBYTESRECEIVED" in cl_cols
        has_bytes_sent = "ZBYTESSENT" in cl_cols
        has_aggregate = "ZAGGREGATECALLEVENT" in cl_cols and "ZWAAGGREGATECALLEVENT" in tables
        has_participants = "ZWACDCALLEVENTPARTICIPANT" in tables

        call_id_expr = (
            "COALESCE(NULLIF(cl.ZCALLIDSTRING, ''), 'call_' || cl.Z_PK)"
            if has_call_id
            else "('call_' || cl.Z_PK)"
        )
        group_jid_expr = "cl.ZGROUPJIDSTRING" if has_group_jid else "NULL"
        dur_expr = "COALESCE(cl.ZDURATION, 0)" if has_duration else "0"
        outcome_expr = "COALESCE(cl.ZOUTCOME, 0)" if has_outcome else "0"
        ts_expr = "CAST((cl.ZDATE + 978307200) * 1000 AS INTEGER)" if has_date else "0"

        if has_bytes_rec and has_bytes_sent:
            bytes_expr = "COALESCE(cl.ZBYTESRECEIVED, 0) + COALESCE(cl.ZBYTESSENT, 0)"
        elif has_bytes_rec:
            bytes_expr = "COALESCE(cl.ZBYTESRECEIVED, 0)"
        else:
            bytes_expr = "NULL"

        agg_join = (
            "LEFT JOIN ZWAAGGREGATECALLEVENT agg ON agg.Z_PK = cl.ZAGGREGATECALLEVENT"
            if has_aggregate
            else ""
        )
        video_expr = "COALESCE(agg.ZVIDEO, 0)" if has_aggregate else "0"
        from_me_expr = "CASE WHEN agg.ZINCOMING = 0 THEN 1 ELSE 0 END" if has_aggregate else "0"

        filter_clause = ""
        if has_new_call_db:
            new_tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM new_call_db.sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "ZWACDCALLEVENT" in new_tables:
                if has_call_id:
                    filter_clause = "WHERE NOT EXISTS (SELECT 1 FROM new_call_db.ZWACDCALLEVENT cur WHERE cur.ZCALLIDSTRING = cl.ZCALLIDSTRING)"
                else:
                    filter_clause = "WHERE NOT EXISTS (SELECT 1 FROM new_call_db.ZWACDCALLEVENT cur WHERE cur.ZDATE = cl.ZDATE AND cur.ZDURATION = cl.ZDURATION)"

        sql = f"""
            SELECT
                cl.Z_PK AS call_pk,
                {call_id_expr} AS call_id,
                {group_jid_expr} AS group_jid,
                {ts_expr} AS timestamp_ms,
                {dur_expr} AS duration_seconds,
                {video_expr} AS is_video,
                {outcome_expr} AS call_result,
                {from_me_expr} AS from_me,
                {bytes_expr} AS bytes_transferred
            FROM ZWACDCALLEVENT cl
            {agg_join}
            {filter_clause}
        """
        try:
            call_rows = conn.execute(sql).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning(f"iOS CallHistory diff query failed ({e}); skipping call preservation.")
            return []

        if not call_rows:
            return []

        participants_by_call: dict[int, list[dict]] = {}
        if has_participants:
            call_pks = [r["call_pk"] for r in call_rows]
            for i in range(0, len(call_pks), 900):
                chunk = call_pks[i : i + 900]
                ph = ",".join("?" * len(chunk))
                p_sql = f"""
                    SELECT p.Z1PARTICIPANTS, p.ZJIDSTRING, COALESCE(p.ZOUTCOME, 0)
                    FROM ZWACDCALLEVENTPARTICIPANT p
                    WHERE p.Z1PARTICIPANTS IN ({ph})
                """
                with contextlib.suppress(sqlite3.OperationalError):
                    for pr in conn.execute(p_sql, chunk).fetchall():
                        cid = pr[0]
                        p_jid = pr[1] or ""
                        p_phone = f"+{p_jid.split('@')[0]}" if "@" in p_jid else ""
                        participants_by_call.setdefault(cid, []).append(
                            {
                                "jid": p_jid,
                                "phone": p_phone,
                                "name": "",
                                "call_result": int(pr[2] or 0),
                            }
                        )

        for r in call_rows:
            c_dict = dict(r)
            c_dict["preserved_at"] = now_iso
            c_dict["source_platform"] = "ios"
            call_pk = c_dict.pop("call_pk", None)
            group_jid = c_dict.pop("group_jid", None)
            is_group = 1 if (group_jid and group_jid != "") else 0
            c_dict["is_group_call"] = is_group

            parts = participants_by_call.get(call_pk, [])
            if is_group:
                c_dict["chat_id"] = group_jid.split("@")[0] if group_jid else "call"
                c_dict["chat_jid"] = group_jid
                c_dict["caller_jid"] = parts[0]["jid"] if parts else None
                c_dict["caller_name"] = parts[0]["name"] if parts else None
            else:
                first_part = parts[0] if parts else None
                caller_jid = first_part["jid"] if first_part else None
                c_dict["caller_jid"] = caller_jid
                c_dict["caller_name"] = first_part["name"] if first_part else ""
                c_dict["chat_id"] = (
                    caller_jid.split("@")[0] if (caller_jid and "@" in caller_jid) else "call"
                )
                c_dict["chat_jid"] = caller_jid

            c_dict["participants"] = (
                json.dumps(parts, ensure_ascii=False, separators=(",", ":")) if parts else None
            )
            extracted_calls.append(c_dict)

    logger.debug(
        f"iOS call diff completed: {len(extracted_calls)} call candidate(s) for preservation."
    )
    return extracted_calls


def record_preserved_messages(
    pres_conn: sqlite3.Connection, rows: list[dict], platform: str, logger: logging.Logger
) -> PreservationResult:
    """
    Bulk insert extracted rows into preserved_messages, update preserved_chats,
    and log an entry in preservation_runs within an atomic transaction.
    Returns PreservationResult(messages, chats).
    """
    if not rows:
        return PreservationResult(messages=0, chats=0)

    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    insert_sql = """
        INSERT OR IGNORE INTO preserved_messages (
            chat_id, chat_jid, chat_type, stanza_id, msg_id, timestamp_ms,
            sender, sender_jid, sender_phone, from_me, text_body, original_text_body,
            media_type, media_name, archive_path, original_path, vcard_data,
            media_width, media_height, media_duration, media_size, is_view_once,
            latitude, longitude, location_name, location_address, location_url,
            live_location_share_duration, poll_data, quoted_text, quoted_sender,
            quoted_ts, quoted_stanza_id, quoted_parent_type, reactions, is_edited,
            edited_ts, is_starred, is_pinned, pin_expiry_ts, action_type,
            participant_name, service_metadata, preserved_at, preservation_reason,
            source_platform
        ) VALUES (
            :chat_id, :chat_jid, :chat_type, :stanza_id, :msg_id, :timestamp_ms,
            :sender, :sender_jid, :sender_phone, :from_me, :text_body, :original_text_body,
            :media_type, :media_name, :archive_path, :original_path, :vcard_data,
            :media_width, :media_height, :media_duration, :media_size, :is_view_once,
            :latitude, :longitude, :location_name, :location_address, :location_url,
            :live_location_share_duration, :poll_data, :quoted_text, :quoted_sender,
            :quoted_ts, :quoted_stanza_id, :quoted_parent_type, :reactions, :is_edited,
            :edited_ts, :is_starred, :is_pinned, :pin_expiry_ts, :action_type,
            :participant_name, :service_metadata, :preserved_at, :preservation_reason,
            :source_platform
        )
    """

    cursor = pres_conn.cursor()
    cursor.execute("BEGIN IMMEDIATE")
    try:
        new_rows = []
        seen_keys = set()
        for r in rows:
            key = (r["chat_id"], r["stanza_id"])
            if key in seen_keys:
                continue
            exists = pres_conn.execute(
                "SELECT text_body, original_text_body, reactions, poll_data FROM preserved_messages WHERE chat_id = ? AND stanza_id = ? LIMIT 1",
                key,
            ).fetchone()
            if not exists:
                normalized = {
                    "chat_id": str(r["chat_id"]),
                    "chat_jid": r.get("chat_jid"),
                    "chat_type": r.get("chat_type", "contact"),
                    "stanza_id": str(r["stanza_id"]),
                    "msg_id": int(r.get("msg_id", 0)),
                    "timestamp_ms": int(r.get("timestamp_ms", 0)),
                    "sender": r.get("sender") or "",
                    "sender_jid": r.get("sender_jid"),
                    "sender_phone": r.get("sender_phone"),
                    "from_me": int(r.get("from_me", 0)),
                    "text_body": r.get("text_body") if r.get("text_body") is not None else "",
                    "original_text_body": r.get("original_text_body"),
                    "media_type": r.get("media_type") or "text",
                    "media_name": r.get("media_name"),
                    "archive_path": r.get("archive_path"),
                    "original_path": r.get("original_path"),
                    "vcard_data": r.get("vcard_data"),
                    "media_width": r.get("media_width"),
                    "media_height": r.get("media_height"),
                    "media_duration": r.get("media_duration"),
                    "media_size": r.get("media_size"),
                    "is_view_once": int(r.get("is_view_once", 0)),
                    "latitude": r.get("latitude"),
                    "longitude": r.get("longitude"),
                    "location_name": r.get("location_name"),
                    "location_address": r.get("location_address"),
                    "location_url": r.get("location_url"),
                    "live_location_share_duration": r.get("live_location_share_duration"),
                    "poll_data": r.get("poll_data"),
                    "quoted_text": r.get("quoted_text") or "",
                    "quoted_sender": r.get("quoted_sender") or "",
                    "quoted_ts": int(r.get("quoted_ts", 0) or 0),
                    "quoted_stanza_id": r.get("quoted_stanza_id"),
                    "quoted_parent_type": r.get("quoted_parent_type"),
                    "reactions": r.get("reactions"),
                    "is_edited": int(r.get("is_edited", 0)),
                    "edited_ts": r.get("edited_ts"),
                    "is_starred": int(r.get("is_starred", 0)),
                    "is_pinned": int(r.get("is_pinned", 0)),
                    "pin_expiry_ts": r.get("pin_expiry_ts"),
                    "action_type": r.get("action_type"),
                    "participant_name": r.get("participant_name") or "",
                    "service_metadata": r.get("service_metadata"),
                    "preserved_at": r.get("preserved_at") or now_iso,
                    "preservation_reason": r.get("preservation_reason") or "deleted_on_phone",
                    "source_platform": r.get("source_platform") or platform,
                    "_chat_subject": r.get("_chat_subject") or r.get("chat_subject"),
                    "_chat_description": r.get("_chat_description") or r.get("chat_description"),
                    "_chat_created_ts": r.get("_chat_created_ts") or r.get("chat_created_ts"),
                    "_chat_creator_jid": r.get("_chat_creator_jid") or r.get("chat_creator_jid"),
                    "_chat_members_json": r.get("_chat_members_json") or r.get("chat_members_json"),
                }
                new_rows.append(normalized)
                seen_keys.add(key)
            else:
                prev_text, prev_orig, prev_rx, prev_poll = (
                    exists[0],
                    exists[1],
                    exists[2],
                    exists[3],
                )
                if r.get("is_edited") == 1 and prev_text != r.get("text_body") and not prev_orig:
                    pres_conn.execute(
                        """UPDATE preserved_messages
                           SET original_text_body = ?, text_body = ?, is_edited = 1, edited_ts = ?
                           WHERE chat_id = ? AND stanza_id = ?""",
                        (prev_text, r.get("text_body"), r.get("edited_ts"), key[0], key[1]),
                    )
                if r.get("reactions") and not prev_rx:
                    pres_conn.execute(
                        "UPDATE preserved_messages SET reactions = ? WHERE chat_id = ? AND stanza_id = ?",
                        (r["reactions"], key[0], key[1]),
                    )
                if r.get("poll_data") and not prev_poll:
                    pres_conn.execute(
                        "UPDATE preserved_messages SET poll_data = ? WHERE chat_id = ? AND stanza_id = ?",
                        (r["poll_data"], key[0], key[1]),
                    )

        if new_rows:
            insert_rows = [
                {k: v for k, v in row.items() if not k.startswith("_chat_")} for row in new_rows
            ]
            cursor.executemany(insert_sql, insert_rows)
            newly_inserted = len(new_rows)
            affected_chats = {(r["chat_id"], r["chat_type"]) for r in new_rows}
            for chat_id, chat_type in affected_chats:
                stat = pres_conn.execute(
                    """
                    SELECT MIN(timestamp_ms), MAX(timestamp_ms), COUNT(*)
                    FROM preserved_messages
                    WHERE chat_id = ? AND chat_type = ?
                """,
                    (chat_id, chat_type),
                ).fetchone()
                if stat and stat[2] is not None and stat[2] > 0:
                    oldest_ts, newest_ts, msg_count = stat[0], stat[1], stat[2]
                    chat_sample = next(
                        (
                            r
                            for r in new_rows
                            if r["chat_id"] == chat_id and r["chat_type"] == chat_type
                        ),
                        {},
                    )
                    cursor.execute(
                        """
                        INSERT INTO preserved_chats (
                            chat_id, chat_type, chat_jid, subject, description,
                            created_ts, creator_jid, oldest_ts, newest_ts, msg_count, members_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(chat_id, chat_type) DO UPDATE SET
                            chat_jid = COALESCE(excluded.chat_jid, preserved_chats.chat_jid),
                            subject = COALESCE(excluded.subject, preserved_chats.subject),
                            description = COALESCE(excluded.description, preserved_chats.description),
                            created_ts = COALESCE(excluded.created_ts, preserved_chats.created_ts),
                            creator_jid = COALESCE(excluded.creator_jid, preserved_chats.creator_jid),
                            oldest_ts = excluded.oldest_ts,
                            newest_ts = excluded.newest_ts,
                            msg_count = excluded.msg_count,
                            members_json = COALESCE(excluded.members_json, preserved_chats.members_json)
                    """,
                        (
                            chat_id,
                            chat_type,
                            chat_sample.get("chat_jid"),
                            chat_sample.get("_chat_subject"),
                            chat_sample.get("_chat_description"),
                            chat_sample.get("_chat_created_ts"),
                            chat_sample.get("_chat_creator_jid"),
                            oldest_ts,
                            newest_ts,
                            msg_count,
                            chat_sample.get("_chat_members_json"),
                        ),
                    )
        else:
            newly_inserted = 0
            affected_chats = set()

        cursor.execute(
            """
            INSERT INTO preservation_runs (
                run_at, source_platform, messages_preserved, chats_affected, calls_preserved, notes
            ) VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                now_iso,
                platform,
                newly_inserted,
                len(affected_chats),
                0,
                f"Evaluated {len(rows)} candidates.",
            ),
        )
        pres_conn.commit()
        logger.info(
            f"Preservation complete: {newly_inserted} new message(s) preserved across "
            f"{len(affected_chats)} chat(s)."
        )
        return PreservationResult(messages=newly_inserted, chats=len(affected_chats))
    except Exception as e:
        pres_conn.rollback()
        logger.error(f"Failed to record preserved messages ({e}); rolled back.")
        raise


def record_preserved_calls(
    pres_conn: sqlite3.Connection,
    calls: list[dict],
    platform: str,
    logger: logging.Logger,
) -> int:
    """Bulk insert preserved calls into preserved_calls within an atomic transaction."""
    if not calls:
        return 0

    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    insert_sql = """
        INSERT OR IGNORE INTO preserved_calls (
            call_id, chat_id, chat_jid, caller_jid, caller_name,
            from_me, timestamp_ms, duration_seconds, is_video,
            call_result, is_group_call, participants, bytes_transferred,
            preserved_at, source_platform
        ) VALUES (
            :call_id, :chat_id, :chat_jid, :caller_jid, :caller_name,
            :from_me, :timestamp_ms, :duration_seconds, :is_video,
            :call_result, :is_group_call, :participants, :bytes_transferred,
            :preserved_at, :source_platform
        )
    """
    cursor = pres_conn.cursor()
    cursor.execute("BEGIN IMMEDIATE")
    try:
        new_calls = []
        seen_call_ids = set()
        for c in calls:
            call_id = str(c["call_id"])
            if call_id in seen_call_ids:
                continue
            exists = pres_conn.execute(
                "SELECT 1 FROM preserved_calls WHERE call_id = ? LIMIT 1",
                (call_id,),
            ).fetchone()
            if not exists:
                new_calls.append(
                    {
                        "call_id": call_id,
                        "chat_id": c.get("chat_id"),
                        "chat_jid": c.get("chat_jid"),
                        "caller_jid": c.get("caller_jid"),
                        "caller_name": c.get("caller_name"),
                        "from_me": int(c.get("from_me", 0)),
                        "timestamp_ms": int(c.get("timestamp_ms", 0)),
                        "duration_seconds": int(c.get("duration_seconds", 0)),
                        "is_video": int(c.get("is_video", 0)),
                        "call_result": int(c.get("call_result", 0)),
                        "is_group_call": int(c.get("is_group_call", 0)),
                        "participants": c.get("participants"),
                        "bytes_transferred": c.get("bytes_transferred"),
                        "preserved_at": c.get("preserved_at") or now_iso,
                        "source_platform": c.get("source_platform") or platform,
                    }
                )
                seen_call_ids.add(call_id)

        if new_calls:
            cursor.executemany(insert_sql, new_calls)
            newly_inserted = len(new_calls)
        else:
            newly_inserted = 0

        cursor.execute(
            """
            INSERT INTO preservation_runs (
                run_at, source_platform, messages_preserved, chats_affected, calls_preserved, notes
            ) VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                now_iso,
                platform,
                0,
                0,
                newly_inserted,
                f"Evaluated {len(calls)} call candidate(s).",
            ),
        )
        pres_conn.commit()
        logger.debug(f"Preservation complete: {newly_inserted} new call(s) preserved.")
        return newly_inserted
    except Exception as e:
        pres_conn.rollback()
        logger.error(f"Failed to record preserved calls ({e}); rolled back.")
        raise


def preserve_deleted_messages(
    old_db_path: str,
    new_db_path: str,
    output_root: str,
    archive_db_path: str,
    platform: str,
    logger: logging.Logger,
) -> PreservationResult:
    """
    High-level orchestrator: verifies account match, diffs old and new databases,
    and commits any deleted/revoked messages to preserved_messages.db.
    Returns PreservationResult(messages, chats).
    """
    if not os.path.isfile(old_db_path):
        logger.debug(f"Previous database not found at {old_db_path}; skipping preservation.")
        return PreservationResult(messages=0, chats=0)

    if not os.path.isfile(new_db_path):
        logger.error(f"Incoming database not found at {new_db_path}; cannot run preservation.")
        return PreservationResult(messages=0, chats=0)

    if not verify_account_match(old_db_path, new_db_path, platform, logger):
        return PreservationResult(messages=0, chats=0)

    from . import archive_db

    with contextlib.closing(archive_db.open_archive_db(output_root)):
        pass

    logger.info("Checking for deleted or revoked messages against previous database snapshot...")
    extracted_calls: list[dict] = []
    if platform == "android":
        extracted = diff_and_extract_android(old_db_path, new_db_path, archive_db_path, logger)
        extracted_calls = diff_and_extract_android_calls(
            old_db_path, new_db_path, archive_db_path, logger
        )
    elif platform == "ios":
        extracted = diff_and_extract_ios(old_db_path, new_db_path, archive_db_path, logger)
        extracted_calls = diff_and_extract_ios_calls(
            old_db_path, new_db_path, archive_db_path, logger
        )
    else:
        logger.warning(f"Unsupported platform '{platform}' for preservation diff; skipping.")
        return PreservationResult(messages=0, chats=0)

    if not extracted:
        logger.info(
            "No deleted or revoked messages detected (0 messages preserved across 0 chats)."
        )
        if not extracted_calls:
            return PreservationResult(messages=0, chats=0)

    with contextlib.closing(open_preservation_db(output_root)) as pres_conn:
        res = (
            record_preserved_messages(pres_conn, extracted, platform, logger)
            if extracted
            else PreservationResult(messages=0, chats=0)
        )
        if extracted_calls:
            record_preserved_calls(pres_conn, extracted_calls, platform, logger)
        return res
