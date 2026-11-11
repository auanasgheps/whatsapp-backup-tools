"""Message hydration, service-event formatting, reaction caching, and iOS receipts."""

from __future__ import annotations

import collections
import json
import os
import re
import sqlite3
from pathlib import Path

from .protobuf import (
    _extract_ios_link_description,
    _extract_ios_quoted_info,
    _extract_ios_reactions,
    _parse_ios_poll_blob,
)
from .system_events import (
    ALLOWED_GROUP_SYSTEM_ACTIONS,
    format_android_system_event,
    format_ios_system_event,
    format_participant_names,
)


def _strip_none_reactions(rows: list) -> list:
    result = []
    for r in rows:
        d = dict(r)
        if d.get("reactions") is None:
            d.pop("reactions", None)
        result.append(d)
    return result


def _resolve_ios_jid(jid_str: str, contacts_map: dict) -> str:
    """Resolve an iOS JID (phone or @lid) to a contact name, phone number, or Unknown."""
    if not jid_str:
        return "Someone"
    jid_str = jid_str.strip()
    if jid_str in ("0@s.whatsapp.net", "0"):
        return "WhatsApp"
    if jid_str in contacts_map:
        full_name, phone_number = contacts_map[jid_str]
        if full_name:
            return full_name
        if phone_number:
            return f"+{phone_number}"
    if not jid_str.endswith("@s.whatsapp.net") and not jid_str.endswith("@lid"):
        alt_s = f"{jid_str}@s.whatsapp.net"
        if alt_s in contacts_map:
            fn, pn = contacts_map[alt_s]
            if fn:
                return fn
            if pn:
                return f"+{pn}"
        alt_lid = f"{jid_str}@lid"
        if alt_lid in contacts_map:
            fn, pn = contacts_map[alt_lid]
            if fn:
                return fn
            if pn:
                return f"+{pn}"
    elif jid_str.endswith("@s.whatsapp.net"):
        bare = jid_str.split("@")[0]
        if bare in contacts_map:
            fn, pn = contacts_map[bare]
            if fn:
                return fn
            if pn:
                return f"+{pn}"
    elif jid_str.endswith("@lid"):
        bare = jid_str.split("@")[0]
        if bare in contacts_map:
            fn, pn = contacts_map[bare]
            if fn:
                return fn
            if pn:
                return f"+{pn}"
    if jid_str.endswith("@s.whatsapp.net"):
        num = jid_str.split("@")[0]
        return f"+{num}"
    if jid_str.endswith("@lid"):
        return "Unknown"
    if jid_str.isdigit():
        return f"+{jid_str}"
    return jid_str


def _format_ios_service_row(row: dict, contacts_map: dict) -> str:
    """Format an iOS service event row into a friendly human-readable string."""
    ev = row.get("group_event_type")
    txt = (row.get("text_body") or "").strip()
    member_jid = (row.get("member_jid") or "").strip()
    from_me = row.get("from_me", 0)
    sender = row.get("sender") or ""
    is_from_me = from_me == 1

    actor = None
    targets: list[str] = []

    if ev == 12:  # Group created
        subj = txt
        if txt.startswith("{"):
            try:
                data = json.loads(txt)
                subj = data.get("subject", "")
                author_jid = data.get("author")
                if author_jid:
                    actor = _resolve_ios_jid(author_jid, contacts_map)
            except (json.JSONDecodeError, AttributeError):
                pass
        if not actor:
            if from_me == 1:
                actor = "You"
            elif member_jid:
                actor = _resolve_ios_jid(member_jid, contacts_map)
            elif sender and sender != "Someone":
                actor = sender
            else:
                actor = "Someone"
        return format_ios_system_event(ev, actor, [], subj, is_from_me)

    if ev in (1, 3, 10):  # Subject, icon, or description changed
        if row.get("chat_type") == "contact":
            return "Messages and calls are end-to-end encrypted. No one outside of this chat, not even WhatsApp, can read or listen to them."
        actor = (
            "You"
            if from_me == 1
            else (
                _resolve_ios_jid(member_jid, contacts_map) if member_jid else (sender or "Someone")
            )
        )
        return format_ios_system_event(ev, actor, [], txt, is_from_me)

    if ev in (4, 15):  # Left or joined via invite link
        target = (
            _resolve_ios_jid(member_jid, contacts_map)
            if member_jid
            else ("You" if from_me == 1 else "A participant")
        )
        return format_ios_system_event(ev, sender or "Someone", [target], txt, is_from_me)

    if ev == 2:  # Participant added or joined / E2E notice for 1:1 contact chats
        if row.get("chat_type") == "contact" or (not member_jid and not txt):
            return "Messages and calls are end-to-end encrypted. No one outside of this chat, not even WhatsApp, can read or listen to them."
        if member_jid:
            actor = (
                "You" if from_me == 1 else (_resolve_ios_jid(txt, contacts_map) if txt else None)
            )
            target = _resolve_ios_jid(member_jid, contacts_map)
        elif from_me == 1:
            actor = "You"
            target = _resolve_ios_jid(txt, contacts_map) if txt else "someone"
        elif sender == "WhatsApp":
            actor = "WhatsApp"
            target = _resolve_ios_jid(txt, contacts_map) if txt else "someone"
        else:
            actor = _resolve_ios_jid(txt, contacts_map) if txt else None
            target = "you"
        return format_ios_system_event(ev, actor or "Someone", [target], txt, is_from_me)

    if ev in (21, 50):  # Multiple participants added or shared via contact
        if ";" in txt:
            actor_part, targets_part = txt.split(";", 1)
            actor = "You" if from_me == 1 else _resolve_ios_jid(actor_part.strip(), contacts_map)
            target_jids = [j.strip() for j in targets_part.split(",") if j.strip()]
        else:
            actor = "You" if from_me == 1 else None
            target_jids = [j.strip() for j in txt.split(",") if j.strip()]
        targets = [_resolve_ios_jid(j, contacts_map) for j in target_jids]
        return format_ios_system_event(ev, actor or "Someone", targets, txt, is_from_me)

    if ev == 7:  # Participant removed
        if member_jid:
            actor = (
                "You" if from_me == 1 else (_resolve_ios_jid(txt, contacts_map) if txt else None)
            )
            target = _resolve_ios_jid(member_jid, contacts_map)
        elif from_me == 1:
            actor = "You"
            target = _resolve_ios_jid(txt, contacts_map) if txt else "someone"
        elif sender == "WhatsApp":
            actor = "WhatsApp"
            target = _resolve_ios_jid(txt, contacts_map) if txt else "someone"
        else:
            actor = _resolve_ios_jid(txt, contacts_map) if txt else None
            target = "you"
        return format_ios_system_event(ev, actor or "Someone", [target], txt, is_from_me)

    if ev in (5, 9):  # Admin changed
        target = (
            _resolve_ios_jid(member_jid, contacts_map)
            if member_jid
            else (_resolve_ios_jid(txt, contacts_map) if txt else "You")
        )
        return format_ios_system_event(ev, sender or "Someone", [target], txt, is_from_me)

    if ev == 6:  # Phone number changed
        target_phone = _resolve_ios_jid(txt, contacts_map) if txt else ""
        return format_ios_system_event(ev, sender or "A contact", [], target_phone, is_from_me)

    if ev == 56:  # Security code changed
        contact = sender or (
            _resolve_ios_jid(member_jid, contacts_map) if member_jid else "this contact"
        )
        return format_ios_system_event(ev, contact, [], txt, is_from_me)

    if ev in (26, 36, 37, 38, 58):  # Settings, approvals, and announcements
        actor_name = (
            "You"
            if from_me == 1
            else (
                _resolve_ios_jid(member_jid, contacts_map) if member_jid else (sender or "Someone")
            )
        )
        return format_ios_system_event(ev, actor_name, [], txt, is_from_me)

    if txt and not txt.startswith("{") and "@lid" not in txt and not txt.isdigit():
        return txt
    return "Group event"


def _format_android_service_row(row: dict) -> str:
    """Format an Android service event row into a friendly human-readable string."""
    act = row.get("action_type")
    txt = (row.get("text_body") or "").strip()
    from_me = row.get("from_me")
    sender = row.get("sender")
    part_name = row.get("participant_name")
    targets = row.get("participant_names")
    if targets is None:
        targets = [part_name] if part_name else []

    # Guard: In a 1-to-1 chat, group actions must never emit group labels
    if row.get("chat_type") == "contact" and act in ALLOWED_GROUP_SYSTEM_ACTIONS:
        if act == 1:
            return "You deleted this message" if from_me == 1 else "This message was deleted"
        return "Messages and calls are end-to-end encrypted. No one outside of this chat, not even WhatsApp, can read or listen to them."

    actor = sender if sender else ("You" if from_me == 1 else "Someone")
    if not targets and act in (4, 5, 13, 79, 123):
        targets = [sender] if sender else (["You"] if from_me == 1 else [])

    metadata = row.get("service_metadata") or {}
    return format_android_system_event(act, actor, targets, txt, metadata)


def _hydrate_android_service_participants(
    conn: sqlite3.Connection,
    msg_ids: list,
    has_mcp: bool,
    has_lid_dn: bool,
    has_jid_server: bool,
    has_jid_raw_string: bool,
) -> dict[int, list[str]]:
    """Hydrate participant names list for Android service messages on demand."""
    if not has_mcp or not msg_ids:
        return {}
    ph = ",".join("?" * len(msg_ids))
    ldn_col = "ldn.display_name AS lid_name," if has_lid_dn else "NULL AS lid_name,"
    ldn_join = (
        "LEFT JOIN lid_display_name ldn ON ldn.lid_row_id = mcp.user_jid_row_id"
        if has_lid_dn
        else ""
    )
    server_col = "j_part_raw.server AS raw_server," if has_jid_server else "NULL AS raw_server,"
    raw_str_col = (
        "j_part_raw.raw_string AS raw_string," if has_jid_raw_string else "NULL AS raw_string,"
    )
    is_lid_pred = "j_part_raw.server = 'lid'" if has_jid_server else "0"

    sql = f"""
        SELECT
            mcp.message_row_id,
            j_part_raw.user AS raw_user,
            {server_col}
            {raw_str_col}
            j_part_real.user AS real_user,
            con_part.display_name AS contact_name,
            {ldn_col}
            con_part.number AS contact_num
        FROM message_system_chat_participant mcp
        LEFT JOIN _jid_map_resolved jm_part ON jm_part.lid_row_id = mcp.user_jid_row_id
        LEFT JOIN jid j_part_real ON j_part_real._id = jm_part.jid_row_id
        LEFT JOIN jid j_part_raw ON j_part_raw._id = mcp.user_jid_row_id
        {ldn_join}
        LEFT JOIN arch.contacts con_part ON con_part.number = COALESCE(j_part_real.user, CASE WHEN NOT ({is_lid_pred}) THEN j_part_raw.user END)
        WHERE mcp.message_row_id IN ({ph})
    """
    try:
        p_rows = conn.execute(sql, msg_ids).fetchall()
    except sqlite3.OperationalError:
        return {}

    p_map: dict[int, list[str]] = {}
    for pr in p_rows:
        mid = pr["message_row_id"]
        raw_str = pr["raw_string"] or ""
        raw_u = pr["raw_user"] or ""
        raw_s = pr["raw_server"] or ""
        real_u = pr["real_user"] or ""
        c_name = pr["contact_name"] or ""
        lid_n = pr["lid_name"] or ""

        if raw_str == "lid_me" or raw_u == "me":
            name = "You"
        elif c_name:
            name = c_name
        elif (real_u or (raw_s != "lid" and raw_u)) == "0":
            name = "WhatsApp"
        elif real_u:
            name = f"+{real_u}"
        elif lid_n:
            name = lid_n
        elif raw_s != "lid" and raw_u:
            name = f"+{raw_u}"
        else:
            name = "Unknown"

        p_map.setdefault(mid, []).append(name)

    return p_map


def _format_service_rows(
    rows: list,
    source_type: str,
    conn: sqlite3.Connection,
    has_mcp: bool,
    has_lid_dn: bool,
    has_jid_server: bool,
    has_jid_raw_string: bool,
) -> None:
    """Format in-place text_body for all service event rows in a result set."""
    service_rows = [r for r in rows if r.get("media_type") == "service"]
    if not service_rows:
        return

    if source_type == "ios":
        needed_jids = set()
        for r in service_rows:
            m_jid = (r.get("member_jid") or "").strip()
            if m_jid:
                needed_jids.add(m_jid)
                if not m_jid.endswith("@s.whatsapp.net") and not m_jid.endswith("@lid"):
                    needed_jids.add(f"{m_jid}@s.whatsapp.net")
                    needed_jids.add(f"{m_jid}@lid")
                elif m_jid.endswith("@s.whatsapp.net"):
                    needed_jids.add(m_jid.split("@")[0])
            txt = (r.get("text_body") or "").strip()
            if txt.startswith("{"):
                try:
                    data = json.loads(txt)
                    author = data.get("author")
                    if author:
                        needed_jids.add(author)
                        if not author.endswith("@s.whatsapp.net") and not author.endswith("@lid"):
                            needed_jids.add(f"{author}@s.whatsapp.net")
                            needed_jids.add(f"{author}@lid")
                        elif author.endswith("@s.whatsapp.net"):
                            needed_jids.add(author.split("@")[0])
                except (json.JSONDecodeError, AttributeError):
                    pass
            for part in re.split(r"[;,]", txt):
                part = part.strip()
                if part:
                    needed_jids.add(part)
                    if not part.endswith("@s.whatsapp.net") and not part.endswith("@lid"):
                        needed_jids.add(f"{part}@s.whatsapp.net")
                        needed_jids.add(f"{part}@lid")
                    elif part.endswith("@s.whatsapp.net"):
                        needed_jids.add(part.split("@")[0])

        contacts_map = {}
        if needed_jids:
            ph = ",".join("?" * len(needed_jids))
            try:
                c_rows = conn.execute(
                    f"SELECT jid, full_name, phone_number FROM _ios_contacts WHERE jid IN ({ph})",
                    list(needed_jids),
                ).fetchall()
                for cr in c_rows:
                    contacts_map[cr["jid"]] = (cr["full_name"], cr["phone_number"])
            except sqlite3.OperationalError:
                pass
            try:
                arch_rows = conn.execute(
                    f"SELECT number, display_name FROM arch.contacts WHERE number IN ({ph})",
                    list(needed_jids),
                ).fetchall()
                for ar in arch_rows:
                    if ar["display_name"]:
                        contacts_map[ar["number"]] = (ar["display_name"], ar["number"])
                        contacts_map[f"{ar['number']}@s.whatsapp.net"] = (
                            ar["display_name"],
                            ar["number"],
                        )
            except sqlite3.OperationalError:
                pass
            try:
                push_rows = conn.execute(
                    f"SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME WHERE ZJID IN ({ph})",
                    list(needed_jids),
                ).fetchall()
                for pr in push_rows:
                    if pr["ZPUSHNAME"]:
                        if pr["ZJID"] not in contacts_map:
                            contacts_map[pr["ZJID"]] = (pr["ZPUSHNAME"], None)
                        bare_p = pr["ZJID"].split("@")[0]
                        if bare_p not in contacts_map:
                            contacts_map[bare_p] = (pr["ZPUSHNAME"], None)
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
                contacts_map[u_jid] = ("You", u_phone)
                contacts_map[u_phone] = ("You", u_phone)
                contacts_map[f"+{u_phone}"] = ("You", u_phone)
        except sqlite3.OperationalError:
            pass

        for r in service_rows:
            r["text_body"] = _format_ios_service_row(r, contacts_map)

    elif source_type == "android":
        p_map = _hydrate_android_service_participants(
            conn,
            [r["msg_id"] for r in service_rows],
            has_mcp,
            has_lid_dn,
            has_jid_server,
            has_jid_raw_string,
        )
        meta_map: dict[int, dict[str, str]] = {}
        m_ids = [r["msg_id"] for r in service_rows if r.get("msg_id")]
        if m_ids:
            ph = ",".join("?" * len(m_ids))
            try:
                eph_rows = conn.execute(
                    f"SELECT message_row_id, setting_duration FROM message_ephemeral_setting WHERE message_row_id IN ({ph})",
                    m_ids,
                ).fetchall()
                for er in eph_rows:
                    meta_map.setdefault(er[0], {})["setting_duration"] = str(er[1])
            except sqlite3.OperationalError:
                pass
            try:
                priv_rows = conn.execute(
                    f"SELECT message_row_id, verified_biz_name FROM message_system_initial_privacy_provider WHERE message_row_id IN ({ph}) AND verified_biz_name IS NOT NULL",
                    m_ids,
                ).fetchall()
                for pr in priv_rows:
                    if pr[1]:
                        meta_map.setdefault(pr[0], {})["verified_biz_name"] = str(pr[1])
            except sqlite3.OperationalError:
                pass

        for r in service_rows:
            names = p_map.get(r["msg_id"], [])
            if not r.get("participant_names"):
                r["participant_names"] = names
            if not r.get("participant_name"):
                r["participant_name"] = format_participant_names(names) if names else ""
            if r.get("msg_id") in meta_map:
                r.setdefault("service_metadata", {}).update(meta_map[r["msg_id"]])
            act = r.get("service_action_type") or r.get("action_type")
            if r.get("chat_type") == "contact" and act == 1:
                r["media_type"] = "deleted"
                r["text_body"] = (
                    "You deleted this message"
                    if r.get("from_me") == 1
                    else "This message was deleted"
                )
            else:
                r["text_body"] = _format_android_service_row(r)
                if r.get("chat_type") == "contact" and r["text_body"] in (
                    "You deleted this message",
                    "This message was deleted",
                ):
                    r["media_type"] = "deleted"


def _ios_reactions(wa_conn: sqlite3.Connection, chat_id: str, msg_ids: list) -> dict:
    """Batch-fetch iOS reactions from ZRECEIPTINFO for a list of message IDs.

    Returns a dict: {msg_id: [(sender_hex, emoji_hex), ...]}.
    Reactions from the current user are annotated with from_me=1 so the frontend can
    place them on the correct corner. "Me" is resolved from sent messages in this chat.
    """
    if not msg_ids:
        return {}

    placeholders = ",".join("?" * len(msg_ids))

    # Resolve "me" from a sent message in this chat so we can annotate reactions.
    # In group chats ZGROUPMEMBER.ZMEMBERJID is populated; in 1-to-1 chats we fall
    # back to ZWAMESSAGE.ZFROMJID.
    my_row = wa_conn.execute(
        """
        SELECT COALESCE(gm.ZMEMBERJID, m.ZFROMJID) AS me_jid
        FROM ZWAMESSAGE m
        LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
        WHERE m.ZCHATSESSION = CAST(? AS INTEGER)
          AND m.ZISFROMME = 1
        LIMIT 1
    """,
        (chat_id,),
    ).fetchone()

    my_phone = None
    if my_row and my_row["me_jid"]:
        jid = my_row["me_jid"]
        at = jid.find("@")
        if at > 0:
            my_phone = jid[:at]

    rows = wa_conn.execute(
        f"""
        SELECT m.Z_PK AS msg_id,
               mi.ZRECEIPTINFO AS receipt_info
        FROM ZWAMESSAGE m
        LEFT JOIN ZWAMESSAGEINFO mi ON mi.Z_PK = m.ZMESSAGEINFO
        WHERE m.Z_PK IN ({placeholders})
          AND mi.ZRECEIPTINFO IS NOT NULL
    """,
        msg_ids,
    ).fetchall()

    if not rows:
        return {}

    reactions_by_msg = {}
    for row in rows:
        raw = row["receipt_info"]
        if not raw:
            continue
        reactors = _extract_ios_reactions(bytes(raw))
        if not reactors:
            continue
        annotated = []
        for sender, emoji in reactors:
            from_me = 0
            if sender and my_phone:
                if sender == my_phone:
                    from_me = 1
            annotated.append((sender, emoji, from_me))
        reactions_by_msg[row["msg_id"]] = annotated

    return reactions_by_msg


def _resolve_and_cache_reactions(
    source_type: str, wa_conn: sqlite3.Connection, archive_conn: sqlite3.Connection, rows: list
) -> None:
    if source_type != "android" or not rows:
        return

    msg_ids = [r["msg_id"] for r in rows if r.get("msg_id") is not None]
    if not msg_ids:
        return

    placeholders = ",".join("?" * len(msg_ids))
    reaction_rows = wa_conn.execute(
        f"""
        SELECT ao.parent_message_row_id AS msg_id,
               GROUP_CONCAT(r.reaction) AS reactions
        FROM message_add_on ao
        JOIN message_add_on_reaction r ON r.message_add_on_row_id = ao._id
        WHERE ao.parent_message_row_id IN ({placeholders})
        GROUP BY ao.parent_message_row_id
    """,
        msg_ids,
    ).fetchall()

    reaction_map = {row["msg_id"]: row["reactions"] for row in reaction_rows}

    for row in rows:
        rx = reaction_map.get(row["msg_id"])
        row["reactions"] = rx

    cache_sql = """
        INSERT OR REPLACE INTO reactions_cache (chat_id, msg_id, reactions)
        VALUES (?, ?, ?)
    """
    archive_conn.executemany(
        cache_sql,
        [
            (row.get("chat_id", ""), row["msg_id"], reaction_map.get(row["msg_id"], ""))
            for row in rows
            if row.get("msg_id") is not None
        ],
    )
    archive_conn.commit()


def _hydrate_poll_rows(rows: list, source_type: str, wa_conn: sqlite3.Connection) -> None:
    """Hydrate poll structured data for poll messages in rows."""
    if not rows or wa_conn is None:
        return

    if source_type == "android":
        tables = {
            r[0]
            for r in wa_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        candidate_mids = [
            r["msg_id"] for r in rows if r.get("media_type") != "poll" and r.get("msg_id")
        ]
        if candidate_mids:
            c_ph = ",".join("?" * len(candidate_mids))
            db_poll_mids = set()
            if "message_poll" in tables:
                try:
                    db_poll_mids.update(
                        r[0]
                        for r in wa_conn.execute(
                            f"SELECT message_row_id FROM message_poll WHERE message_row_id IN ({c_ph})",
                            candidate_mids,
                        ).fetchall()
                    )
                except sqlite3.OperationalError:
                    pass
            if "message_add_on" in tables:
                mao_cols = {
                    r[1] for r in wa_conn.execute("PRAGMA table_info(message_add_on)").fetchall()
                }
                if "message_add_on_type" in mao_cols:
                    try:
                        db_poll_mids.update(
                            r[0]
                            for r in wa_conn.execute(
                                f"SELECT parent_message_row_id FROM message_add_on WHERE parent_message_row_id IN ({c_ph}) AND message_add_on_type = 67",
                                candidate_mids,
                            ).fetchall()
                        )
                    except sqlite3.OperationalError:
                        pass
            for r in rows:
                if r.get("msg_id") in db_poll_mids:
                    r["media_type"] = "poll"

    poll_rows = [r for r in rows if r.get("media_type") == "poll"]
    if not poll_rows:
        return

    if source_type == "android":
        tables = {
            r[0]
            for r in wa_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        mids = [r["msg_id"] for r in poll_rows]
        ph = ",".join("?" * len(mids))

        meta_map = {}
        if "message_poll" in tables:
            try:
                meta_rows = wa_conn.execute(
                    f"SELECT message_row_id, selectable_options_count FROM message_poll WHERE message_row_id IN ({ph})",
                    mids,
                ).fetchall()
                meta_map = {r["message_row_id"]: r["selectable_options_count"] for r in meta_rows}
            except sqlite3.OperationalError:
                pass

        opt_rows = []
        if "message_poll_option" in tables:
            try:
                opt_rows = wa_conn.execute(
                    f"SELECT _id, message_row_id, option_name, vote_total FROM message_poll_option WHERE message_row_id IN ({ph}) ORDER BY _id ASC",
                    mids,
                ).fetchall()
            except sqlite3.OperationalError:
                pass

        user_voted_options = set()
        voter_counts = {}
        addon_votes_by_opt = {}
        if "message_add_on" in tables:
            mao_cols = {
                r[1] for r in wa_conn.execute("PRAGMA table_info(message_add_on)").fetchall()
            }
            if "message_add_on_type" in mao_cols:
                try:
                    vc_rows = wa_conn.execute(
                        f"""
                        SELECT parent_message_row_id, COUNT(DISTINCT _id) AS voter_cnt
                        FROM message_add_on
                        WHERE parent_message_row_id IN ({ph}) AND message_add_on_type = 67
                        GROUP BY parent_message_row_id
                        """,
                        mids,
                    ).fetchall()
                    voter_counts = {r["parent_message_row_id"]: r["voter_cnt"] for r in vc_rows}
                except sqlite3.OperationalError:
                    pass

                if "message_add_on_poll_vote_selected_option" in tables:
                    try:
                        user_v_rows = wa_conn.execute(
                            f"""
                            SELECT ps.message_poll_option_id
                            FROM message_add_on mao
                            JOIN message_add_on_poll_vote_selected_option ps ON ps.message_add_on_row_id = mao._id
                            WHERE mao.parent_message_row_id IN ({ph}) AND mao.from_me = 1 AND mao.message_add_on_type = 67
                            """,
                            mids,
                        ).fetchall()
                        user_voted_options = {r[0] for r in user_v_rows}
                    except sqlite3.OperationalError:
                        pass

                    try:
                        opt_ids = [opt["_id"] for opt in opt_rows]
                        if opt_ids:
                            opt_ph = ",".join("?" * len(opt_ids))
                            v_cnt_rows = wa_conn.execute(
                                f"""
                                SELECT ps.message_poll_option_id, COUNT(DISTINCT ps.message_add_on_row_id)
                                FROM message_add_on_poll_vote_selected_option ps
                                WHERE ps.message_poll_option_id IN ({opt_ph})
                                GROUP BY ps.message_poll_option_id
                                """,
                                opt_ids,
                            ).fetchall()
                            addon_votes_by_opt = {r[0]: r[1] for r in v_cnt_rows}
                    except sqlite3.OperationalError:
                        pass

        options_by_mid = collections.defaultdict(list)
        for opt in opt_rows:
            v_cnt = max(opt["vote_total"] or 0, addon_votes_by_opt.get(opt["_id"], 0))
            options_by_mid[opt["message_row_id"]].append(
                {
                    "id": opt["_id"],
                    "name": opt["option_name"] or "",
                    "vote_count": v_cnt,
                    "user_selected": opt["_id"] in user_voted_options,
                }
            )

        for r in poll_rows:
            mid = r["msg_id"]
            opts = options_by_mid.get(mid, [])
            total_votes = sum(o["vote_count"] for o in opts)
            total_voters = voter_counts.get(mid, total_votes)
            question = r.get("text_body") or "Poll"
            r["text_body"] = question
            r["poll"] = {
                "question": question,
                "multiple_answers": (meta_map.get(mid, 0) == 0),
                "total_votes": total_votes,
                "total_voters": total_voters,
                "options": opts,
            }

    elif source_type == "ios":
        tables = {
            r[0]
            for r in wa_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "ZWAMESSAGEINFO" not in tables:
            return
        mids = [r["msg_id"] for r in poll_rows]
        ph = ",".join("?" * len(mids))
        info_rows = wa_conn.execute(
            f"SELECT ZMESSAGE, ZRECEIPTINFO FROM ZWAMESSAGEINFO WHERE ZMESSAGE IN ({ph}) AND ZRECEIPTINFO IS NOT NULL",
            mids,
        ).fetchall()
        info_map = {r["ZMESSAGE"]: r["ZRECEIPTINFO"] for r in info_rows}

        for r in poll_rows:
            mid = r["msg_id"]
            blob = info_map.get(mid)
            if not blob:
                r["poll"] = None
                continue
            parsed = _parse_ios_poll_blob(bytes(blob))
            if not parsed:
                r["poll"] = None
                continue
            votes = parsed["votes"]
            opts = []
            for idx, opt_name in enumerate(parsed["options"]):
                cnt = sum(1 for v in votes if idx in v["option_indices"])
                user_selected = any(
                    idx in v["option_indices"]
                    for v in votes
                    if not v["voter_jid"] or v["voter_jid"] == "You"
                )
                opts.append(
                    {
                        "id": idx,
                        "name": opt_name,
                        "vote_count": cnt,
                        "user_selected": user_selected,
                    }
                )
            total_votes = sum(o["vote_count"] for o in opts)
            r["text_body"] = parsed["question"]
            r["poll"] = {
                "question": parsed["question"],
                "multiple_answers": parsed["multiple_answers"],
                "total_votes": total_votes,
                "total_voters": len(votes),
                "options": opts,
            }


def _hydrate_quoted_rows(
    rows: list[dict],
    source_type: str,
    wa_conn: sqlite3.Connection | None,
) -> None:
    """Hydrate quoted message fields on iOS rows from embedded ZMETADATA blobs, and Android rows for quoted thumbnails."""
    if not rows:
        return

    if source_type == "android":
        for row in rows:
            qt = row.get("quoted_text")
            if qt and (
                qt.startswith("📷")
                or qt.startswith("🎥")
                or qt.startswith("Sticker")
                or qt.startswith("📄")
            ):
                row["quoted_has_thumb"] = 1

        need_thumb = [
            r
            for r in rows
            if (r.get("quoted_text") or r.get("quoted_msg_id")) and not r.get("quoted_has_thumb")
        ]
        if need_thumb and wa_conn is not None:
            mids = [r["msg_id"] for r in need_thumb if r.get("msg_id")]
            q_mids = [r["quoted_msg_id"] for r in need_thumb if r.get("quoted_msg_id")]
            has_thumb_mids = set()
            try:
                tables = {
                    r[0]
                    for r in wa_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if "message_quoted_media" in tables and mids:
                    ph = ",".join("?" * len(mids))
                    has_thumb_mids.update(
                        r[0]
                        for r in wa_conn.execute(
                            f"SELECT message_row_id FROM message_quoted_media WHERE message_row_id IN ({ph}) AND thumbnail IS NOT NULL",
                            mids,
                        ).fetchall()
                    )
                if "message_quoted_text" in tables and mids:
                    ph = ",".join("?" * len(mids))
                    has_thumb_mids.update(
                        r[0]
                        for r in wa_conn.execute(
                            f"SELECT message_row_id FROM message_quoted_text WHERE message_row_id IN ({ph}) AND thumbnail IS NOT NULL",
                            mids,
                        ).fetchall()
                    )
                if q_mids:
                    ph_q = ",".join("?" * len(q_mids))
                    if "message_thumbnail" in tables:
                        has_thumb_mids.update(
                            r[0]
                            for r in wa_conn.execute(
                                f"SELECT message_row_id FROM message_thumbnail WHERE message_row_id IN ({ph_q}) AND thumbnail IS NOT NULL",
                                q_mids,
                            ).fetchall()
                        )
                    if "message_quoted_media" in tables:
                        has_thumb_mids.update(
                            r[0]
                            for r in wa_conn.execute(
                                f"SELECT message_row_id FROM message_quoted_media WHERE message_row_id IN ({ph_q}) AND thumbnail IS NOT NULL",
                                q_mids,
                            ).fetchall()
                        )
                    if "message_quoted_text" in tables:
                        has_thumb_mids.update(
                            r[0]
                            for r in wa_conn.execute(
                                f"SELECT message_row_id FROM message_quoted_text WHERE message_row_id IN ({ph_q}) AND thumbnail IS NOT NULL",
                                q_mids,
                            ).fetchall()
                        )
                    if "media_hash_thumbnail" in tables and "message_media" in tables:
                        has_thumb_mids.update(
                            r[0]
                            for r in wa_conn.execute(
                                f"""SELECT mm.message_row_id FROM message_media mm
                                   JOIN media_hash_thumbnail mht ON mht.media_hash = mm.file_hash
                                   WHERE mm.message_row_id IN ({ph_q}) AND mht.thumbnail IS NOT NULL""",
                                q_mids,
                            ).fetchall()
                        )
                    if "message" in tables:
                        has_thumb_mids.update(
                            r[0]
                            for r in wa_conn.execute(
                                f"SELECT _id FROM message WHERE _id IN ({ph_q}) AND message_type IN (1, 3, 9, 20, 26, 42, 43)",
                                q_mids,
                            ).fetchall()
                        )
            except sqlite3.OperationalError:
                pass

            for row in need_thumb:
                if (
                    row.get("msg_id") in has_thumb_mids
                    or row.get("quoted_msg_id") in has_thumb_mids
                ):
                    row["quoted_has_thumb"] = 1
        return

    if source_type != "ios":
        return

    extracted_senders = {}
    for row in rows:
        if row.get("quoted_stanza_id"):
            continue
        raw_meta = row.get("_raw_metadata")
        if not raw_meta:
            continue
        q_info = _extract_ios_quoted_info(raw_meta)
        if q_info:
            row["quoted_stanza_id"] = q_info["quoted_stanza_id"]
            if not row.get("quoted_text"):
                row["quoted_text"] = q_info["quoted_text"]
            if q_info.get("quoted_has_thumb"):
                row["quoted_has_thumb"] = 1
            if row.get("media_type") != "link" and row.get("raw_msg_type") != 7:
                row["link_title"] = None
                row["link_url"] = None
                row["link_desc"] = None
                if row.get("media_type") == "text":
                    row["has_thumbnail"] = 0

            sender_jid = q_info.get("quoted_sender")
            if sender_jid and not row.get("quoted_sender"):
                extracted_senders[row["msg_id"]] = sender_jid
            elif not row.get("quoted_sender"):
                if row.get("from_me") == 1:
                    row["quoted_sender"] = "You"
                elif row.get("sender"):
                    row["quoted_sender"] = row["sender"]

    if extracted_senders and wa_conn is not None:
        jids = list(set(extracted_senders.values()))
        ph = ",".join("?" * len(jids))
        contact_map = {}
        try:
            c_rows = wa_conn.execute(
                f"SELECT jid, full_name, phone_number FROM _ios_contacts WHERE jid IN ({ph})",
                jids,
            ).fetchall()
            for cr in c_rows:
                name = cr["full_name"] or cr["phone_number"]
                if name:
                    contact_map[cr["jid"]] = name
        except sqlite3.OperationalError:
            pass

        for row in rows:
            mid = row.get("msg_id")
            if mid in extracted_senders:
                raw_s = extracted_senders[mid]
                if raw_s in contact_map:
                    row["quoted_sender"] = contact_map[raw_s]
                else:
                    phone = raw_s.split("@")[0] if "@" in raw_s else raw_s
                    row["quoted_sender"] = f"+{phone}" if phone.isdigit() else phone


def _hydrate_link_preview_rows(rows: list[dict], source_type: str) -> None:
    """Hydrate link preview fields across Android and iOS rows."""
    if not rows:
        return
    for row in rows:
        raw_meta = row.pop("_raw_metadata", None)
        if source_type == "ios" and raw_meta and not row.get("link_desc"):
            if (
                row.get("media_type") == "link"
                or row.get("raw_msg_type") == 7
                or row.get("link_url")
                or row.get("link_title")
            ):
                desc = _extract_ios_link_description(raw_meta)
                if desc:
                    row["link_desc"] = desc
        if (
            source_type == "ios"
            and raw_meta
            and row.get("media_type") == "link"
            and not row.get("has_thumbnail")
            and b"\xff\xd8\xff" in raw_meta
        ):
            row["has_thumbnail"] = 1
        if not row.get("link_url") and (row.get("link_title") or row.get("media_type") == "link"):
            m = re.search(r"(?:https?://|www\.)[^\s<>\"']+", row.get("text_body") or "")
            if m:
                extracted_url = m.group(0).rstrip(".,;:!?)'\"")
                if extracted_url.startswith("www."):
                    extracted_url = "https://" + extracted_url
                row["link_url"] = extracted_url


def _hydrate_live_photos(
    rows: list[dict],
    source_type: str,
    wa_conn: sqlite3.Connection | None,
) -> None:
    """Hydrate Live Photo (iOS) and Motion Photo (Android) pairings."""
    if not rows or wa_conn is None:
        return

    if source_type == "android":
        mids = [r["msg_id"] for r in rows if r.get("msg_id")]
        if not mids:
            return
        ph = ",".join("?" * len(mids))
        try:
            try:
                p_rows = wa_conn.execute(
                    f"""
                    SELECT ma.parent_message_row_id, mm.file_path, ac.archive_path
                    FROM message_association ma
                    LEFT JOIN message_media mm ON mm.message_row_id = ma.child_message_row_id
                    LEFT JOIN arch.archive_copies ac ON ac.original_path = mm.file_path
                    WHERE ma.association_type = 11
                      AND ma.parent_message_row_id IN ({ph})
                    """,
                    mids,
                ).fetchall()
            except sqlite3.OperationalError:
                p_rows = wa_conn.execute(
                    f"""
                    SELECT ma.parent_message_row_id, mm.file_path, NULL AS archive_path
                    FROM message_association ma
                    LEFT JOIN message_media mm ON mm.message_row_id = ma.child_message_row_id
                    WHERE ma.association_type = 11
                      AND ma.parent_message_row_id IN ({ph})
                    """,
                    mids,
                ).fetchall()

            parent_live_map = {}
            for pr in p_rows:
                path = pr[2] or pr[1]
                if path:
                    parent_live_map[pr[0]] = path

            c_rows = wa_conn.execute(
                f"""
                SELECT child_message_row_id
                FROM message_association
                WHERE association_type = 11
                  AND child_message_row_id IN ({ph})
                """,
                mids,
            ).fetchall()
            child_ids = {cr[0] for cr in c_rows}

            for r in rows:
                mid = r.get("msg_id")
                if mid in parent_live_map:
                    r["is_live_photo"] = 1
                    r["live_photo_path"] = parent_live_map[mid]
                if mid in child_ids:
                    r["is_live_photo_video"] = 1
        except sqlite3.OperationalError:
            pass

    elif source_type == "ios":
        stanzas = [r["stanza_id"] for r in rows if r.get("stanza_id")]
        if not stanzas:
            return
        ph = ",".join("?" * len(stanzas))
        try:
            try:
                p_rows = wa_conn.execute(
                    f"""
                    SELECT mpa.parent_stanza_id, mi.ZMEDIALOCALPATH, ac.archive_path
                    FROM ext.message_parent_association mpa
                    JOIN ZWAMESSAGE m_c ON m_c.ZSTANZAID = mpa.stanza_id
                    LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m_c.ZMEDIAITEM
                    LEFT JOIN arch.archive_copies ac ON ac.original_path = 'Message/' || mi.ZMEDIALOCALPATH
                    WHERE mpa.type = 12
                      AND mpa.parent_stanza_id IN ({ph})
                    """,
                    stanzas,
                ).fetchall()
            except sqlite3.OperationalError:
                p_rows = wa_conn.execute(
                    f"""
                    SELECT mpa.parent_stanza_id, mi.ZMEDIALOCALPATH, NULL AS archive_path
                    FROM ext.message_parent_association mpa
                    JOIN ZWAMESSAGE m_c ON m_c.ZSTANZAID = mpa.stanza_id
                    LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m_c.ZMEDIAITEM
                    WHERE mpa.type = 12
                      AND mpa.parent_stanza_id IN ({ph})
                    """,
                    stanzas,
                ).fetchall()

            parent_live_map = {}
            for pr in p_rows:
                path = pr[2] or (("Message/" + pr[1]) if pr[1] else None)
                if path:
                    parent_live_map[pr[0]] = path

            c_rows = wa_conn.execute(
                f"""
                SELECT stanza_id
                FROM ext.message_parent_association
                WHERE type = 12
                  AND stanza_id IN ({ph})
                """,
                stanzas,
            ).fetchall()
            child_stanzas = {cr[0] for cr in c_rows}

            for r in rows:
                sid = r.get("stanza_id")
                if sid in parent_live_map:
                    r["is_live_photo"] = 1
                    r["live_photo_path"] = parent_live_map[sid]
                if sid in child_stanzas:
                    r["is_live_photo_video"] = 1
        except sqlite3.OperationalError:
            pass


def _backfill_recent_messages(archive_target: sqlite3.Connection | Path, rows: list) -> None:
    if not rows or not archive_target:
        return
    # Only cache live messages, never synthetic preserved or call-event entries
    rows = [
        r
        for r in rows
        if not r.get("is_preserved")
        and r.get("media_type") != "call"
        and isinstance(r.get("msg_id"), int)
        and r["msg_id"] > 0
    ]
    if not rows:
        return
    conn = None
    should_close = False
    try:
        if isinstance(archive_target, (str, Path)):
            p = Path(archive_target)
            if not p.exists():
                return
            conn = sqlite3.connect(str(p), timeout=10.0)
            conn.row_factory = sqlite3.Row
            should_close = True
        else:
            conn = archive_target
        has_recent = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recent_messages'"
            ).fetchone()
        )
        if not has_recent:
            return
        cur_cols = {r[1] for r in conn.execute("PRAGMA table_info(recent_messages)").fetchall()}
        keys = rows[0].keys()
        col_specs: list[tuple[str, object]] = [
            ("chat_id", lambda r: r["chat_id"]),
            ("chat_type", lambda r: r["chat_type"]),
            ("msg_id", lambda r: r["msg_id"]),
        ]
        if "stanza_id" in cur_cols:
            col_specs.append(("stanza_id", lambda r: r.get("stanza_id")))
        col_specs.extend(
            [
                ("timestamp_ms", lambda r: r["timestamp_ms"]),
                ("sender", lambda r: r["sender"]),
                ("from_me", lambda r: r["from_me"]),
                ("archive_path", lambda r: r["archive_path"] if "archive_path" in keys else None),
                ("media_type", lambda r: r["media_type"]),
                ("media_name", lambda r: r["media_name"] if "media_name" in keys else None),
                ("text_body", lambda r: r["text_body"]),
                ("quoted_text", lambda r: r["quoted_text"] if "quoted_text" in keys else None),
                (
                    "quoted_sender",
                    lambda r: r["quoted_sender"] if "quoted_sender" in keys else None,
                ),
                ("quoted_ts", lambda r: r["quoted_ts"] if "quoted_ts" in keys else None),
            ]
        )
        has_qmid = "quoted_msg_id" in cur_cols
        has_lp = "link_title" in cur_cols and "has_thumbnail" in cur_cols
        if has_qmid or has_lp:
            col_specs.append(
                ("quoted_msg_id", lambda r: r["quoted_msg_id"] if "quoted_msg_id" in keys else None)
            )
            if "quoted_has_thumb" in cur_cols:
                col_specs.append(("quoted_has_thumb", lambda r: r.get("quoted_has_thumb", 0) or 0))
        col_specs.extend(
            [
                ("reactions", lambda r: r["reactions"] if "reactions" in keys else None),
                ("is_edited", lambda r: r["is_edited"] if "is_edited" in keys else 0),
                ("edited_ts", lambda r: r["edited_ts"] if "edited_ts" in keys else None),
                ("is_view_once", lambda r: r["is_view_once"] if "is_view_once" in keys else 0),
            ]
        )
        if has_lp:
            col_specs.extend(
                [
                    ("link_title", lambda r: r["link_title"] if "link_title" in keys else None),
                    ("link_desc", lambda r: r["link_desc"] if "link_desc" in keys else None),
                    ("link_url", lambda r: r["link_url"] if "link_url" in keys else None),
                    (
                        "link_preview_type",
                        lambda r: r["link_preview_type"] if "link_preview_type" in keys else 0,
                    ),
                    (
                        "has_thumbnail",
                        lambda r: r["has_thumbnail"] if "has_thumbnail" in keys else 0,
                    ),
                ]
            )
        if "is_starred" in cur_cols and "is_pinned" in cur_cols:
            col_specs.extend(
                [
                    ("is_starred", lambda r: r["is_starred"] if "is_starred" in keys else 0),
                    ("is_pinned", lambda r: r["is_pinned"] if "is_pinned" in keys else 0),
                ]
            )
        insert_cols = [c for c, _ in col_specs]
        update_cols = [c for c in insert_cols if c not in ("chat_id", "chat_type", "msg_id")]
        placeholders = ", ".join("?" for _ in insert_cols)
        updates = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
        sql = (
            f"INSERT INTO recent_messages ({', '.join(insert_cols)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(chat_id, chat_type, msg_id) DO UPDATE SET {updates}"
        )
        params = [tuple(fn(r) for _, fn in col_specs) for r in rows]
        with conn:
            conn.executemany(sql, params)
    finally:
        if should_close and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _media_type_from_path(path: str) -> str:
    if path is None:
        return "text"
    p = path.lower()
    if p.endswith(".vcf"):
        return "vcard"
    if (
        "sticker" in p
        or "stickers" in p
        or (os.path.basename(p).startswith("stk-") and (p.endswith(".webp") or p.endswith(".was")))
        or p.endswith(".webp")
        or p.endswith(".was")
    ):
        return "sticker"
    fname = os.path.basename(p)
    if (
        p.endswith(".gif")
        or "/animated gifs/" in p
        or "whatsapp animated gifs" in p
        or fname.startswith("gif-")
    ):
        return "gif"
    ext = os.path.splitext(p)[1]
    if ext in (".jpg", ".jpeg", ".png", ".heic", ".heif"):
        return "image"
    if ext in (".mp4", ".mov", ".avi", ".mkv", ".3gp"):
        return "video"
    if ext in (".mp3", ".ogg", ".aac", ".opus", ".m4a"):
        return "audio"
    return "document"


def _parse_ios_receipt_blob(blob: bytes, conn, is_group: bool = False, msg_ts_s: int = 0) -> list:
    """Decode iOS ZRECEIPTINFO into per-recipient delivered/read timestamps.

    Blob layout (validated against real DB + phone screenshots):
      top field 3 = base_ts (unix seconds = send time)
      top field 2 = repeated recipient entry
    Recipient entry:
      field 1  = LID bytes (value[1:] hex, strip a trailing 'f')
      field 5  = read delta in seconds (present => read at base + field5)
      field 10 = repeated delivered event {1: delta_seconds}; delivered = base + min(deltas)
      field 9  = repeated event {1: delta_seconds}; delivered fallback when field 10 absent
                 (compact format: no base_ts, no field 5 => delivered-only, read blank)
      field 4  = unrelated large value; NOT a delivered/read gate => ignored
    """

    def _read_receipt_varint(data, pos):
        result = 0
        shift = 0
        while pos < len(data):
            b = data[pos]
            pos += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        return result, pos

    def _event_delta(value):
        """Return sub-field 1 (delta in seconds) of a field-9/field-10 event, or None."""
        pos = 0
        delta = None
        while pos < len(value):
            tag_byte, pos = _read_receipt_varint(value, pos)
            field = tag_byte >> 3
            wire = tag_byte & 0x07
            if wire == 0:
                v, pos = _read_receipt_varint(value, pos)
                if field == 1:
                    delta = v
            elif wire == 2:
                length, pos = _read_receipt_varint(value, pos)
                pos += length
            elif wire == 1:
                pos += 8
            elif wire == 5:
                pos += 4
            else:
                break
        return delta

    def _parse_member_entry(data):
        """Return (lid, delivered_delta, read_delta). Deltas are seconds; None when absent."""
        pos = 0
        lid = ""
        read_delta = None
        delivered_deltas = []
        fallback_deltas = []
        while pos < len(data):
            tag_byte, pos = _read_receipt_varint(data, pos)
            field = tag_byte >> 3
            wire = tag_byte & 0x07
            if wire == 2:
                length, pos = _read_receipt_varint(data, pos)
                value = data[pos : pos + length]
                pos += length
                if field == 1:
                    if len(value) > 1:
                        s = value[1:].hex()
                        lid = s[:-1] if s.endswith("f") else s
                elif field == 10:
                    d = _event_delta(value)
                    if d is not None:
                        delivered_deltas.append(d)
                elif field == 9:
                    d = _event_delta(value)
                    if d is not None:
                        fallback_deltas.append(d)
            elif wire == 0:
                value, pos = _read_receipt_varint(data, pos)
                if field == 5:
                    read_delta = value
            elif wire == 1:
                pos += 8
            elif wire == 5:
                pos += 4
            else:
                break
        if delivered_deltas:
            delivered_delta = min(delivered_deltas)
        elif fallback_deltas:
            delivered_delta = min(fallback_deltas)
        else:
            delivered_delta = None
        return lid, delivered_delta, read_delta

    base_ts = None
    entries = []
    pos = 0
    while pos < len(blob):
        try:
            tag_byte, pos = _read_receipt_varint(blob, pos)
        except Exception:
            break
        field = tag_byte >> 3
        wire = tag_byte & 0x07
        if wire == 2:
            try:
                length, pos = _read_receipt_varint(blob, pos)
            except Exception:
                break
            value = blob[pos : pos + length]
            pos += length
            if field == 2:
                entries.append(value)
        elif wire == 0:
            try:
                value, pos = _read_receipt_varint(blob, pos)
            except Exception:
                break
            if field == 3:
                base_ts = value
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        else:
            break

    anchor = base_ts if base_ts is not None else msg_ts_s
    if anchor is None or not entries:
        return []

    parsed = []
    for entry_bytes in entries:
        try:
            parsed.append(_parse_member_entry(entry_bytes))
        except Exception:
            continue

    if not is_group:
        delivered_deltas = [dd for _, dd, _ in parsed if dd is not None]
        read_deltas = [rd for _, _, rd in parsed if rd is not None]
        if not delivered_deltas and not read_deltas:
            return []
        return [
            {
                "name": "",
                "jid": "",
                "delivered_ts": (anchor + min(delivered_deltas)) * 1000
                if delivered_deltas
                else None,
                "read_ts": (anchor + min(read_deltas)) * 1000 if read_deltas else None,
                "played_ts": None,
            }
        ]

    has_phone_col = False
    try:
        ic_cols = {
            r["name"] if isinstance(r, sqlite3.Row) else r[1]
            for r in conn.execute("PRAGMA table_info(_ios_contacts)").fetchall()
        }
        has_phone_col = "phone_number" in ic_cols
    except Exception:
        pass
    phone_col_expr = ", phone_number" if has_phone_col else ", NULL AS phone_number"

    members = []
    for lid, delivered_delta, read_delta in parsed:
        name = lid
        if lid:
            row = conn.execute(
                f"SELECT full_name {phone_col_expr} FROM _ios_contacts WHERE jid = ?",
                (f"{lid}@lid",),
            ).fetchone()
            if row and row[0]:
                name = row[0]
            elif row and row[1]:
                c_row = conn.execute(
                    "SELECT display_name FROM arch.contacts WHERE number = ?", (row[1],)
                ).fetchone()
                if c_row and c_row[0]:
                    name = c_row[0]
                else:
                    name = f"+{row[1]}"
            else:
                row = conn.execute(
                    "SELECT display_name FROM arch.contacts WHERE number = ?", (lid,)
                ).fetchone()
                if row and row[0]:
                    name = row[0]
        members.append(
            {
                "name": name or lid or "",
                "jid": lid or "",
                "delivered_ts": (anchor + delivered_delta) * 1000
                if delivered_delta is not None
                else None,
                "read_ts": (anchor + read_delta) * 1000 if read_delta is not None else None,
                "played_ts": None,
            }
        )
    return members


def _ios_read_without_timestamp(msg_status: int, read_ts) -> bool:
    """iOS aggregate ZMESSAGESTATUS 8 = read (in groups WhatsApp only sets it once
    every recipient has read). Recent messages (~mid-2026 onward) keep this flag but
    no longer persist the per-recipient read timestamp. Return True when the message
    is confirmed read yet no timestamp is stored, so the UI can show that state
    instead of a blank."""
    return msg_status == 8 and read_ts is None


def _parse_ios_receipt_device_rows(infra_rows: list, conn, is_group: bool) -> list[dict]:
    """Group per-device receipt rows from infra.receipt_device by user_jid."""
    by_user: dict[str, dict] = {}
    for row in infra_rows:
        ujid = row["user_jid"]
        if not ujid:
            continue
        deliv = row["delivered_timestamp"]
        read = row["read_timestamp"]
        played = row["played_timestamp"]

        if ujid not in by_user:
            by_user[ujid] = {
                "delivered_sec": deliv,
                "read_sec": read,
                "played_sec": played,
            }
        else:
            entry = by_user[ujid]
            if deliv is not None:
                entry["delivered_sec"] = (
                    min(entry["delivered_sec"], deliv)
                    if entry["delivered_sec"] is not None
                    else deliv
                )
            if read is not None:
                entry["read_sec"] = (
                    min(entry["read_sec"], read) if entry["read_sec"] is not None else read
                )
            if played is not None:
                entry["played_sec"] = (
                    min(entry["played_sec"], played) if entry["played_sec"] is not None else played
                )

    if not is_group:
        delivered_secs = [
            d["delivered_sec"] for d in by_user.values() if d["delivered_sec"] is not None
        ]
        read_secs = [d["read_sec"] for d in by_user.values() if d["read_sec"] is not None]
        played_secs = [d["played_sec"] for d in by_user.values() if d["played_sec"] is not None]
        if not delivered_secs and not read_secs and not played_secs:
            return []
        deliv_ts = min(delivered_secs) * 1000 if delivered_secs else None
        read_ts = min(read_secs) * 1000 if read_secs else None
        played_ts = min(played_secs) * 1000 if played_secs else None
        return [
            {
                "name": "",
                "jid": "",
                "delivered_ts": deliv_ts,
                "read_ts": read_ts,
                "played_ts": played_ts,
                "read_known": read_ts is not None,
            }
        ]

    has_phone_col = False
    try:
        ic_cols = {
            r["name"] if isinstance(r, sqlite3.Row) else r[1]
            for r in conn.execute("PRAGMA table_info(_ios_contacts)").fetchall()
        }
        has_phone_col = "phone_number" in ic_cols
    except Exception:
        pass
    phone_col_expr = ", phone_number" if has_phone_col else ", NULL AS phone_number"

    members = []
    for ujid, data in by_user.items():
        name = None
        c_row = conn.execute(
            f"SELECT full_name {phone_col_expr} FROM _ios_contacts WHERE jid = ?", (ujid,)
        ).fetchone()
        if c_row and c_row[0]:
            name = c_row[0]
        elif c_row and c_row[1]:
            con_row = conn.execute(
                "SELECT display_name FROM arch.contacts WHERE number = ?", (c_row[1],)
            ).fetchone()
            if con_row and con_row[0]:
                name = con_row[0]
            else:
                name = f"+{c_row[1]}"
        else:
            prefix = ujid.split("@")[0]
            c_row2 = conn.execute(
                f"SELECT full_name {phone_col_expr} FROM _ios_contacts WHERE jid = ?", (prefix,)
            ).fetchone()
            if c_row2 and c_row2[0]:
                name = c_row2[0]
            elif c_row2 and c_row2[1]:
                con_row = conn.execute(
                    "SELECT display_name FROM arch.contacts WHERE number = ?", (c_row2[1],)
                ).fetchone()
                if con_row and con_row[0]:
                    name = con_row[0]
                else:
                    name = f"+{c_row2[1]}"
            else:
                p_row = conn.execute(
                    "SELECT ZPUSHNAME FROM ZWAPROFILEPUSHNAME WHERE ZJID = ?", (ujid,)
                ).fetchone()
                if p_row and p_row[0]:
                    name = p_row[0]
                else:
                    name = prefix

        jid_clean = ujid.split("@")[0]
        deliv_ts = data["delivered_sec"] * 1000 if data["delivered_sec"] is not None else None
        read_ts = data["read_sec"] * 1000 if data["read_sec"] is not None else None
        played_ts = data["played_sec"] * 1000 if data["played_sec"] is not None else None

        members.append(
            {
                "name": name or jid_clean or "",
                "jid": jid_clean,
                "delivered_ts": deliv_ts,
                "read_ts": read_ts,
                "played_ts": played_ts,
                "read_known": read_ts is not None,
            }
        )
    return members
