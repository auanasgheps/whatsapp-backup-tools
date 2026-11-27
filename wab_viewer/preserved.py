"""Preserved-messages database detection, querying, and live-merge logic."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path


def _detect_preserved_db(output_root: Path) -> Path | None:
    wa_db_dir = output_root / "Whatsapp Databases"
    for base in (wa_db_dir, output_root):
        cand = base / "preserved_messages.db"
        if cand.exists():
            return cand
    return None


def _query_preserved_messages(
    pres_conn: sqlite3.Connection,
    chat_id: str,
    chat_type: str | None,
    before: int | None,
    after: int | None,
    before_inclusive: bool,
    after_inclusive: bool,
    limit: int,
) -> list[dict]:
    type_clause = " AND chat_type = ?" if chat_type else ""
    type_params = [chat_type] if chat_type else []

    if before is not None:
        b_op = "<=" if before_inclusive else "<"
        sql = f"""
            SELECT rowid, * FROM preserved_messages
            WHERE chat_id = ?{type_clause} AND timestamp_ms {b_op} ?
            ORDER BY timestamp_ms DESC, rowid DESC LIMIT ?
        """
        rows = pres_conn.execute(sql, [chat_id] + type_params + [before, limit]).fetchall()
    elif after is not None:
        a_op = ">=" if after_inclusive else ">"
        sql = f"""
            SELECT rowid, * FROM preserved_messages
            WHERE chat_id = ?{type_clause} AND timestamp_ms {a_op} ?
            ORDER BY timestamp_ms ASC, rowid ASC LIMIT ?
        """
        rows = pres_conn.execute(sql, [chat_id] + type_params + [after, limit]).fetchall()
    else:
        sql = f"""
            SELECT rowid, * FROM preserved_messages
            WHERE chat_id = ?{type_clause}
            ORDER BY timestamp_ms DESC, rowid DESC LIMIT ?
        """
        rows = pres_conn.execute(sql, [chat_id] + type_params + [limit]).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        d["msg_id"] = -int(d["rowid"])
        d["sort_id"] = -int(d["rowid"])
        d["is_preserved"] = 1
        d["is_view_once"] = int(d.get("is_view_once") or 0)
        d["is_edited"] = int(d.get("is_edited") or 0)
        d["is_starred"] = int(d.get("is_starred") or 0)
        d["is_pinned"] = int(d.get("is_pinned") or 0)
        d["quoted_msg_id"] = None
        path_check = (d.get("archive_path") or d.get("original_path") or "").lower()
        d["is_voice_note"] = (
            1
            if d.get("media_type") == "audio"
            and ("voice notes" in path_check or path_check.endswith(".opus"))
            else 0
        )
        if d.get("media_type") == "audio":
            if d["is_voice_note"]:
                d["media_name"] = "Voice message"
            elif not d.get("media_name") or d.get("media_name").endswith(".opus"):
                d["media_name"] = "Audio"
        if d.get("live_location_share_duration") and d.get("media_type") == "location":
            d["media_type"] = "live_location"
        if d.get("poll_data"):
            try:
                p = (
                    json.loads(d["poll_data"])
                    if isinstance(d["poll_data"], str)
                    else d["poll_data"]
                )
                if isinstance(p, dict):
                    opts = []
                    for opt in p.get("options") or []:
                        vc = opt.get("vote_count", opt.get("votes", 0))
                        opts.append(
                            {
                                "id": opt.get("id"),
                                "name": opt.get("name", ""),
                                "vote_count": vc,
                                "user_selected": bool(opt.get("user_selected", False)),
                            }
                        )
                    total_v = p.get("total_votes", sum(o["vote_count"] for o in opts))
                    multi = p.get("multiple_answers")
                    if multi is None:
                        multi = p.get("selectable_options_count") == 0
                    d["poll"] = {
                        "question": p.get("question") or d.get("text_body") or "Poll",
                        "multiple_answers": bool(multi),
                        "total_votes": total_v,
                        "total_voters": p.get("total_voters", total_v),
                        "options": opts,
                    }
            except Exception:
                pass
        if d.get("media_type") == "poll" and not d.get("poll"):
            d["poll"] = {
                "question": d.get("text_body") or "Poll",
                "multiple_answers": False,
                "total_votes": 0,
                "total_voters": 0,
                "options": [],
            }
        d["link_title"] = None
        d["link_desc"] = None
        d["link_url"] = None
        d["link_preview_type"] = 0
        d["has_thumbnail"] = 0
        if d.get("service_metadata"):
            try:
                sm = (
                    json.loads(d["service_metadata"])
                    if isinstance(d["service_metadata"], str)
                    else d["service_metadata"]
                )
                if isinstance(sm, dict) and "link_preview" in sm:
                    lp = sm["link_preview"]
                    d["link_title"] = lp.get("title")
                    d["link_desc"] = lp.get("desc")
                    d["link_url"] = lp.get("url")
                    d["link_preview_type"] = int(lp.get("preview_type", 0) or 0)
                    if lp.get("thumbnail_b64"):
                        d["has_thumbnail"] = 1
            except Exception:
                pass
        if not d.get("link_url") and (d.get("link_title") or d.get("media_type") == "link"):
            m = re.search(r"(?:https?://|www\.)[^\s<>\"']+", d.get("text_body") or "")
            if m:
                cand_url = m.group(0).rstrip(".,;:!?)'\"")
                if cand_url.startswith("www."):
                    cand_url = "https://" + cand_url
                d["link_url"] = cand_url
        d.pop("rowid", None)
        d.pop("action_type", None)
        d.pop("participant_name", None)
        d.setdefault(
            "is_video_note",
            1
            if (
                d.get("media_type") == "video"
                and (
                    (
                        d.get("original_path")
                        and (
                            "video notes" in str(d["original_path"]).lower()
                            or "ptv-" in str(d["original_path"]).lower()
                        )
                    )
                    or (
                        d.get("archive_path")
                        and (
                            "video notes" in str(d["archive_path"]).lower()
                            or "ptv-" in str(d["archive_path"]).lower()
                        )
                    )
                )
            )
            else 0,
        )
        d.setdefault("raw_msg_type", 28 if d.get("is_video_note") else None)
        result.append(d)
    return result


def _resolve_quoted_msg_ids(
    rows: list[dict],
    source_type: str,
    wa_conn: sqlite3.Connection | None,
    pres_conn: sqlite3.Connection | None,
) -> None:
    if not rows:
        return

    try:
        unresolved = [
            r for r in rows if r.get("quoted_msg_id") is None and r.get("quoted_stanza_id")
        ]
        if not unresolved:
            return

        local_map = {
            r.get("stanza_id"): (
                r["msg_id"],
                r.get("timestamp_ms", 0),
                r.get("text_body"),
                r.get("sender"),
            )
            for r in rows
            if r.get("stanza_id") and r.get("msg_id") is not None
        }
        still_needed = set()
        for r in unresolved:
            q_stanza = r["quoted_stanza_id"]
            if q_stanza in local_map:
                r["quoted_msg_id"], loc_ts, loc_text, loc_sender = local_map[q_stanza]
                if not r.get("quoted_ts") and loc_ts:
                    r["quoted_ts"] = loc_ts
                if not r.get("quoted_text") and loc_text:
                    r["quoted_text"] = loc_text
                if not r.get("quoted_sender") and loc_sender:
                    r["quoted_sender"] = loc_sender
            else:
                still_needed.add(q_stanza)

        if not still_needed:
            return

        if pres_conn is not None:
            ph = ",".join("?" * len(still_needed))
            try:
                pres_matches = pres_conn.execute(
                    f"SELECT stanza_id, -rowid AS pres_id, timestamp_ms, text_body, sender FROM preserved_messages WHERE stanza_id IN ({ph})",
                    list(still_needed),
                ).fetchall()
                pres_map = {
                    m["stanza_id"]: (
                        m["pres_id"],
                        m["timestamp_ms"],
                        m["text_body"],
                        m["sender"],
                    )
                    for m in pres_matches
                }
                for r in unresolved:
                    if r.get("quoted_msg_id") is None and r["quoted_stanza_id"] in pres_map:
                        pid, pts, ptext, psender = pres_map[r["quoted_stanza_id"]]
                        r["quoted_msg_id"] = pid
                        if not r.get("quoted_ts") and pts:
                            r["quoted_ts"] = pts
                        if not r.get("quoted_text") and ptext:
                            r["quoted_text"] = ptext
                        if not r.get("quoted_sender") and psender:
                            r["quoted_sender"] = psender
                        still_needed.discard(r["quoted_stanza_id"])
            except sqlite3.OperationalError:
                pass

        if not still_needed or wa_conn is None:
            return

        ph = ",".join("?" * len(still_needed))
        try:
            if source_type == "android":
                live_matches = wa_conn.execute(
                    f"SELECT key_id AS stanza_id, _id AS live_id, timestamp, message_type FROM message WHERE key_id IN ({ph})",
                    list(still_needed),
                ).fetchall()
                live_map = {
                    m["stanza_id"]: (m["live_id"], m["timestamp"], m["message_type"])
                    for m in live_matches
                }
            else:
                tables = {
                    row[0]
                    for row in wa_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                has_mi = "ZWAMEDIAITEM" in tables
                mi_join = "LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM" if has_mi else ""
                mi_title_col = ", mi.ZTITLE AS media_title" if has_mi else ", NULL AS media_title"
                live_matches = wa_conn.execute(
                    f"""
                    SELECT m.ZSTANZAID AS stanza_id,
                           m.Z_PK AS live_id,
                           CAST((m.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER) AS ts,
                           m.ZMESSAGETYPE AS mtype,
                           m.ZTEXT AS text,
                           m.ZISFROMME AS from_me,
                           COALESCE(m.ZPUSHNAME, m.ZFROMJID, '') AS raw_sender
                           {mi_title_col}
                    FROM ZWAMESSAGE m
                    {mi_join}
                    WHERE m.ZSTANZAID IN ({ph})
                    """,
                    list(still_needed),
                ).fetchall()
                live_map = {m["stanza_id"]: m for m in live_matches}

            ios_type_labels = {
                1: "📷 Photo",
                38: "📷 Photo",
                2: "🎥 Video",
                39: "🎥 Video",
                54: "🎥 Video",
                3: "🎵 Audio",
                53: "🎵 Audio",
                11: "🎞️ GIF",
                5: "📍 Location",
                16: "📍 Live location",
                15: "Sticker",
                8: "📄 Document",
                46: "Poll",
            }
            for r in unresolved:
                if r.get("quoted_msg_id") is None and r["quoted_stanza_id"] in live_map:
                    if source_type == "android":
                        live_id, live_ts, live_type = live_map[r["quoted_stanza_id"]]
                        r["quoted_msg_id"] = live_id
                        if not r.get("quoted_ts"):
                            r["quoted_ts"] = live_ts
                        if live_type in (1, 3, 9, 20, 26, 42, 43):
                            r["quoted_has_thumb"] = 1
                    else:
                        m_row = live_map[r["quoted_stanza_id"]]
                        r["quoted_msg_id"] = m_row["live_id"]
                        if not r.get("quoted_ts"):
                            r["quoted_ts"] = m_row["ts"]
                        live_type = m_row["mtype"]
                        if live_type in (1, 2, 8, 11, 15, 38, 39, 54):
                            r["quoted_has_thumb"] = 1
                        if not r.get("quoted_text"):
                            cand_text = (
                                m_row["text"]
                                or m_row["media_title"]
                                or ios_type_labels.get(live_type, "")
                            )
                            if cand_text:
                                r["quoted_text"] = cand_text
                        if not r.get("quoted_sender"):
                            if m_row["from_me"] == 1:
                                r["quoted_sender"] = "You"
                            elif m_row["raw_sender"]:
                                raw_s = m_row["raw_sender"]
                                phone = raw_s.split("@")[0] if "@" in raw_s else raw_s
                                r["quoted_sender"] = f"+{phone}" if phone.isdigit() else phone
                            elif r.get("from_me") == 0 and r.get("sender"):
                                r["quoted_sender"] = r["sender"]
        except sqlite3.OperationalError:
            pass
    finally:
        for r in rows:
            r.pop("quoted_stanza_id", None)


def _has_preserved_content(msg: dict) -> bool:
    """Check if a preserved message record contains actual preserved content."""
    if msg.get("text_body") and msg["text_body"].strip():
        return True
    if msg.get("archive_path") or msg.get("original_path"):
        return True
    if msg.get("poll_data") or msg.get("poll"):
        return True
    if msg.get("latitude") is not None or msg.get("location_name"):
        return True
    if msg.get("vcard_data"):
        return True
    if msg.get("media_type") in ("image", "video", "audio", "document", "sticker", "gif"):
        return True
    return False


def _merge_messages(
    live_rows: list[dict],
    pres_rows: list[dict],
    descending: bool,
    limit: int,
) -> list[dict]:
    """Merge live messages from the active database with preserved historical records.

    Invariants:
    - Phase 1: Revoked/deleted placeholders (media_type == 'deleted') are re-hydrated
      with pre-revocation preserved content only if the preserved record has meaningful content.
    - Phase 2: Live non-deleted messages take absolute precedence. If a message is
      alive in the database, any duplicate preserved stub is discarded.
    - Preserved rows with no content are discarded.
    - Matches on stanza_id (with fallback to timestamp/from_me when stanza_id is absent).
    - Preserved polls and options are deduped against live polls.
    """
    if not pres_rows:
        return live_rows[:limit]
    if not live_rows:
        return [p for p in pres_rows if _has_preserved_content(p)][:limit]

    merged = list(live_rows)
    used_pres_indexes = set()

    # Phase 1: Re-hydrate deleted/revoked stubs with preserved content
    for i, p in enumerate(pres_rows):
        if p.get("preservation_reason") in ("revoked_by_sender", "revoked_by_admin"):
            if not _has_preserved_content(p):
                continue
            p_stanza = p.get("stanza_id")
            p_ts = p.get("timestamp_ms", 0)
            p_fm = p.get("from_me", 0)
            for j, live in enumerate(merged):
                if live.get("media_type") == "deleted" and live.get("from_me") == p_fm:
                    if p_stanza and live.get("stanza_id") == p_stanza:
                        merged[j] = p
                        used_pres_indexes.add(i)
                        break
                    elif abs(live.get("timestamp_ms", 0) - p_ts) < 2000:
                        merged[j] = p
                        used_pres_indexes.add(i)
                        break

    live_stanzas = {r["stanza_id"] for r in merged if r.get("stanza_id")}
    live_keys = {(r.get("timestamp_ms"), r.get("from_me")) for r in merged}

    # Phase 2: Append preserved rows that do not exist in the live database
    for i, p in enumerate(pres_rows):
        if i in used_pres_indexes:
            continue
        if not _has_preserved_content(p):
            continue
        p_stanza = p.get("stanza_id")
        p_ts = p.get("timestamp_ms", 0)
        p_fm = p.get("from_me", 0)

        # Invariant: If the message exists alive in the database (not deleted),
        # never duplicate it or show it as preserved!
        if p_stanza:
            if p_stanza in live_stanzas:
                continue
            # Special protection for polls: check if a live poll exists matching this poll
            if p.get("media_type") == "poll":
                is_live_poll = False
                for live in merged:
                    if live.get("media_type") == "poll" and live.get("from_me") == p_fm:
                        if abs(live.get("timestamp_ms", 0) - p_ts) < 2000:
                            is_live_poll = True
                            break
                if is_live_poll:
                    continue
            merged.append(p)
            live_stanzas.add(p_stanza)
        else:
            key = (p_ts, p_fm)
            if key not in live_keys:
                merged.append(p)
                live_keys.add(key)

    merged.sort(
        key=lambda r: (r.get("timestamp_ms", 0), r.get("sort_id") or r.get("msg_id") or 0),
        reverse=descending,
    )
    return merged[:limit]


def _merge_preserved_chats(
    result: list[dict],
    pres_conn: sqlite3.Connection,
    archive_conn: sqlite3.Connection,
) -> list[dict]:
    existing_chats = {(c["id"], c["type"]) for c in result}
    for c in result:
        c.setdefault("is_preserved_only", False)
        c.setdefault("is_archived", 0)
        c.setdefault("description", None)
        c.setdefault("unread_count", 0)
        c.setdefault("ephemeral_duration", 0)
        c.setdefault("is_pinned", False)
        c.setdefault("pinned_time", 0)
        c.setdefault("is_business", False)
        c.setdefault("business_name", None)

    try:
        pres_cols = {
            c[1] for c in pres_conn.execute("PRAGMA table_info(preserved_chats)").fetchall()
        }
    except sqlite3.OperationalError:
        pres_cols = set()

    select_cols = ["chat_id", "chat_type", "newest_ts"]
    if "subject" in pres_cols:
        select_cols.append("subject")
    if "description" in pres_cols:
        select_cols.append("description")

    try:
        pres_chats = pres_conn.execute(
            f"SELECT {', '.join(select_cols)} FROM preserved_chats"
        ).fetchall()
    except sqlite3.OperationalError:
        pres_chats = []

    for pc in pres_chats:
        cid = pc["chat_id"]
        ctype = pc["chat_type"]
        if (cid, ctype) in existing_chats:
            continue

        display_name = cid
        description = None
        if ctype == "group":
            grp_row = archive_conn.execute(
                "SELECT subject, folder FROM groups WHERE chat_row_id = ?", (cid,)
            ).fetchone()
            if grp_row and grp_row["subject"]:
                display_name = grp_row["subject"]
            elif "subject" in pc.keys() and pc["subject"]:
                display_name = pc["subject"]
            elif grp_row and grp_row["folder"]:
                display_name = grp_row["folder"]

            if "description" in pc.keys() and pc["description"]:
                description = str(pc["description"]).strip() or None
            if not description:
                try:
                    arch_grp_cols = {
                        c[1] for c in archive_conn.execute("PRAGMA table_info(groups)").fetchall()
                    }
                    if "description" in arch_grp_cols:
                        grp_desc_row = archive_conn.execute(
                            "SELECT NULLIF(description, '') AS description FROM groups WHERE chat_row_id = ?",
                            (cid,),
                        ).fetchone()
                        if grp_desc_row and grp_desc_row["description"]:
                            description = str(grp_desc_row["description"]).strip() or None
                except sqlite3.OperationalError:
                    pass
        else:
            con_row = archive_conn.execute(
                "SELECT display_name, folder FROM contacts WHERE number = ?", (cid,)
            ).fetchone()
            if con_row and con_row["display_name"]:
                display_name = con_row["display_name"]
            elif cid == "0":
                display_name = "WhatsApp"
            elif con_row and con_row["folder"]:
                display_name = con_row["folder"]
            else:
                display_name = f"+{cid}"

        try:
            last_msg = pres_conn.execute(
                """
                SELECT text_body, media_type, from_me, media_name, is_view_once
                FROM preserved_messages
                WHERE chat_id = ? AND chat_type = ?
                ORDER BY timestamp_ms DESC LIMIT 1
                """,
                (cid, ctype),
            ).fetchone()
        except sqlite3.OperationalError:
            last_msg = pres_conn.execute(
                """
                SELECT text_body, media_type, from_me, media_name
                FROM preserved_messages
                WHERE chat_id = ? AND chat_type = ?
                ORDER BY timestamp_ms DESC LIMIT 1
                """,
                (cid, ctype),
            ).fetchone()

        last_preview = ""
        last_type = "text"
        last_from_me = 0
        is_vo = 0
        if last_msg:
            last_preview = last_msg["text_body"] or last_msg["media_name"] or ""
            last_type = last_msg["media_type"] or "text"
            last_from_me = int(last_msg["from_me"])
            if "is_view_once" in last_msg.keys() and last_msg["is_view_once"]:
                is_vo = int(last_msg["is_view_once"])

        result.append(
            {
                "id": cid,
                "type": ctype,
                "phone_number": "" if ctype == "group" else cid,
                "display_name": display_name,
                "description": description,
                "newest_ts": pc["newest_ts"],
                "last_msg_preview": last_preview,
                "last_msg_type": last_type,
                "last_msg_from_me": last_from_me,
                "is_view_once": is_vo,
                "is_preserved_only": True,
                "is_archived": 0,
                "unread_count": 0,
                "ephemeral_duration": 0,
                "is_pinned": False,
                "pinned_time": 0,
                "is_business": False,
                "business_name": None,
            }
        )
        existing_chats.add((cid, ctype))

    result.sort(
        key=lambda c: (
            bool(c.get("is_pinned")),
            int(c.get("pinned_time") or 0),
            int(c.get("newest_ts") or 0),
        ),
        reverse=True,
    )
    return result
