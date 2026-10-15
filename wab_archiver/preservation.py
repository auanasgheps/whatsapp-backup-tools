import contextlib
import logging
import os
import sqlite3
from datetime import UTC, datetime
from typing import NamedTuple

from shared.source_detection import WA_DATABASES_DIR_NAME

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


PRESERVED_SCHEMA = """
    CREATE TABLE IF NOT EXISTS preserved_messages (
        chat_id              TEXT NOT NULL,
        chat_type            TEXT NOT NULL,
        stanza_id            TEXT NOT NULL,
        msg_id               INTEGER NOT NULL,
        timestamp_ms         INTEGER NOT NULL,
        sender               TEXT NOT NULL,
        from_me              INTEGER NOT NULL,
        text_body            TEXT NOT NULL DEFAULT '',
        media_type           TEXT NOT NULL DEFAULT 'text',
        media_name           TEXT,
        archive_path         TEXT,
        quoted_text          TEXT,
        quoted_sender        TEXT,
        quoted_ts            INTEGER,
        reactions            TEXT,
        is_edited            INTEGER NOT NULL DEFAULT 0,
        edited_ts            INTEGER,
        action_type          INTEGER,
        participant_name     TEXT,
        preserved_at         TEXT NOT NULL,
        preservation_reason  TEXT NOT NULL,
        source_platform      TEXT NOT NULL,
        PRIMARY KEY (chat_id, stanza_id)
    );

    CREATE TABLE IF NOT EXISTS preserved_chats (
        chat_id              TEXT NOT NULL,
        chat_type            TEXT NOT NULL,
        newest_ts            INTEGER NOT NULL,
        msg_count            INTEGER NOT NULL,
        PRIMARY KEY (chat_id, chat_type)
    );

    CREATE TABLE IF NOT EXISTS preservation_runs (
        run_id               INTEGER PRIMARY KEY AUTOINCREMENT,
        run_at               TEXT NOT NULL,
        source_platform      TEXT NOT NULL,
        messages_preserved   INTEGER NOT NULL,
        chats_affected       INTEGER NOT NULL,
        notes                TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_pres_chat_ts
        ON preserved_messages(chat_id, timestamp_ms DESC);

    CREATE INDEX IF NOT EXISTS idx_pres_chat_type_ts
        ON preserved_messages(chat_type, chat_id, timestamp_ms DESC);

    CREATE INDEX IF NOT EXISTS idx_pres_stanza
        ON preserved_messages(stanza_id);
"""


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
    conn.executescript(PRESERVED_SCHEMA)

    # Automated schema migration check
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(preserved_messages)")}
    if "participant_name" not in existing_cols:
        conn.execute("ALTER TABLE preserved_messages ADD COLUMN participant_name TEXT")

    if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
        conn.execute("PRAGMA user_version = 1")
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
        conn.execute("ATTACH DATABASE ? AS old_db", (old_db_path,))
        conn.execute("ATTACH DATABASE ? AS arch", (archive_db_path,))

        # Inspect table schema in old_db and arch to adapt queries gracefully
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
        has_media = "message_media" in old_tables
        has_quoted = "message_quoted" in old_tables
        has_system = "message_system" in old_tables
        has_edit_info = "message_edit_info" in old_tables
        has_add_on = "message_add_on" in old_tables
        has_jid_map = "jid_map" in old_tables
        has_arch_contacts = "contacts" in arch_tables
        has_arch_copies = "archive_copies" in arch_tables

        mm_join = (
            "LEFT JOIN old_db.message_media mm ON mm.message_row_id = m._id" if has_media else ""
        )
        mq_join = (
            "LEFT JOIN old_db.message_quoted mq ON mq.message_row_id = m._id" if has_quoted else ""
        )
        ms_join = (
            "LEFT JOIN old_db.message_system ms ON ms.message_row_id = m._id" if has_system else ""
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
        con_s_arm = "NULLIF(con_s.display_name, '')," if has_arch_contacts else ""
        con_sq_arm = "NULLIF(con_sq.display_name, '')," if has_arch_contacts else ""

        file_path_col = "mm.file_path" if has_media else "NULL"
        media_name_col = "mm.media_name" if has_media else "NULL"
        ac_join = (
            f"LEFT JOIN arch.archive_copies ac ON ac.original_path = COALESCE({file_path_col}, 'vcard:' || m._id)"
            if has_arch_copies
            else ""
        )
        archive_path_expr = "ac.archive_path" if has_arch_copies else "NULL"
        quoted_text_col = "mq.text_data" if has_quoted else "NULL"
        quoted_ts_col = "mq.timestamp" if has_quoted else "0"
        quoted_from_me_col = "mq.from_me" if has_quoted else "0"
        edit_is_edited_col = (
            "CASE WHEN mei.message_row_id IS NOT NULL THEN 1 ELSE 0 END" if has_edit_info else "0"
        )
        edit_ts_col = "mei.edited_timestamp" if has_edit_info else "NULL"
        ms_action_col = "ms.action_type" if has_system else "NULL"

        # Check total messages in old vs new to detect device reset scenario
        old_count = conn.execute("SELECT COUNT(*) FROM old_db.message").fetchone()[0]
        new_count = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        is_device_reset = (old_count >= 100) and (new_count < old_count * 0.1)

        # 1. Identify missing messages (deleted on device)
        sql_deleted = f"""
            SELECT
                m._id AS msg_id,
                COALESCE(NULLIF(m.key_id, ''), 'msg_' || m._id) AS stanza_id,
                CASE
                    WHEN c.subject IS NOT NULL THEN
                        COALESCE(
                            (SELECT CAST(cur_c._id AS TEXT) FROM chat cur_c JOIN jid cur_j ON cur_j._id = cur_c.jid_row_id WHERE cur_j.user = j_chat.user),
                            CAST(m.chat_row_id AS TEXT)
                        )
                    ELSE COALESCE(j_chat.user, CAST(m.chat_row_id AS TEXT))
                END AS chat_id,
                CASE WHEN c.subject IS NOT NULL THEN 'group' ELSE 'contact' END AS chat_type,
                COALESCE(m.timestamp, 0) AS timestamp_ms,
                COALESCE(
                    {con_s_arm}
                    CASE WHEN COALESCE(j2.user, j.user) = '0' THEN 'WhatsApp' END,
                    CASE WHEN j2.user IS NOT NULL THEN '+' || j2.user END,
                    CASE WHEN j.user IS NOT NULL THEN '+' || j.user END,
                    ''
                ) AS sender,
                m.from_me AS from_me,
                COALESCE(m.text_data, '') AS text_body,
                CASE
                    WHEN m.message_type = 7 THEN 'service'
                    WHEN m.message_type IN (4, 14) THEN 'vcard'
                    WHEN m.message_type IS NULL OR m.message_type = 0 THEN
                        CASE WHEN m.text_data IS NOT NULL
                              AND (INSTR(LOWER(m.text_data), 'http://') > 0
                                   OR INSTR(LOWER(m.text_data), 'https://') > 0)
                             THEN 'link' ELSE 'text' END
                    ELSE
                        CASE
                            WHEN LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.vcf' THEN 'vcard'
                            WHEN LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%sticker%' THEN 'sticker'
                            WHEN LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.jpg'
                              OR LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.jpeg'
                              OR LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.png'
                              OR LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.webp' THEN 'image'
                            WHEN LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.mp4'
                              OR LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.mov'
                              OR LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.3gp' THEN 'video'
                            WHEN LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.opus'
                              OR LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.m4a'
                              OR LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.ogg'
                              OR LOWER(COALESCE({file_path_col}, {media_name_col}, '')) LIKE '%.mp3' THEN 'audio'
                            ELSE 'document'
                        END
                END AS media_type,
                COALESCE({media_name_col}, CASE WHEN m.message_type IN (4, 14) THEN m.text_data END, '') AS media_name,
                {archive_path_expr} AS archive_path,
                COALESCE({quoted_text_col}, '') AS quoted_text,
                CASE WHEN {quoted_from_me_col} = 1 THEN 'You'
                     ELSE COALESCE(
                         {con_sq_arm}
                         CASE WHEN jq2.user IS NOT NULL THEN '+' || jq2.user END,
                         CASE WHEN jq.user IS NOT NULL THEN '+' || jq.user END,
                         '')
                END AS quoted_sender,
                COALESCE({quoted_ts_col}, 0) AS quoted_ts,
                {edit_is_edited_col} AS is_edited,
                {edit_ts_col} AS edited_ts,
                {ms_action_col} AS action_type,
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
            {edit_join}
            {con_s_join}
            {con_sq_join}
            {ac_join}
            WHERE (
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
        """
        for r in conn.execute(sql_deleted):
            row_dict = dict(r)
            row_dict["preserved_at"] = now_iso
            row_dict["source_platform"] = "android"
            row_dict["reactions"] = None
            row_dict["participant_name"] = ""
            extracted_rows.append(row_dict)

        # 2. Identify revoked messages ("Delete for everyone")
        sql_revoked = f"""
            SELECT
                m._id AS msg_id,
                COALESCE(NULLIF(m.key_id, ''), 'msg_' || m._id) AS stanza_id,
                CASE
                    WHEN c.subject IS NOT NULL THEN
                        COALESCE(
                            (SELECT CAST(cur_c._id AS TEXT) FROM chat cur_c JOIN jid cur_j ON cur_j._id = cur_c.jid_row_id WHERE cur_j.user = j_chat.user),
                            CAST(m.chat_row_id AS TEXT)
                        )
                    ELSE COALESCE(j_chat.user, CAST(m.chat_row_id AS TEXT))
                END AS chat_id,
                CASE WHEN c.subject IS NOT NULL THEN 'group' ELSE 'contact' END AS chat_type,
                COALESCE(m.timestamp, 0) AS timestamp_ms,
                COALESCE(
                    {con_s_arm}
                    CASE WHEN COALESCE(j2.user, j.user) = '0' THEN 'WhatsApp' END,
                    CASE WHEN j2.user IS NOT NULL THEN '+' || j2.user END,
                    CASE WHEN j.user IS NOT NULL THEN '+' || j.user END,
                    ''
                ) AS sender,
                m.from_me AS from_me,
                COALESCE(m.text_data, '') AS text_body,
                'text' AS media_type,
                COALESCE({media_name_col}, '') AS media_name,
                {archive_path_expr} AS archive_path,
                COALESCE({quoted_text_col}, '') AS quoted_text,
                CASE WHEN {quoted_from_me_col} = 1 THEN 'You'
                     ELSE COALESCE(
                         {con_sq_arm}
                         CASE WHEN jq2.user IS NOT NULL THEN '+' || jq2.user END,
                         CASE WHEN jq.user IS NOT NULL THEN '+' || jq.user END,
                         '')
                END AS quoted_sender,
                COALESCE({quoted_ts_col}, 0) AS quoted_ts,
                {edit_is_edited_col} AS is_edited,
                {edit_ts_col} AS edited_ts,
                {ms_action_col} AS action_type,
                'revoked_by_sender' AS preservation_reason
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
            {edit_join}
            {con_s_join}
            {con_sq_join}
            {ac_join}
            WHERE m.message_type != 15 AND cur.message_type = 15
        """
        for r in conn.execute(sql_revoked):
            row_dict = dict(r)
            row_dict["preserved_at"] = now_iso
            row_dict["source_platform"] = "android"
            row_dict["reactions"] = None
            row_dict["participant_name"] = ""
            extracted_rows.append(row_dict)

        # Hydrate reactions from old_db if available
        if has_add_on and extracted_rows:
            msg_ids = [r["msg_id"] for r in extracted_rows]
            # Batch into chunks of 900 for SQLite parameter limits
            chunk_size = 900
            for i in range(0, len(msg_ids), chunk_size):
                chunk = msg_ids[i : i + chunk_size]
                ph = ",".join("?" * len(chunk))
                rx_sql = f"""
                    SELECT ao.parent_message_row_id AS msg_id,
                           GROUP_CONCAT(r.reaction) AS reactions
                    FROM old_db.message_add_on ao
                    JOIN old_db.message_add_on_reaction r ON r.message_add_on_row_id = ao._id
                    WHERE ao.parent_message_row_id IN ({ph})
                    GROUP BY ao.parent_message_row_id
                """
                rx_map = dict(conn.execute(rx_sql, chunk).fetchall())
                for row in extracted_rows:
                    if row["msg_id"] in rx_map:
                        row["reactions"] = rx_map[row["msg_id"]]

    logger.debug(f"Android diff completed: {len(extracted_rows)} candidates for preservation.")
    return extracted_rows


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
        conn.execute("ATTACH DATABASE ? AS old_db", (old_db_path,))
        conn.execute("ATTACH DATABASE ? AS arch", (archive_db_path,))

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
        has_arch_contacts = "contacts" in arch_tables
        has_arch_copies = "archive_copies" in arch_tables

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
        quoted_text_col = "COALESCE(qm.ZTEXT, '')" if has_parent else "''"
        quoted_sender_col = (
            "CASE WHEN qm.ZISFROMME = 1 THEN 'You' "
            f"ELSE COALESCE({con_sq_arm} NULLIF(qm.ZPUSHNAME, ''), '') END"
            if has_parent
            else "''"
        )
        quoted_ts_col = (
            "COALESCE(CAST((qm.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER), 0)"
            if has_parent
            else "0"
        )
        media_path_col = (
            "COALESCE('Message/' || mi.ZMEDIALOCALPATH, 'vcard:' || m.Z_PK)"
            if has_media
            else "('vcard:' || m.Z_PK)"
        )
        media_name_col = "COALESCE(mi.ZTITLE, '')" if has_media else "''"
        media_local_expr = "COALESCE(mi.ZMEDIALOCALPATH, mi.ZTITLE, '')" if has_media else "''"
        pp_arm = "NULLIF(pp_phone.ZPUSHNAME, '')," if has_profile else ""
        ac_join = (
            f"LEFT JOIN arch.archive_copies ac ON ac.original_path = {media_path_col}"
            if has_arch_copies
            else ""
        )
        ac_path_col = "ac.archive_path" if has_arch_copies else "NULL"

        old_count = conn.execute("SELECT COUNT(*) FROM old_db.ZWAMESSAGE").fetchone()[0]
        new_count = conn.execute("SELECT COUNT(*) FROM ZWAMESSAGE").fetchone()[0]
        is_device_reset = (old_count >= 100) and (new_count < old_count * 0.1)

        sql_deleted = f"""
            SELECT
                m.Z_PK AS msg_id,
                COALESCE(NULLIF(m.ZSTANZAID, ''), 'msg_' || m.Z_PK) AS stanza_id,
                COALESCE(
                    (SELECT CAST(cur_cs.Z_PK AS TEXT) FROM ZWACHATSESSION cur_cs WHERE cur_cs.ZCONTACTJID = cs.ZCONTACTJID),
                    CAST(m.ZCHATSESSION AS TEXT)
                ) AS chat_id,
                CASE WHEN cs.ZGROUPINFO IS NOT NULL THEN 'group' ELSE 'contact' END AS chat_type,
                CAST((m.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER) AS timestamp_ms,
                COALESCE(
                    {con_s_arm}
                    {pp_arm}
                    NULLIF(cs.ZPARTNERNAME, ''),
                    CASE WHEN m.ZFROMJID NOT LIKE '%@g.us' THEN NULLIF(m.ZPUSHNAME, '') END,
                    ''
                ) AS sender,
                m.ZISFROMME AS from_me,
                COALESCE(m.ZTEXT, '') AS text_body,
                CASE
                    WHEN m.ZMESSAGETYPE = 6 THEN 'service'
                    WHEN m.ZMESSAGETYPE = 4 THEN 'vcard'
                    WHEN m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0 THEN
                        CASE WHEN m.ZTEXT IS NOT NULL
                              AND (INSTR(LOWER(m.ZTEXT), 'http://') > 0
                                   OR INSTR(LOWER(m.ZTEXT), 'https://') > 0)
                             THEN 'link' ELSE 'text' END
                    ELSE
                        CASE
                            WHEN LOWER({media_local_expr}) LIKE '%.vcf' THEN 'vcard'
                            WHEN LOWER({media_local_expr}) LIKE '%sticker%' THEN 'sticker'
                            WHEN LOWER({media_local_expr}) LIKE '%.jpg'
                              OR LOWER({media_local_expr}) LIKE '%.jpeg'
                              OR LOWER({media_local_expr}) LIKE '%.png'
                              OR LOWER({media_local_expr}) LIKE '%.webp' THEN 'image'
                            WHEN LOWER({media_local_expr}) LIKE '%.mp4'
                              OR LOWER({media_local_expr}) LIKE '%.mov' THEN 'video'
                            WHEN LOWER({media_local_expr}) LIKE '%.opus'
                              OR LOWER({media_local_expr}) LIKE '%.m4a'
                              OR LOWER({media_local_expr}) LIKE '%.mp3' THEN 'audio'
                            ELSE 'document'
                        END
                END AS media_type,
                {media_name_col} AS media_name,
                {ac_path_col} AS archive_path,
                {quoted_text_col} AS quoted_text,
                {quoted_sender_col} AS quoted_sender,
                {quoted_ts_col} AS quoted_ts,
                0 AS is_edited,
                NULL AS edited_ts,
                NULL AS action_type,
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
            {pp_join}
            {qm_join}
            {con_s_join}
            {con_sq_join}
            {ac_join}
            WHERE (
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
        """
        for r in conn.execute(sql_deleted):
            row_dict = dict(r)
            row_dict["preserved_at"] = now_iso
            row_dict["source_platform"] = "ios"
            row_dict["reactions"] = None
            row_dict["participant_name"] = ""
            extracted_rows.append(row_dict)

        sql_revoked = f"""
            SELECT
                m.Z_PK AS msg_id,
                COALESCE(NULLIF(m.ZSTANZAID, ''), 'msg_' || m.Z_PK) AS stanza_id,
                COALESCE(
                    (SELECT CAST(cur_cs.Z_PK AS TEXT) FROM ZWACHATSESSION cur_cs WHERE cur_cs.ZCONTACTJID = cs.ZCONTACTJID),
                    CAST(m.ZCHATSESSION AS TEXT)
                ) AS chat_id,
                CASE WHEN cs.ZGROUPINFO IS NOT NULL THEN 'group' ELSE 'contact' END AS chat_type,
                CAST((m.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER) AS timestamp_ms,
                COALESCE(
                    {con_s_arm}
                    {pp_arm}
                    NULLIF(cs.ZPARTNERNAME, ''),
                    CASE WHEN m.ZFROMJID NOT LIKE '%@g.us' THEN NULLIF(m.ZPUSHNAME, '') END,
                    ''
                ) AS sender,
                m.ZISFROMME AS from_me,
                COALESCE(m.ZTEXT, '') AS text_body,
                'text' AS media_type,
                {media_name_col} AS media_name,
                {ac_path_col} AS archive_path,
                {quoted_text_col} AS quoted_text,
                {quoted_sender_col} AS quoted_sender,
                {quoted_ts_col} AS quoted_ts,
                0 AS is_edited,
                NULL AS edited_ts,
                NULL AS action_type,
                'revoked_by_sender' AS preservation_reason
            FROM old_db.ZWAMESSAGE m
            JOIN ZWAMESSAGE cur ON cur.ZSTANZAID = m.ZSTANZAID
            {mi_join}
            LEFT JOIN old_db.ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
            {pp_join}
            {qm_join}
            {con_s_join}
            {con_sq_join}
            {ac_join}
            WHERE m.ZMESSAGETYPE != 14 AND cur.ZMESSAGETYPE = 14
        """
        for r in conn.execute(sql_revoked):
            row_dict = dict(r)
            row_dict["preserved_at"] = now_iso
            row_dict["source_platform"] = "ios"
            row_dict["reactions"] = None
            row_dict["participant_name"] = ""
            extracted_rows.append(row_dict)

    logger.debug(f"iOS diff completed: {len(extracted_rows)} candidates for preservation.")
    return extracted_rows


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
            chat_id, chat_type, stanza_id, msg_id, timestamp_ms,
            sender, from_me, text_body, media_type, media_name,
            archive_path, quoted_text, quoted_sender, quoted_ts,
            reactions, is_edited, edited_ts, action_type,
            participant_name, preserved_at, preservation_reason, source_platform
        ) VALUES (
            :chat_id, :chat_type, :stanza_id, :msg_id, :timestamp_ms,
            :sender, :from_me, :text_body, :media_type, :media_name,
            :archive_path, :quoted_text, :quoted_sender, :quoted_ts,
            :reactions, :is_edited, :edited_ts, :action_type,
            :participant_name, :preserved_at, :preservation_reason, :source_platform
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
                "SELECT 1 FROM preserved_messages WHERE chat_id = ? AND stanza_id = ? LIMIT 1",
                key,
            ).fetchone()
            if not exists:
                new_rows.append(r)
                seen_keys.add(key)

        if new_rows:
            cursor.executemany(insert_sql, new_rows)
            newly_inserted = len(new_rows)
            affected_chats = {(r["chat_id"], r["chat_type"]) for r in new_rows}
            for chat_id, chat_type in affected_chats:
                stat = pres_conn.execute(
                    """
                    SELECT MAX(timestamp_ms), COUNT(*)
                    FROM preserved_messages
                    WHERE chat_id = ? AND chat_type = ?
                """,
                    (chat_id, chat_type),
                ).fetchone()
                if stat and stat[0] is not None:
                    cursor.execute(
                        """
                        INSERT INTO preserved_chats (chat_id, chat_type, newest_ts, msg_count)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(chat_id, chat_type) DO UPDATE SET
                            newest_ts = excluded.newest_ts,
                            msg_count = excluded.msg_count
                    """,
                        (chat_id, chat_type, stat[0], stat[1]),
                    )
        else:
            newly_inserted = 0
            affected_chats = set()

        cursor.execute(
            """
            INSERT INTO preservation_runs (
                run_at, source_platform, messages_preserved, chats_affected, notes
            ) VALUES (?, ?, ?, ?, ?)
        """,
            (
                now_iso,
                platform,
                newly_inserted,
                len(affected_chats),
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
    if platform == "android":
        extracted = diff_and_extract_android(old_db_path, new_db_path, archive_db_path, logger)
    elif platform == "ios":
        extracted = diff_and_extract_ios(old_db_path, new_db_path, archive_db_path, logger)
    else:
        logger.warning(f"Unsupported platform '{platform}' for preservation diff; skipping.")
        return PreservationResult(messages=0, chats=0)

    if not extracted:
        logger.info(
            "No deleted or revoked messages detected (0 messages preserved across 0 chats)."
        )
        return PreservationResult(messages=0, chats=0)

    with contextlib.closing(open_preservation_db(output_root)) as pres_conn:
        return record_preserved_messages(pres_conn, extracted, platform, logger)
