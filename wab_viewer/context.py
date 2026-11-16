"""Per-application runtime state container (ViewerContext) and schema dataclasses."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .db_cache import _ensure_jid_map_resolved, _find_ios_db

if TYPE_CHECKING:
    from flask import Flask


def get_version() -> str:
    """Return installed `whatsapp-backup-tools` version string, falling back to `pyproject.toml`."""
    try:
        from importlib.metadata import version as _pkg_version

        return _pkg_version("whatsapp-backup-tools")
    except Exception:
        import tomllib

        root = Path(__file__).resolve().parents[1]
        return tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]


@dataclass(frozen=True)
class SchemaCapabilities:
    """Detected tables and column capabilities of the opened WhatsApp source DB."""

    has_android_hd: bool
    has_ios_hd: bool
    has_ios_ext_pinned: bool
    ext_pin_key_col: str
    has_ext_thread_id_deleted: bool
    has_reactions: bool
    has_ios_reactions: bool
    has_message_poll: bool
    has_message_add_on: bool
    has_pin_in_chat_table: bool
    has_lid_dn: bool
    has_message_system: bool
    has_mcp: bool
    has_ios_group_event: bool
    has_jid_server: bool
    has_jid_raw_string: bool
    has_android_edit_info: bool
    has_android_add_on: bool
    has_mei_ts: bool
    has_mao_ts: bool
    has_android_key_id: bool
    has_ios_stanza_id: bool
    has_ios_sort: bool
    has_ios_starred: bool
    has_msg_starred: bool
    has_wa_zwamessage_parent: bool
    has_message_thumbnail: bool
    has_media_hash_thumbnail: bool
    has_message_quoted_media: bool
    has_message_quoted_text: bool
    has_message_quoted_location: bool
    has_mm_width: bool
    has_mm_height: bool
    has_mm_size: bool
    has_mm_duration: bool
    has_mm_file_hash: bool
    has_mm_gif_attribution: bool
    has_message_media_map: bool
    has_ios_vcard_name: bool
    has_ios_vcard_string: bool
    has_ios_aspect_ratio: bool
    has_ios_filesize: bool
    has_ios_duration: bool
    has_ios_title: bool
    has_ios_mediaurl: bool
    has_ios_metadata: bool
    has_ios_lat: bool
    has_ios_lng: bool
    has_mq_key_id: bool
    has_mq_msg_type: bool
    has_mq_pm_chat: bool
    has_message_view_once_media: bool
    has_message_revoked: bool
    has_message_location: bool
    has_ml_lat: bool
    has_ml_lng: bool
    has_ml_name: bool
    has_ml_address: bool
    has_ml_url: bool
    has_ml_duration: bool
    has_message_text: bool
    has_mt_title: bool
    has_mt_desc: bool
    has_mt_url: bool
    has_mt_preview_type: bool
    has_chat_archived: bool
    has_cs_archived: bool
    cs_archived_col_name: str
    has_cs_ident: bool
    has_cs_last_msg_date: bool
    mi_cols: frozenset[str]
    mdi_cols: frozenset[str]
    has_ios_mdi: bool
    has_ios_mdi_thumb: bool
    has_ios_mdi_matched: bool
    has_ios_mdi_title: bool
    has_ios_mdi_summary: bool
    has_msg_origin: bool
    has_ios_media_origin: bool


@dataclass(frozen=True)
class QuerySet:
    """Compiled SQL SELECT/FILTER fragments scoped to a single Flask app instance."""

    android_select: str | None
    android_stat_select: str | None
    android_stat_aggregate_select: str | None
    android_filter: str
    android_filter_ts: str
    android_ts: str
    android_is_media: str
    android_is_link: str
    android_is_document_undownloaded: str
    android_gif_cond: str
    ios_select: str
    ios_filter: str
    ios_filter_ts: str
    ios_ts: str
    ios_is_media: str
    ios_is_link: str
    ios_is_document_undownloaded: str


class ViewerContext:
    """Encapsulates all per-app database connections, locks, capabilities, and SQL queries."""

    def __init__(
        self,
        output_root: Path,
        source_type: str | None,
        wa_db_path: str | None,
        archive_db_path: Path,
        preserved_db_path: Path | None,
        cache_conn: sqlite3.Connection,
        archive_conn: sqlite3.Connection,
        indexing_state: dict,
        indexing_lock: threading.Lock,
        bulk_index_state: dict,
        schema_alert: dict | None,
        caps: SchemaCapabilities,
        queries: QuerySet,
    ) -> None:
        self.output_root = output_root
        self.source_type = source_type
        self.wa_db_path = wa_db_path
        self.archive_db_path = archive_db_path
        self.preserved_db_path = preserved_db_path
        self.cache_conn = cache_conn
        self.archive_conn = archive_conn
        self.wa_local = threading.local()
        self.indexing_state = indexing_state
        self.indexing_lock = indexing_lock
        self.bulk_index_state = bulk_index_state
        self.schema_alert = schema_alert
        self.caps = caps
        self.queries = queries
        self.android_chat_row_ids_cache: dict[tuple[str, str], list[int]] = {}
        self.android_contact_chats_cache: tuple[dict[str, str], dict[str, str]] | None = None
        self.ios_contact_chats_cache: tuple[dict[str, str], dict[str, str]] | None = None
        self.thumbnail_404_cache: set[str] = set()

    def get_wa(self) -> sqlite3.Connection | None:
        """Return thread-local SQLite connection to the WhatsApp source DB with ancillary attaches."""
        if self.source_type is None:
            return None
        conn = getattr(self.wa_local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.wa_db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA cache_size = -32000")
            conn.execute("PRAGMA temp_store = MEMORY")
            conn.execute("ATTACH DATABASE ? AS arch", (str(self.archive_db_path),))
            if self.source_type == "android":
                _ensure_jid_map_resolved(conn, self.archive_conn)
            if self.source_type == "ios":
                self._init_ios_connection(conn)
            self.wa_local.conn = conn
        return conn

    def _init_ios_connection(self, conn: sqlite3.Connection) -> None:
        """Attach ancillary iOS databases and populate the temporary contact resolution table."""
        conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _ios_contacts "
            "(jid TEXT PRIMARY KEY, full_name TEXT, phone_number TEXT)"
        )
        contacts_v2 = _find_ios_db(self.wa_db_path, "ContactsV2.sqlite", self.output_root)
        if contacts_v2:
            conn.execute("ATTACH DATABASE ? AS cv", (str(contacts_v2),))
            cv_cols = {
                r["name"]
                for r in conn.execute("PRAGMA cv.table_info(ZWAADDRESSBOOKCONTACT)").fetchall()
            }
            if "ZLID" in cv_cols:
                conn.execute("""
                    INSERT INTO _ios_contacts (jid, full_name, phone_number)
                    SELECT
                        ZLID AS jid,
                        ZFULLNAME AS full_name,
                        CASE WHEN ZWHATSAPPID LIKE '%@s.whatsapp.net'
                             THEN SUBSTR(ZWHATSAPPID, 1, INSTR(ZWHATSAPPID, '@') - 1)
                        END AS phone_number
                    FROM cv.ZWAADDRESSBOOKCONTACT
                    WHERE ZLID IS NOT NULL
                      AND ((ZFULLNAME IS NOT NULL AND ZFULLNAME != '') OR (ZWHATSAPPID LIKE '%@s.whatsapp.net'))
                    ON CONFLICT(jid) DO UPDATE SET
                        full_name = COALESCE(excluded.full_name, _ios_contacts.full_name),
                        phone_number = COALESCE(excluded.phone_number, _ios_contacts.phone_number)
                """)
                conn.execute("""
                    INSERT INTO _ios_contacts (jid, full_name, phone_number)
                    SELECT
                        SUBSTR(ZLID, 1, INSTR(ZLID, '@') - 1) AS jid,
                        ZFULLNAME AS full_name,
                        CASE WHEN ZWHATSAPPID LIKE '%@s.whatsapp.net'
                             THEN SUBSTR(ZWHATSAPPID, 1, INSTR(ZWHATSAPPID, '@') - 1)
                        END AS phone_number
                    FROM cv.ZWAADDRESSBOOKCONTACT
                    WHERE ZLID LIKE '%@lid'
                      AND ((ZFULLNAME IS NOT NULL AND ZFULLNAME != '') OR (ZWHATSAPPID LIKE '%@s.whatsapp.net'))
                    ON CONFLICT(jid) DO UPDATE SET
                        full_name = COALESCE(excluded.full_name, _ios_contacts.full_name),
                        phone_number = COALESCE(excluded.phone_number, _ios_contacts.phone_number)
                """)
            if "ZWHATSAPPID" in cv_cols:
                conn.execute("""
                    INSERT INTO _ios_contacts (jid, full_name, phone_number)
                    SELECT
                        ZWHATSAPPID AS jid,
                        ZFULLNAME AS full_name,
                        CASE WHEN ZWHATSAPPID LIKE '%@s.whatsapp.net'
                             THEN SUBSTR(ZWHATSAPPID, 1, INSTR(ZWHATSAPPID, '@') - 1)
                        END AS phone_number
                    FROM cv.ZWAADDRESSBOOKCONTACT
                    WHERE ZWHATSAPPID IS NOT NULL
                      AND ((ZFULLNAME IS NOT NULL AND ZFULLNAME != '') OR (ZWHATSAPPID LIKE '%@s.whatsapp.net'))
                    ON CONFLICT(jid) DO UPDATE SET
                        full_name = COALESCE(excluded.full_name, _ios_contacts.full_name),
                        phone_number = COALESCE(excluded.phone_number, _ios_contacts.phone_number)
                """)
                conn.execute("""
                    INSERT INTO _ios_contacts (jid, full_name, phone_number)
                    SELECT
                        SUBSTR(ZWHATSAPPID, 1, INSTR(ZWHATSAPPID, '@') - 1) AS jid,
                        ZFULLNAME AS full_name,
                        CASE WHEN ZWHATSAPPID LIKE '%@s.whatsapp.net'
                             THEN SUBSTR(ZWHATSAPPID, 1, INSTR(ZWHATSAPPID, '@') - 1)
                        END AS phone_number
                    FROM cv.ZWAADDRESSBOOKCONTACT
                    WHERE ZWHATSAPPID LIKE '%@s.whatsapp.net'
                    ON CONFLICT(jid) DO UPDATE SET
                        full_name = COALESCE(excluded.full_name, _ios_contacts.full_name),
                        phone_number = COALESCE(excluded.phone_number, _ios_contacts.phone_number)
                """)

        lid_db = _find_ios_db(self.wa_db_path, "LID.sqlite", self.output_root)
        if lid_db:
            conn.execute("ATTACH DATABASE ? AS lid_db", (str(lid_db),))
            lid_cols = {
                r["name"] for r in conn.execute("PRAGMA lid_db.table_info(ZWAZACCOUNT)").fetchall()
            }
            if "ZIDENTIFIER" in lid_cols and "ZPHONENUMBER" in lid_cols:
                conn.execute("""
                    INSERT INTO _ios_contacts (jid, full_name, phone_number)
                    SELECT
                        ZIDENTIFIER AS jid,
                        NULL AS full_name,
                        ZPHONENUMBER AS phone_number
                    FROM lid_db.ZWAZACCOUNT
                    WHERE ZIDENTIFIER IS NOT NULL
                      AND ZPHONENUMBER IS NOT NULL
                      AND ZPHONENUMBER != ''
                    ON CONFLICT(jid) DO UPDATE SET
                        phone_number = COALESCE(_ios_contacts.phone_number, excluded.phone_number)
                """)
                conn.execute("""
                    INSERT INTO _ios_contacts (jid, full_name, phone_number)
                    SELECT
                        SUBSTR(ZIDENTIFIER, 1, INSTR(ZIDENTIFIER, '@') - 1) AS jid,
                        NULL AS full_name,
                        ZPHONENUMBER AS phone_number
                    FROM lid_db.ZWAZACCOUNT
                    WHERE ZIDENTIFIER LIKE '%@lid'
                      AND ZPHONENUMBER IS NOT NULL
                      AND ZPHONENUMBER != ''
                    ON CONFLICT(jid) DO UPDATE SET
                        phone_number = COALESCE(_ios_contacts.phone_number, excluded.phone_number)
                """)

        try:
            conn.execute("""
                INSERT INTO _ios_contacts (jid, full_name, phone_number)
                SELECT
                    ZJID AS jid,
                    ZPUSHNAME AS full_name,
                    CASE WHEN ZJID LIKE '%@s.whatsapp.net'
                         THEN SUBSTR(ZJID, 1, INSTR(ZJID, '@') - 1)
                    END AS phone_number
                FROM ZWAPROFILEPUSHNAME
                WHERE ZJID IS NOT NULL AND ZPUSHNAME IS NOT NULL AND ZPUSHNAME != ''
                ON CONFLICT(jid) DO UPDATE SET
                    full_name = COALESCE(NULLIF(_ios_contacts.full_name, ''), excluded.full_name),
                    phone_number = COALESCE(_ios_contacts.phone_number, excluded.phone_number)
            """)
            conn.execute("""
                INSERT INTO _ios_contacts (jid, full_name, phone_number)
                SELECT
                    SUBSTR(ZJID, 1, INSTR(ZJID, '@') - 1) AS jid,
                    ZPUSHNAME AS full_name,
                    CASE WHEN ZJID LIKE '%@s.whatsapp.net'
                         THEN SUBSTR(ZJID, 1, INSTR(ZJID, '@') - 1)
                    END AS phone_number
                FROM ZWAPROFILEPUSHNAME
                WHERE ZJID IS NOT NULL AND ZPUSHNAME IS NOT NULL AND ZPUSHNAME != ''
                ON CONFLICT(jid) DO UPDATE SET
                    full_name = COALESCE(NULLIF(_ios_contacts.full_name, ''), excluded.full_name),
                    phone_number = COALESCE(_ios_contacts.phone_number, excluded.phone_number)
            """)
        except sqlite3.OperationalError:
            pass

        try:
            cs_cols = {r[1] for r in conn.execute("PRAGMA table_info(ZWACHATSESSION)").fetchall()}
            if "ZCONTACTIDENTIFIER" in cs_cols:
                conn.execute("""
                    INSERT INTO _ios_contacts (jid, full_name, phone_number)
                    SELECT
                        cs.ZCONTACTJID AS jid,
                        NULLIF(cs.ZPARTNERNAME, '') AS full_name,
                        CASE WHEN cs.ZCONTACTIDENTIFIER LIKE '%@s.whatsapp.net'
                             THEN SUBSTR(cs.ZCONTACTIDENTIFIER, 1, INSTR(cs.ZCONTACTIDENTIFIER, '@') - 1)
                        END AS phone_number
                    FROM ZWACHATSESSION cs
                    WHERE cs.ZCONTACTJID LIKE '%@lid'
                      AND cs.ZCONTACTIDENTIFIER LIKE '%@s.whatsapp.net'
                    ON CONFLICT(jid) DO UPDATE SET
                        full_name = COALESCE(NULLIF(_ios_contacts.full_name, ''), excluded.full_name),
                        phone_number = COALESCE(_ios_contacts.phone_number, excluded.phone_number)
                """)
                conn.execute("""
                    INSERT INTO _ios_contacts (jid, full_name, phone_number)
                    SELECT
                        SUBSTR(cs.ZCONTACTJID, 1, INSTR(cs.ZCONTACTJID, '@') - 1) AS jid,
                        NULLIF(cs.ZPARTNERNAME, '') AS full_name,
                        CASE WHEN cs.ZCONTACTIDENTIFIER LIKE '%@s.whatsapp.net'
                             THEN SUBSTR(cs.ZCONTACTIDENTIFIER, 1, INSTR(cs.ZCONTACTIDENTIFIER, '@') - 1)
                        END AS phone_number
                    FROM ZWACHATSESSION cs
                    WHERE cs.ZCONTACTJID LIKE '%@lid'
                      AND cs.ZCONTACTIDENTIFIER LIKE '%@s.whatsapp.net'
                    ON CONFLICT(jid) DO UPDATE SET
                        full_name = COALESCE(NULLIF(_ios_contacts.full_name, ''), excluded.full_name),
                        phone_number = COALESCE(_ios_contacts.phone_number, excluded.phone_number)
                """)
                conn.execute("""
                    INSERT INTO _ios_contacts (jid, full_name, phone_number)
                    SELECT
                        cs.ZCONTACTIDENTIFIER AS jid,
                        NULLIF(cs.ZPARTNERNAME, '') AS full_name,
                        SUBSTR(cs.ZCONTACTJID, 1, INSTR(cs.ZCONTACTJID, '@') - 1) AS phone_number
                    FROM ZWACHATSESSION cs
                    WHERE cs.ZCONTACTJID LIKE '%@s.whatsapp.net'
                      AND cs.ZCONTACTIDENTIFIER LIKE '%@lid'
                    ON CONFLICT(jid) DO UPDATE SET
                        full_name = COALESCE(NULLIF(_ios_contacts.full_name, ''), excluded.full_name),
                        phone_number = COALESCE(_ios_contacts.phone_number, excluded.phone_number)
                """)
                conn.execute("""
                    INSERT INTO _ios_contacts (jid, full_name, phone_number)
                    SELECT
                        SUBSTR(cs.ZCONTACTIDENTIFIER, 1, INSTR(cs.ZCONTACTIDENTIFIER, '@') - 1) AS jid,
                        NULLIF(cs.ZPARTNERNAME, '') AS full_name,
                        SUBSTR(cs.ZCONTACTJID, 1, INSTR(cs.ZCONTACTJID, '@') - 1) AS phone_number
                    FROM ZWACHATSESSION cs
                    WHERE cs.ZCONTACTJID LIKE '%@s.whatsapp.net'
                      AND cs.ZCONTACTIDENTIFIER LIKE '%@lid'
                    ON CONFLICT(jid) DO UPDATE SET
                        full_name = COALESCE(NULLIF(_ios_contacts.full_name, ''), excluded.full_name),
                        phone_number = COALESCE(_ios_contacts.phone_number, excluded.phone_number)
                """)
            conn.execute("""
                INSERT INTO _ios_contacts (jid, full_name, phone_number)
                SELECT
                    cs.ZCONTACTJID AS jid,
                    NULLIF(cs.ZPARTNERNAME, '') AS full_name,
                    SUBSTR(cs.ZCONTACTJID, 1, INSTR(cs.ZCONTACTJID, '@') - 1) AS phone_number
                FROM ZWACHATSESSION cs
                WHERE cs.ZCONTACTJID LIKE '%@s.whatsapp.net'
                  AND cs.ZPARTNERNAME IS NOT NULL AND cs.ZPARTNERNAME != ''
                ON CONFLICT(jid) DO UPDATE SET
                    full_name = COALESCE(NULLIF(_ios_contacts.full_name, ''), excluded.full_name),
                    phone_number = COALESCE(_ios_contacts.phone_number, excluded.phone_number)
            """)
            conn.execute("""
                INSERT INTO _ios_contacts (jid, full_name, phone_number)
                SELECT
                    SUBSTR(cs.ZCONTACTJID, 1, INSTR(cs.ZCONTACTJID, '@') - 1) AS jid,
                    NULLIF(cs.ZPARTNERNAME, '') AS full_name,
                    SUBSTR(cs.ZCONTACTJID, 1, INSTR(cs.ZCONTACTJID, '@') - 1) AS phone_number
                FROM ZWACHATSESSION cs
                WHERE cs.ZCONTACTJID LIKE '%@s.whatsapp.net'
                  AND cs.ZPARTNERNAME IS NOT NULL AND cs.ZPARTNERNAME != ''
                ON CONFLICT(jid) DO UPDATE SET
                    full_name = COALESCE(NULLIF(_ios_contacts.full_name, ''), excluded.full_name),
                    phone_number = COALESCE(_ios_contacts.phone_number, excluded.phone_number)
            """)
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("""
                UPDATE _ios_contacts
                SET full_name = (
                    SELECT con.display_name
                    FROM arch.contacts con
                    WHERE (con.number = _ios_contacts.phone_number
                       OR con.number = _ios_contacts.jid
                       OR con.number = SUBSTR(_ios_contacts.jid, 1, INSTR(_ios_contacts.jid || '@', '@') - 1))
                      AND con.display_name IS NOT NULL
                      AND con.display_name != ''
                      AND con.display_name NOT LIKE 'Unknown%'
                    LIMIT 1
                )
                WHERE (full_name IS NULL OR full_name = '' OR full_name LIKE '+%')
                  AND EXISTS (
                      SELECT 1 FROM arch.contacts con
                      WHERE (con.number = _ios_contacts.phone_number
                         OR con.number = _ios_contacts.jid
                         OR con.number = SUBSTR(_ios_contacts.jid, 1, INSTR(_ios_contacts.jid || '@', '@') - 1))
                        AND con.display_name IS NOT NULL
                        AND con.display_name != ''
                        AND con.display_name NOT LIKE 'Unknown%'
                  )
            """)
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("""
                UPDATE _ios_contacts
                SET full_name = (
                    SELECT p.full_name FROM _ios_contacts p
                    WHERE p.jid = (_ios_contacts.phone_number || '@s.whatsapp.net')
                      AND p.full_name IS NOT NULL AND p.full_name != ''
                      AND p.full_name NOT LIKE '+%'
                    LIMIT 1
                )
                WHERE (full_name IS NULL OR full_name = '' OR full_name LIKE '+%')
                  AND phone_number IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM _ios_contacts p
                      WHERE p.jid = (_ios_contacts.phone_number || '@s.whatsapp.net')
                        AND p.full_name IS NOT NULL AND p.full_name != ''
                        AND p.full_name NOT LIKE '+%'
                  )
            """)
            conn.execute("""
                UPDATE _ios_contacts
                SET full_name = (
                    SELECT l.full_name FROM _ios_contacts l
                    WHERE l.phone_number = SUBSTR(_ios_contacts.jid, 1, INSTR(_ios_contacts.jid, '@') - 1)
                      AND l.full_name IS NOT NULL AND l.full_name != ''
                      AND l.full_name NOT LIKE '+%'
                    LIMIT 1
                )
                WHERE (full_name IS NULL OR full_name = '' OR full_name LIKE '+%')
                  AND _ios_contacts.jid LIKE '%@s.whatsapp.net'
                  AND EXISTS (
                      SELECT 1 FROM _ios_contacts l
                      WHERE l.phone_number = SUBSTR(_ios_contacts.jid, 1, INSTR(_ios_contacts.jid, '@') - 1)
                        AND l.full_name IS NOT NULL AND l.full_name != ''
                        AND l.full_name NOT LIKE '+%'
                  )
            """)
        except sqlite3.OperationalError:
            pass

        try:
            user_row = conn.execute(
                "SELECT ZTOJID FROM ZWAMESSAGE WHERE ZFROMJID LIKE '%@g.us' AND ZTOJID LIKE '%@s.whatsapp.net' LIMIT 1"
            ).fetchone()
            if not user_row:
                user_row = conn.execute(
                    "SELECT ZTOJID FROM ZWAMESSAGE WHERE ZISFROMME = 0 AND ZTOJID LIKE '%@s.whatsapp.net' LIMIT 1"
                ).fetchone()
            if user_row and user_row[0]:
                u_jid = user_row[0]
                u_phone = u_jid.split("@")[0]
                conn.execute(
                    "INSERT OR REPLACE INTO _ios_contacts (jid, full_name, phone_number) VALUES (?, 'You', ?)",
                    (u_jid, u_phone),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO _ios_contacts (jid, full_name, phone_number) VALUES (?, 'You', ?)",
                    (u_phone, u_phone),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO _ios_contacts (jid, full_name, phone_number) VALUES (?, 'You', ?)",
                    (f"+{u_phone}", u_phone),
                )
        except sqlite3.OperationalError:
            pass

        conn.execute("CREATE INDEX IF NOT EXISTS _ios_contacts_jid ON _ios_contacts(jid)")
        infra_db = _find_ios_db(self.wa_db_path, "MessagingInfraDatabase.sqlite", self.output_root)
        if not infra_db:
            infra_db = _find_ios_db(
                self.wa_db_path,
                "MessagingInfraDB_v2/MessagingInfraDatabase.sqlite",
                self.output_root,
            )
        if infra_db:
            conn.execute("ATTACH DATABASE ? AS infra", (str(infra_db),))
        ext_db = _find_ios_db(self.wa_db_path, "ExtChatDatabase.sqlite", self.output_root)
        if not ext_db:
            ext_db = _find_ios_db(
                self.wa_db_path, "ExtChatDB/ExtChatDatabase.sqlite", self.output_root
            )
        if ext_db:
            try:
                conn.execute("ATTACH DATABASE ? AS ext", (str(ext_db),))
                self._enrich_ios_contacts_from_ext(conn)
            except sqlite3.OperationalError:
                pass

    def _enrich_ios_contacts_from_ext(self, conn: sqlite3.Connection) -> None:
        """Merge LID/PN/push-name mappings from ExtChatDatabase.sqlite contact_metadata."""
        has_cm = bool(
            conn.execute(
                "SELECT 1 FROM ext.sqlite_master WHERE type='table' AND name='contact_metadata'"
            ).fetchone()
        )
        if not has_cm:
            return
        cm_cols = {r[1] for r in conn.execute("PRAGMA ext.table_info(contact_metadata)").fetchall()}
        if not {"contact_lid", "contact_pn"}.issubset(cm_cols):
            return
        push_expr = "NULLIF(contact_push_name, '')" if "contact_push_name" in cm_cols else "NULL"
        conn.execute(
            f"""
            INSERT INTO _ios_contacts (jid, full_name, phone_number)
            SELECT
                CASE
                    WHEN INSTR(contact_lid, '@') > 0 THEN contact_lid
                    ELSE contact_lid || '@lid'
                END,
                {push_expr},
                CASE
                    WHEN contact_pn IS NOT NULL AND contact_pn != ''
                    THEN CASE
                        WHEN INSTR(contact_pn, '@') > 0
                        THEN SUBSTR(contact_pn, 1, INSTR(contact_pn, '@') - 1)
                        ELSE contact_pn
                    END
                END
            FROM ext.contact_metadata
            WHERE contact_lid IS NOT NULL AND contact_lid != ''
            ON CONFLICT(jid) DO UPDATE SET
                full_name = COALESCE(NULLIF(_ios_contacts.full_name, ''), excluded.full_name),
                phone_number = COALESCE(_ios_contacts.phone_number, excluded.phone_number)
            """
        )
        conn.execute(
            f"""
            INSERT INTO _ios_contacts (jid, full_name, phone_number)
            SELECT
                CASE
                    WHEN INSTR(contact_pn, '@') > 0 THEN contact_pn
                    ELSE contact_pn || '@s.whatsapp.net'
                END,
                {push_expr},
                CASE
                    WHEN INSTR(contact_pn, '@') > 0
                    THEN SUBSTR(contact_pn, 1, INSTR(contact_pn, '@') - 1)
                    ELSE contact_pn
                END
            FROM ext.contact_metadata
            WHERE contact_pn IS NOT NULL AND contact_pn != ''
            ON CONFLICT(jid) DO UPDATE SET
                full_name = COALESCE(NULLIF(_ios_contacts.full_name, ''), excluded.full_name),
                phone_number = COALESCE(_ios_contacts.phone_number, excluded.phone_number)
            """
        )
        # Propagate existing ContactsV2 full_name from PN row to newly linked LID row
        conn.execute(
            """
            UPDATE _ios_contacts
            SET full_name = (
                SELECT p.full_name FROM _ios_contacts p
                WHERE p.jid = (_ios_contacts.phone_number || '@s.whatsapp.net')
                  AND p.full_name IS NOT NULL AND p.full_name != '' AND p.full_name NOT LIKE '+%'
                LIMIT 1
            )
            WHERE (full_name IS NULL OR full_name = '' OR full_name LIKE '+%')
              AND phone_number IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM _ios_contacts p
                  WHERE p.jid = (_ios_contacts.phone_number || '@s.whatsapp.net')
                    AND p.full_name IS NOT NULL AND p.full_name != '' AND p.full_name NOT LIKE '+%'
              )
            """
        )

    def get_preserved(self) -> sqlite3.Connection | None:
        """Return thread-local read-only connection to preserved_messages.db, if present."""
        if self.preserved_db_path is None or not self.preserved_db_path.exists():
            return None
        conn = getattr(self.wa_local, "pres_conn", None)
        if conn is None:
            conn = sqlite3.connect(
                f"file:{self.preserved_db_path.resolve().as_posix()}?mode=ro",
                uri=True,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA cache_size = -32000")
            conn.execute("PRAGMA temp_store = MEMORY")
            conn.execute("PRAGMA mmap_size = 268435456")
            self.wa_local.pres_conn = conn
        return conn

    def close_wa_conn(self, exc: BaseException | None) -> None:
        """Commit thread-local SQLite connections on Flask appcontext teardown."""
        if self.source_type is not None:
            conn = getattr(self.wa_local, "conn", None)
            if conn is not None:
                try:
                    conn.commit()
                except sqlite3.Error:
                    pass
        pres_conn = getattr(self.wa_local, "pres_conn", None)
        if pres_conn is not None:
            try:
                pres_conn.commit()
            except sqlite3.Error:
                pass

    def get_cache(self) -> sqlite3.Connection:
        """Return shared FTS/preference cache DB connection."""
        return self.cache_conn

    def get_archive(self) -> sqlite3.Connection:
        """Return shared `.wa_media_archiver.db` connection."""
        return self.archive_conn


def get_viewer_context() -> ViewerContext:
    """Return the active `ViewerContext` stored on the current Flask application."""
    from flask import current_app

    return current_app.extensions["viewer"]


def get_app_viewer_context(app: Flask) -> ViewerContext:
    """Return the `ViewerContext` stored on an explicit Flask application instance."""
    return app.extensions["viewer"]
