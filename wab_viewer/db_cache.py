"""Cache (.wa_viewer.db), archive (.wa_media_archiver.db), and source DB helpers."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import sys
import time
from pathlib import Path


def get_cache_db_path(output_root: Path) -> Path:
    return output_root / ".wa_viewer.db"


def get_archive_db_path(output_root: Path) -> Path:
    target = output_root / ".wa_media_archiver.db"
    if not target.exists() and (output_root / ".wa_archiver.db").exists():
        return output_root / ".wa_archiver.db"
    return target


def _ensure_writable(path: Path) -> None:
    """Clear the read-only file attribute if present so SQLite can write to it."""
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if not p.exists():
            continue
        try:
            if sys.platform == "win32":
                import ctypes

                attrs = ctypes.windll.kernel32.GetFileAttributesW(str(p))
                if attrs != -1 and (attrs & 1):  # FILE_ATTRIBUTE_READONLY
                    ctypes.windll.kernel32.SetFileAttributesW(str(p), attrs & ~1)
            mode = p.stat().st_mode
            if not (mode & stat.S_IWRITE):
                p.chmod(mode | stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass


CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS message_index (
    rowid        INTEGER PRIMARY KEY,
    chat_id      TEXT NOT NULL,
    chat_type    TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_midx_chat_ts ON message_index(chat_id, timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_midx_chat_type_ts ON message_index(chat_id, chat_type, timestamp_ms);

CREATE VIRTUAL TABLE IF NOT EXISTS message_index_fts USING fts5(
    text_body,
    content=''
);

CREATE TABLE IF NOT EXISTS sync_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS indexed_chats (
    chat_id   TEXT NOT NULL,
    chat_type TEXT NOT NULL,
    PRIMARY KEY (chat_id, chat_type)
);

CREATE TABLE IF NOT EXISTS user_preferences (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


VALID_PREF_VALUES = {
    "theme": {"dark", "light"},
    "date_format": {"DD/MM/YYYY", "MM/DD/YYYY", "YYYY/MM/DD"},
    "font_size": {"small", "medium", "large"},
}


GALLERY_PAGE_SIZE = 100


def _ensure_wa_indexes(wa_db_path: Path) -> None:
    try:
        conn = sqlite3.connect(str(wa_db_path))
    except sqlite3.OperationalError as e:
        if "readonly" in str(e).lower():
            print(f"[wab_viewer] Notice: Source DB is read-only; skipping index optimization: {e}")
            return
        raise

    try:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError as e:
            if "readonly" in str(e).lower():
                print(
                    f"[wab_viewer] Notice: Source DB is read-only; skipping index optimization: {e}"
                )
                return
            raise

        existing = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        if "idx_message_chat_ts" not in existing:
            has_android = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message'"
            ).fetchone()
            if has_android:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_message_chat_ts "
                    "ON message(chat_row_id, timestamp)"
                )
        if "idx_poll_option_msg" not in existing:
            has_poll_opt = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_poll_option'"
            ).fetchone()
            if has_poll_opt:
                cols = {
                    r[1] for r in conn.execute("PRAGMA table_info(message_poll_option)").fetchall()
                }
                if "message_row_id" in cols:
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_poll_option_msg "
                        "ON message_poll_option(message_row_id)"
                    )
        if "idx_message_add_on_parent" not in existing:
            has_add_on = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_add_on'"
            ).fetchone()
            if has_add_on:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(message_add_on)").fetchall()}
                if "parent_message_row_id" in cols and "message_add_on_type" in cols:
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_message_add_on_parent "
                        "ON message_add_on(parent_message_row_id, message_add_on_type)"
                    )
        if "idx_zwamessage_chat_ts" not in existing:
            has_ios = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ZWAMESSAGE'"
            ).fetchone()
            if has_ios:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_zwamessage_chat_ts "
                    "ON ZWAMESSAGE(ZCHATSESSION, ZMESSAGEDATE)"
                )
        if "idx_zwamessage_pin_lookup" not in existing or "idx_zwamessage_stanza" not in existing:
            has_ios = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ZWAMESSAGE'"
            ).fetchone()
            if has_ios:
                zwa_cols = {r[1] for r in conn.execute("PRAGMA table_info(ZWAMESSAGE)").fetchall()}
                if (
                    "idx_zwamessage_pin_lookup" not in existing
                    and {"ZCHATSESSION", "ZMESSAGETYPE", "ZPARENTMESSAGE"} <= zwa_cols
                ):
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_zwamessage_pin_lookup "
                        "ON ZWAMESSAGE(ZCHATSESSION, ZPARENTMESSAGE) "
                        "WHERE ZMESSAGETYPE = 66"
                    )
                if "idx_zwamessage_stanza" not in existing and "ZSTANZAID" in zwa_cols:
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_zwamessage_stanza ON ZWAMESSAGE(ZSTANZAID)"
                    )
        if "idx_message_key_id" not in existing:
            has_android = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message'"
            ).fetchone()
            if has_android:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(message)").fetchall()}
                if "key_id" in cols:
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_message_key_id ON message(key_id)")
        if "idx_pres_cur_key_id" in existing:
            conn.execute("DROP INDEX IF EXISTS idx_pres_cur_key_id")
        if "idx_message_quoted_key_id" not in existing:
            has_mq = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_quoted'"
            ).fetchone()
            if has_mq:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(message_quoted)").fetchall()}
                if "key_id" in cols:
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_message_quoted_key_id "
                        "ON message_quoted(key_id)"
                    )
        if "idx_message_quoted_media_row_id" not in existing:
            has_mqm = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_quoted_media'"
            ).fetchone()
            if has_mqm:
                cols = {
                    r[1] for r in conn.execute("PRAGMA table_info(message_quoted_media)").fetchall()
                }
                if "message_row_id" in cols:
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_message_quoted_media_row_id "
                        "ON message_quoted_media(message_row_id)"
                    )
        if "idx_message_quoted_text_row_id" not in existing:
            has_mqt = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_quoted_text'"
            ).fetchone()
            if has_mqt:
                cols = {
                    r[1] for r in conn.execute("PRAGMA table_info(message_quoted_text)").fetchall()
                }
                if "message_row_id" in cols:
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_message_quoted_text_row_id "
                        "ON message_quoted_text(message_row_id)"
                    )

        # Database self-healing for Android: sanitize empty phantom messages and re-hide empty shadow chats
        has_android_msg = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message'"
        ).fetchone()
        has_android_chat = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat'"
        ).fetchone()
        if has_android_msg and has_android_chat:
            try:
                has_mm = bool(
                    conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_media'"
                    ).fetchone()
                )
                has_mv = bool(
                    conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_vcard'"
                    ).fetchone()
                )
                del_clauses = ["message_type = 0", "(text_data IS NULL OR text_data = '')"]
                if has_mm:
                    del_clauses.append("_id NOT IN (SELECT message_row_id FROM message_media)")
                if has_mv:
                    del_clauses.append("_id NOT IN (SELECT message_row_id FROM message_vcard)")
                conn.execute(f"DELETE FROM message WHERE {' AND '.join(del_clauses)}")
                chat_cols = {r[1] for r in conn.execute("PRAGMA table_info(chat)").fetchall()}
                if "hidden" in chat_cols:
                    conn.execute("""
                        UPDATE chat
                        SET hidden = 1
                        WHERE (hidden = 0 OR hidden IS NULL)
                          AND (subject IS NULL OR subject = '')
                          AND jid_row_id NOT IN (
                              SELECT _id FROM jid WHERE server = 'g.us' OR raw_string LIKE '%@g.us'
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM message m
                              WHERE m.chat_row_id = chat._id
                                AND (
                                    (m.text_data IS NOT NULL AND m.text_data != '' AND (m.message_type IS NULL OR m.message_type = 0 OR m.message_type IN (5, 16)))
                                    OR m.message_type IN (1, 2, 3, 4, 9, 13, 14, 15, 16, 20, 28, 42, 43, 64, 66, 82)
                                )
                          )
                    """)
                if "display_message_row_id" in chat_cols:
                    conn.execute("""
                        UPDATE chat
                        SET display_message_row_id = NULL
                        WHERE display_message_row_id IS NOT NULL
                          AND display_message_row_id NOT IN (SELECT _id FROM message)
                    """)
            except sqlite3.OperationalError:
                pass

        conn.commit()
    except sqlite3.OperationalError as e:
        if "readonly" in str(e).lower():
            print(f"[wab_viewer] Notice: Source DB is read-only; skipping index optimization: {e}")
            return
        raise
    finally:
        conn.close()


def _open_cache_db(output_root: Path) -> sqlite3.Connection:
    cache_path = get_cache_db_path(output_root)
    _ensure_writable(cache_path)
    try:
        conn = sqlite3.connect(str(cache_path), check_same_thread=False)
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA cache_size = -8000")
        conn.executescript(CACHE_SCHEMA)
        conn.execute("SELECT 1 FROM message_index_fts LIMIT 0")
        if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
            conn.execute("PRAGMA user_version = 1")
        conn.row_factory = sqlite3.Row
        conn.execute("INSERT OR IGNORE INTO user_preferences (key, value) VALUES ('theme', 'dark')")
        conn.execute(
            "INSERT OR IGNORE INTO user_preferences (key, value) VALUES ('date_format', 'DD/MM/YYYY')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO user_preferences (key, value) VALUES ('font_size', 'medium')"
        )
        conn.commit()
    except sqlite3.OperationalError as e:
        if "readonly" in str(e).lower():
            raise sqlite3.OperationalError(
                f"Cannot open cache database at '{cache_path}' for writing ({e}). "
                "Ensure the archive folder and its database files are not marked read-only."
            ) from e
        if "contentless_delete" in str(e).lower():
            try:
                conn.close()
            except Exception:
                pass
            for p in (
                cache_path,
                cache_path.with_name(cache_path.name + "-wal"),
                cache_path.with_name(cache_path.name + "-shm"),
            ):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
            return _open_cache_db(output_root)
        raise
    return conn


def _run_db_maintenance(
    cache_conn: sqlite3.Connection, archive_conn: sqlite3.Connection | None
) -> None:
    """Run lightweight non-blocking maintenance on cache and archive databases."""
    try:
        cache_conn.execute("PRAGMA incremental_vacuum(500)")
        cache_conn.execute("PRAGMA optimize")
        cache_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception as e:
        print(f"[wab_viewer] Notice: Cache DB maintenance: {e}")

    if archive_conn is not None:
        try:
            archive_conn.execute("PRAGMA incremental_vacuum(500)")
            archive_conn.execute("PRAGMA optimize")
            archive_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass


def _detect_source_db(output_root: Path):
    wa_db_dir = output_root / "Whatsapp Databases"
    for base in (wa_db_dir, output_root):
        msgstore = base / "msgstore.db"
        chat_storage = base / "ChatStorage.sqlite"
        if msgstore.exists():
            return ("android", str(msgstore))
        if chat_storage.exists():
            return ("ios", str(chat_storage))
    return (None, None)


def _source_changed(cache_conn: sqlite3.Connection, source_path: str) -> bool:
    try:
        st = os.stat(source_path)
        current_mtime = str(st.st_mtime)
        current_size = str(st.st_size)
    except OSError:
        return True

    rows = {
        r["key"]: r["value"]
        for r in cache_conn.execute(
            "SELECT key, value FROM sync_meta WHERE key IN ('source_mtime', 'source_size')"
        ).fetchall()
    }
    return rows.get("source_mtime") != current_mtime or rows.get("source_size") != current_size


def _save_source_stamp(cache_conn: sqlite3.Connection, source_path: str):
    st = os.stat(source_path)
    cache_conn.execute(
        "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('source_mtime', ?)",
        (str(st.st_mtime),),
    )
    cache_conn.execute(
        "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('source_size', ?)",
        (str(st.st_size),),
    )
    cache_conn.execute(
        "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('last_scanned_at', ?)",
        (str(int(time.time())),),
    )
    cache_conn.commit()


def _ensure_jid_map_resolved(
    conn: sqlite3.Connection,
    archive_conn: sqlite3.Connection | None,
) -> None:
    """Populate temporary lookup tables for resolved JIDs, LIDs, and contacts."""
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _jid_map_resolved (lid_row_id INTEGER PRIMARY KEY, jid_row_id INTEGER)"
    )
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _lid_map_resolved (jid_row_id INTEGER PRIMARY KEY, lid_row_id INTEGER)"
    )
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _contacts_resolved (clean_number TEXT PRIMARY KEY, display_name TEXT, folder TEXT)"
    )

    # 1. Populate _contacts_resolved from attached arch.contacts or archive_conn
    has_arch_contacts = False
    try:
        conn.execute("SELECT 1 FROM arch.contacts LIMIT 1")
        has_arch_contacts = True
    except sqlite3.OperationalError:
        pass

    if has_arch_contacts:
        contacts_query_conn = conn
        contacts_sql = "SELECT number, folder, display_name FROM arch.contacts"
    elif archive_conn is not None:
        contacts_query_conn = archive_conn
        contacts_sql = "SELECT number, folder, display_name FROM contacts"
    else:
        contacts_query_conn = None
        contacts_sql = ""

    contact_rows = []
    if contacts_query_conn is not None:
        try:
            contact_rows = contacts_query_conn.execute(contacts_sql).fetchall()
        except sqlite3.OperationalError:
            contact_rows = []

    contacts_to_insert = []
    if contact_rows:
        for r in contact_rows:
            raw_num = str(r[0] or "").strip()
            folder = str(r[1] or "")
            disp = str(r[2] or "").strip()
            if not disp and folder:
                disp = re.sub(r"\s*\(\+?\d+\)$", "", folder).strip()
            if not raw_num:
                continue
            digits = re.sub(r"\D", "", raw_num)
            if digits:
                contacts_to_insert.append((digits, disp, folder))
                clean_stripped = raw_num.lstrip("+").split(":")[0]
                if clean_stripped != digits:
                    contacts_to_insert.append((clean_stripped, disp, folder))
                if digits.startswith("00"):
                    contacts_to_insert.append((digits[2:], disp, folder))
                elif digits.startswith("0") and len(digits) > 8:
                    contacts_to_insert.append((digits[1:], disp, folder))

        try:
            # Fast in-memory suffix bridging: match international E.164 JIDs to national contact numbers and vice-versa
            jid_table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jid'"
            ).fetchone()
            if jid_table_exists:
                jid_cols = {
                    r["name"] if isinstance(r, sqlite3.Row) else r[1]
                    for r in conn.execute("PRAGMA table_info(jid)").fetchall()
                }
                phone_filter = (
                    "WHERE (server = 's.whatsapp.net' OR server IS NULL)"
                    if "server" in jid_cols
                    else "WHERE 1=1"
                )
                jid_users = {
                    str(row[0]).split(":")[0].split(".")[0]
                    for row in conn.execute(
                        f"SELECT user FROM jid {phone_filter} AND user IS NOT NULL AND user != ''"
                    ).fetchall()
                }
                # Index JID users by suffixes (lengths 8..15) for O(1) matching
                jid_by_suffix = {}
                for ju in jid_users:
                    if ju.isdigit() and len(ju) >= 8:
                        for length in range(8, len(ju) + 1):
                            suf = ju[-length:]
                            jid_by_suffix.setdefault(suf, []).append(ju)

                bridged = []
                for digits, d_name, fld in list(contacts_to_insert):
                    if len(digits) >= 8:
                        if digits in jid_by_suffix:
                            for ju in jid_by_suffix[digits]:
                                if ju != digits:
                                    bridged.append((ju, d_name, fld))
                        if digits.isdigit():
                            for length in range(8, len(digits)):
                                suf = digits[-length:]
                                if suf in jid_users:
                                    bridged.append((suf, d_name, fld))
                if bridged:
                    contacts_to_insert.extend(bridged)
        except sqlite3.OperationalError:
            pass

        if contacts_to_insert:
            conn.executemany(
                "INSERT OR REPLACE INTO _contacts_resolved (clean_number, display_name, folder) VALUES (?, ?, ?)",
                contacts_to_insert,
            )

    # 2. Inspect available schema tables in conn
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    except sqlite3.OperationalError:
        return

    jid_cols = (
        {
            r["name"] if isinstance(r, sqlite3.Row) else r[1]
            for r in conn.execute("PRAGMA table_info(jid)").fetchall()
        }
        if "jid" in tables
        else set()
    )
    has_jid_server = "server" in jid_cols
    has_jid_raw_string = "raw_string" in jid_cols
    has_jid_map = "jid_map" in tables
    has_chat = "chat" in tables
    has_ldn = "lid_display_name" in tables
    has_message = "message" in tables

    # 3. Populate from jid_map
    if has_jid_map:
        try:
            if has_jid_server:
                conn.execute("""
                    INSERT OR IGNORE INTO _jid_map_resolved (lid_row_id, jid_row_id)
                    SELECT j_lid._id, j_phone._id
                    FROM jid_map jm
                    JOIN jid j1 ON j1._id = jm.lid_row_id
                    JOIN jid j2 ON j2._id = jm.jid_row_id
                    JOIN jid j_lid ON j_lid._id IN (j1._id, j2._id) AND j_lid.server = 'lid'
                    JOIN jid j_phone ON j_phone._id IN (j1._id, j2._id) AND j_phone.server = 's.whatsapp.net'
                """)
            elif has_jid_raw_string:
                conn.execute("""
                    INSERT OR IGNORE INTO _jid_map_resolved (lid_row_id, jid_row_id)
                    SELECT j_lid._id, j_phone._id
                    FROM jid_map jm
                    JOIN jid j1 ON j1._id = jm.lid_row_id
                    JOIN jid j2 ON j2._id = jm.jid_row_id
                    JOIN jid j_lid ON j_lid._id IN (j1._id, j2._id) AND j_lid.raw_string LIKE '%@lid'
                    JOIN jid j_phone ON j_phone._id IN (j1._id, j2._id) AND j_phone.raw_string LIKE '%@s.whatsapp.net'
                """)
            else:
                conn.execute("""
                    INSERT OR IGNORE INTO _jid_map_resolved (lid_row_id, jid_row_id)
                    SELECT lid_row_id, MIN(jid_row_id) AS jid_row_id
                    FROM jid_map GROUP BY lid_row_id
                """)
        except sqlite3.OperationalError:
            pass

    # 4. Populate from chat.account_jid_row_id
    if has_chat:
        try:
            chat_cols = {
                r["name"] if isinstance(r, sqlite3.Row) else r[1]
                for r in conn.execute("PRAGMA table_info(chat)").fetchall()
            }
            if "account_jid_row_id" in chat_cols:
                if has_jid_server:
                    conn.execute("""
                        INSERT OR IGNORE INTO _jid_map_resolved (lid_row_id, jid_row_id)
                        SELECT j_lid._id, j_phone._id
                        FROM chat c
                        JOIN jid j1 ON j1._id = c.jid_row_id
                        JOIN jid j2 ON j2._id = c.account_jid_row_id
                        JOIN jid j_lid ON j_lid._id IN (j1._id, j2._id) AND j_lid.server = 'lid'
                        JOIN jid j_phone ON j_phone._id IN (j1._id, j2._id) AND j_phone.server = 's.whatsapp.net'
                        WHERE c.account_jid_row_id IS NOT NULL AND c.account_jid_row_id != 0
                    """)
                elif has_jid_raw_string:
                    conn.execute("""
                        INSERT OR IGNORE INTO _jid_map_resolved (lid_row_id, jid_row_id)
                        SELECT j_lid._id, j_phone._id
                        FROM chat c
                        JOIN jid j1 ON j1._id = c.jid_row_id
                        JOIN jid j2 ON j2._id = c.account_jid_row_id
                        JOIN jid j_lid ON j_lid._id IN (j1._id, j2._id) AND j_lid.raw_string LIKE '%@lid'
                        JOIN jid j_phone ON j_phone._id IN (j1._id, j2._id) AND j_phone.raw_string LIKE '%@s.whatsapp.net'
                        WHERE c.account_jid_row_id IS NOT NULL AND c.account_jid_row_id != 0
                    """)
                else:
                    conn.execute("""
                        INSERT OR IGNORE INTO _jid_map_resolved (lid_row_id, jid_row_id)
                        SELECT jid_row_id, account_jid_row_id
                        FROM chat
                        WHERE account_jid_row_id IS NOT NULL AND account_jid_row_id != 0
                    """)
        except sqlite3.OperationalError:
            pass

    # 5. Populate from lid_display_name matching phone numbers or contacts
    if has_ldn and "jid" in tables:
        try:
            phone_filter = (
                "WHERE server = 's.whatsapp.net'"
                if has_jid_server
                else ("WHERE raw_string LIKE '%@s.whatsapp.net'" if has_jid_raw_string else "")
            )
            phone_jids = {
                str(r[0]).split(":")[0]: r[1]
                for r in conn.execute(
                    f"SELECT user, _id FROM jid {phone_filter} WHERE user IS NOT NULL AND user != ''"
                ).fetchall()
            }
            ldn_rows = conn.execute(
                "SELECT lid_row_id, display_name FROM lid_display_name WHERE display_name IS NOT NULL AND display_name != ''"
            ).fetchall()
            ldn_mappings = []
            for lid_id, d_name in ldn_rows:
                d_digits = re.sub(r"\D", "", d_name)
                if not d_digits:
                    continue
                if d_digits in phone_jids:
                    ldn_mappings.append((lid_id, phone_jids[d_digits]))
                elif len(d_digits) >= 8:
                    for pu, p_id in phone_jids.items():
                        if pu == d_digits or (
                            len(pu) >= 8 and (pu.endswith(d_digits) or d_digits.endswith(pu))
                        ):
                            ldn_mappings.append((lid_id, p_id))
                            break
            if ldn_mappings:
                conn.executemany(
                    "INSERT OR IGNORE INTO _jid_map_resolved (lid_row_id, jid_row_id) VALUES (?, ?)",
                    ldn_mappings,
                )
        except sqlite3.OperationalError:
            pass

    # 6. Populate from 1-to-1 message history
    if has_message and has_chat and "jid" in tables:
        try:
            if has_jid_server:
                conn.execute("""
                    INSERT OR IGNORE INTO _jid_map_resolved (lid_row_id, jid_row_id)
                    SELECT j_lid._id, j_phone._id
                    FROM message m
                    JOIN chat c ON c._id = m.chat_row_id AND c.subject IS NULL
                    JOIN jid j_chat ON j_chat._id = c.jid_row_id
                    JOIN jid j_sender ON j_sender._id = m.sender_jid_row_id
                    JOIN jid j_lid ON j_lid._id IN (j_chat._id, j_sender._id) AND j_lid.server = 'lid'
                    JOIN jid j_phone ON j_phone._id IN (j_chat._id, j_sender._id) AND j_phone.server = 's.whatsapp.net'
                    WHERE m.from_me = 0
                """)
            elif has_jid_raw_string:
                conn.execute("""
                    INSERT OR IGNORE INTO _jid_map_resolved (lid_row_id, jid_row_id)
                    SELECT j_lid._id, j_phone._id
                    FROM message m
                    JOIN chat c ON c._id = m.chat_row_id AND c.subject IS NULL
                    JOIN jid j_chat ON j_chat._id = c.jid_row_id
                    JOIN jid j_sender ON j_sender._id = m.sender_jid_row_id
                    JOIN jid j_lid ON j_lid._id IN (j_chat._id, j_sender._id) AND j_lid.raw_string LIKE '%@lid'
                    JOIN jid j_phone ON j_phone._id IN (j_chat._id, j_sender._id) AND j_phone.raw_string LIKE '%@s.whatsapp.net'
                    WHERE m.from_me = 0
                """)
        except sqlite3.OperationalError:
            pass

    # 7. Bridge any companion device LIDs sharing user identity with resolved primary LIDs
    if "jid" in tables and has_jid_server:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO _jid_map_resolved (lid_row_id, jid_row_id)
                SELECT j_comp._id, jm.jid_row_id
                FROM _jid_map_resolved jm
                JOIN jid j_pri ON j_pri._id = jm.lid_row_id
                JOIN jid j_comp ON j_comp.user = j_pri.user AND j_comp.server = 'lid'
                WHERE j_comp._id != jm.lid_row_id
            """)
        except sqlite3.OperationalError:
            pass

    # 8. Mirror into _lid_map_resolved
    try:
        conn.execute("""
            INSERT OR IGNORE INTO _lid_map_resolved (jid_row_id, lid_row_id)
            SELECT jid_row_id, lid_row_id
            FROM _jid_map_resolved
        """)
    except sqlite3.OperationalError:
        pass


def _find_ios_db(wa_db_path: str, rel_path: str, output_root: Path | None) -> Path | None:
    db_p = Path(wa_db_path)
    candidates = [
        db_p.parent / rel_path,
        db_p.parent / "Whatsapp Databases" / rel_path,
        db_p.parent.parent / "Whatsapp Databases" / rel_path,
        db_p.parent.parent / rel_path,
        db_p.parent / "ExtChatDB" / rel_path,
    ]
    if output_root is not None:
        candidates.extend(
            [
                output_root / "Whatsapp Databases" / rel_path,
                output_root / rel_path,
                output_root / "ExtChatDB" / rel_path,
            ]
        )
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def _find_ios_call_history_db(wa_db_path: str, output_root: Path | None) -> Path | None:
    for name in (
        "CallHistory.sqlite",
        "CallHistory.storedata",
        "Library/CallHistory/CallHistory.storedata",
        "Library/CallHistory/CallHistory.sqlite",
        "CallHistory/CallHistory.sqlite",
    ):
        found = _find_ios_db(wa_db_path, name, output_root)
        if found is not None:
            return found
    return None


def open_and_migrate_archive_db(
    archive_db_path: Path,
    rescan: bool,
    source_is_changed: bool,
    has_prior_stamp: bool,
) -> tuple[sqlite3.Connection, bool]:
    """Open .wa_media_archiver.db, ensure tables/migrations, and report if recent cache cleared."""
    _ensure_writable(archive_db_path)
    archive_conn = sqlite3.connect(str(archive_db_path), check_same_thread=False)
    archive_conn.execute("PRAGMA journal_mode = WAL")
    archive_conn.execute("PRAGMA busy_timeout = 5000")
    archive_conn.row_factory = sqlite3.Row
    archive_conn.executescript("""
        CREATE TABLE IF NOT EXISTS contacts (
            number TEXT PRIMARY KEY,
            folder TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS groups (
            chat_row_id TEXT PRIMARY KEY,
            folder TEXT NOT NULL DEFAULT '',
            subject TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS archive_copies (
            original_path TEXT PRIMARY KEY,
            archive_path TEXT NOT NULL,
            sha256 TEXT,
            is_hardlink INTEGER NOT NULL DEFAULT 0,
            timestamp_ms INTEGER
        );
        CREATE TABLE IF NOT EXISTS recent_messages (
            chat_id        TEXT NOT NULL,
            chat_type      TEXT NOT NULL,
            msg_id         INTEGER NOT NULL,
            stanza_id      TEXT,
            timestamp_ms   INTEGER NOT NULL,
            sender         TEXT NOT NULL,
            from_me        INTEGER NOT NULL,
            archive_path   TEXT,
            media_type     TEXT NOT NULL DEFAULT 'text',
            media_name     TEXT,
            text_body      TEXT NOT NULL,
            quoted_text    TEXT,
            quoted_sender  TEXT,
            quoted_ts      INTEGER,
            quoted_msg_id  INTEGER,
            reactions      TEXT,
            is_edited      INTEGER NOT NULL DEFAULT 0,
            edited_ts      INTEGER,
            is_view_once   INTEGER NOT NULL DEFAULT 0,
            link_title     TEXT,
            link_desc      TEXT,
            link_url       TEXT,
            link_preview_type INTEGER NOT NULL DEFAULT 0,
            has_thumbnail  INTEGER NOT NULL DEFAULT 0,
            quoted_has_thumb INTEGER NOT NULL DEFAULT 0,
            is_starred     INTEGER NOT NULL DEFAULT 0,
            is_pinned      INTEGER NOT NULL DEFAULT 0,
            mentions       TEXT,
            PRIMARY KEY (chat_id, chat_type, msg_id)
        );
        CREATE INDEX IF NOT EXISTS idx_recent_chat_ts
            ON recent_messages(chat_id, chat_type, timestamp_ms DESC);
        CREATE INDEX IF NOT EXISTS idx_archive_copies_path
            ON archive_copies(archive_path);

        CREATE TABLE IF NOT EXISTS reactions_cache (
            chat_id    TEXT NOT NULL,
            msg_id     INTEGER NOT NULL,
            reactions  TEXT NOT NULL,
            PRIMARY KEY (chat_id, msg_id)
        );
    """)
    copy_cols = [r[1] for r in archive_conn.execute("PRAGMA table_info(archive_copies)").fetchall()]
    if "is_hardlink" not in copy_cols:
        archive_conn.execute(
            "ALTER TABLE archive_copies ADD COLUMN is_hardlink INTEGER NOT NULL DEFAULT 0"
        )
    if "timestamp_ms" not in copy_cols:
        archive_conn.execute("ALTER TABLE archive_copies ADD COLUMN timestamp_ms INTEGER")

    arch_user_ver = archive_conn.execute("PRAGMA user_version").fetchone()[0]
    cols = [r[1] for r in archive_conn.execute("PRAGMA table_info(recent_messages)").fetchall()]
    for col_name, col_def in (
        ("stanza_id", "TEXT"),
        ("reactions", "TEXT"),
        ("is_edited", "INTEGER NOT NULL DEFAULT 0"),
        ("edited_ts", "INTEGER"),
        ("is_view_once", "INTEGER NOT NULL DEFAULT 0"),
        ("quoted_msg_id", "INTEGER"),
        ("link_title", "TEXT"),
        ("link_desc", "TEXT"),
        ("link_url", "TEXT"),
        ("link_preview_type", "INTEGER NOT NULL DEFAULT 0"),
        ("has_thumbnail", "INTEGER NOT NULL DEFAULT 0"),
        ("quoted_has_thumb", "INTEGER NOT NULL DEFAULT 0"),
        ("is_starred", "INTEGER NOT NULL DEFAULT 0"),
        ("is_pinned", "INTEGER NOT NULL DEFAULT 0"),
        ("mentions", "TEXT"),
    ):
        if col_name not in cols:
            archive_conn.execute(f"ALTER TABLE recent_messages ADD COLUMN {col_name} {col_def}")

    cleared_cache = False
    if arch_user_ver < 5 or rescan or (source_is_changed and has_prior_stamp):
        archive_conn.execute("DELETE FROM recent_messages")
        cleared_cache = True
        has_rx_cache = bool(
            archive_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reactions_cache'"
            ).fetchone()
        )
        if has_rx_cache and (rescan or (source_is_changed and has_prior_stamp)):
            archive_conn.execute("DELETE FROM reactions_cache")
    if arch_user_ver < 5:
        archive_conn.execute("PRAGMA user_version = 5")
    try:
        archive_conn.execute(
            """UPDATE recent_messages
               SET media_type = 'deleted',
                   text_body = CASE WHEN from_me = 1 THEN 'You deleted this message' ELSE 'This message was deleted' END
               WHERE chat_type = 'contact'
                 AND (
                     media_type = 'service'
                     OR text_body LIKE '%group subject%'
                     OR text_body LIKE '%group description%'
                 )"""
        )
        archive_conn.execute(
            """UPDATE recent_messages
               SET quoted_has_thumb = 1
               WHERE quoted_has_thumb = 0
                 AND (
                     quoted_text LIKE '📷%'
                     OR quoted_text LIKE '🎥%'
                     OR quoted_text LIKE '🎞️%'
                     OR quoted_text LIKE 'Sticker%'
                     OR quoted_text LIKE '📄%'
                 )"""
        )
        archive_conn.execute(
            """DELETE FROM recent_messages
               WHERE media_type IN ('location', 'live_location')
                  OR (media_type = 'link' AND (text_body LIKE 'Location%' OR text_body LIKE 'Live Location%'))"""
        )
    except sqlite3.OperationalError:
        pass
    archive_conn.commit()
    return archive_conn, cleared_cache
