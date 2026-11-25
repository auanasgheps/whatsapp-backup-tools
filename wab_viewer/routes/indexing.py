"""Background FTS5 indexing and user preference routes."""

from __future__ import annotations

import threading

from flask import Blueprint, Response, jsonify, request

from ..context import get_viewer_context
from ..db_cache import VALID_PREF_VALUES, get_cache_db_path
from ..fts import _clear_fts_index, bg_index_all
from ..locales_loader import SUPPORTED_LANGUAGES, load_locale

bp = Blueprint("indexing", __name__)


@bp.route("/api/index-status")
def api_index_status():
    ctx = get_viewer_context()
    cache = ctx.get_cache()
    indexed = cache.execute("SELECT COUNT(*) FROM message_index").fetchone()[0]
    return jsonify({"indexed": indexed})


@bp.route("/api/chat-index-status")
def api_chat_index_status():
    ctx = get_viewer_context()
    chat_id = request.args.get("chat_id", "")
    chat_type = request.args.get("chat_type", "")
    key = (chat_id, chat_type)
    with ctx.indexing_lock:
        if (
            ctx.get_cache()
            .execute("SELECT 1 FROM indexed_chats WHERE chat_id = ? AND chat_type = ?", key)
            .fetchone()
        ):
            return jsonify({"status": "done"})
        state = ctx.indexing_state.get(key, "idle")
        if state == "done":
            ctx.indexing_state.pop(key, None)
            state = "idle"
    return jsonify({"status": state})


@bp.route("/api/index/source-size")
def api_index_source_size():
    ctx = get_viewer_context()
    row = (
        ctx.get_cache().execute("SELECT value FROM sync_meta WHERE key = 'source_size'").fetchone()
    )
    size = int(row["value"]) if row and row["value"] else 0
    return jsonify({"bytes": size})


@bp.route("/api/index/progress")
def api_index_progress():
    ctx = get_viewer_context()
    with ctx.indexing_lock:
        state = dict(ctx.bulk_index_state)
    return jsonify(state)


@bp.route("/api/index/all", methods=["POST"])
def api_index_all():
    ctx = get_viewer_context()
    if ctx.source_type is None:
        return jsonify({"error": "no source database"}), 400
    with ctx.indexing_lock:
        if ctx.bulk_index_state["running"]:
            return jsonify({"error": "already running"})
        ctx.bulk_index_state["running"] = True
        ctx.bulk_index_state["indexed_msgs"] = 0
        ctx.bulk_index_state["total_msgs"] = 0
    threading.Thread(target=bg_index_all, args=(ctx,), daemon=True).start()
    return jsonify({"ok": True})


@bp.route("/api/index/clear", methods=["POST"])
def api_index_clear():
    ctx = get_viewer_context()
    with ctx.indexing_lock:
        if ctx.bulk_index_state["running"]:
            return jsonify({"error": "indexing in progress"}), 409
    _clear_fts_index(ctx.get_cache())
    ctx.get_cache().execute("VACUUM")
    return jsonify({"ok": True})


@bp.route("/api/index/optimize", methods=["POST"])
def api_index_optimize():
    ctx = get_viewer_context()
    with ctx.indexing_lock:
        if ctx.bulk_index_state["running"]:
            return jsonify({"error": "indexing in progress"}), 409
    cache_path = get_cache_db_path(ctx.output_root)
    wal_path = cache_path.with_name(cache_path.name + "-wal")

    def _calc_size():
        sz = cache_path.stat().st_size if cache_path.exists() else 0
        if wal_path.exists():
            sz += wal_path.stat().st_size
        return sz

    size_before = _calc_size()
    c = ctx.get_cache()
    c.execute("INSERT INTO message_index_fts(message_index_fts) VALUES('optimize')")
    c.commit()
    c.execute("VACUUM")
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.execute("PRAGMA optimize")
    size_after = _calc_size()
    return jsonify(
        {
            "ok": True,
            "size_before": size_before,
            "size_after": size_after,
            "reclaimed": max(0, size_before - size_after),
        }
    )


@bp.route("/api/preferences", methods=["GET"])
def api_preferences_get():
    ctx = get_viewer_context()
    rows = ctx.get_cache().execute("SELECT key, value FROM user_preferences").fetchall()
    return jsonify({r["key"]: r["value"] for r in rows})


@bp.route("/api/preferences", methods=["POST"])
def api_preferences_post():
    ctx = get_viewer_context()
    data = request.get_json(force=True)
    key = data.get("key", "")
    value = data.get("value", "")
    if key not in VALID_PREF_VALUES:
        return jsonify({"error": "unknown preference key"}), 400
    if value not in VALID_PREF_VALUES[key]:
        return jsonify({"error": f"invalid value for {key}"}), 400
    ctx.get_cache().execute(
        "INSERT OR REPLACE INTO user_preferences (key, value) VALUES (?, ?)",
        (key, value),
    )
    ctx.get_cache().commit()
    return jsonify({"ok": True})


@bp.route("/api/locales/<lang_code>", methods=["GET"])
def api_locale_get(lang_code: str):
    """Serve a UTF-8 locale dictionary JSON file by language code."""
    import json

    base = (lang_code or "").split("-")[0].lower()
    if base not in SUPPORTED_LANGUAGES:
        return jsonify({"error": f"unsupported language: {lang_code}"}), 404
    data = load_locale(base)
    return Response(
        json.dumps(data, ensure_ascii=False),
        mimetype="application/json; charset=utf-8",
    )
