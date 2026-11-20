#!/usr/bin/env python3
"""
wab_viewer — Browse archived WhatsApp chats via a local Flask web UI.

Usage:
    python -m wab_viewer <output_root> [--port PORT] [--host HOST] [--no-browser] [--rescan] [--verbose]

Dependencies:
    pip install flask
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sqlite3
import sys
import threading
import webbrowser
from pathlib import Path

from shared.schema_validator import validate_viewer_schema

try:
    from flask import Flask
except ImportError:
    Flask = None

from .context import ViewerContext, get_version
from .db_cache import (
    _detect_source_db,
    _ensure_wa_indexes,
    _ensure_writable,
    _open_cache_db,
    _run_db_maintenance,
    _save_source_stamp,
    _source_changed,
    get_archive_db_path,
    open_and_migrate_archive_db,
)
from .fts import _clear_fts_index
from .preserved import _detect_preserved_db
from .queries.sql_builder import (
    build_viewer_queries,
    detect_schema_capabilities,
)
from .softbank import SoftbankEmojiJSONProvider

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args():
    p = argparse.ArgumentParser(description="Browse archived WhatsApp chats")
    p.add_argument(
        "output_root", nargs="?", default=None, help="Path to the archive output directory"
    )
    p.add_argument("--port", type=int, default=5000, help="Port to listen on (default: 5000)")
    p.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
    p.add_argument(
        "--no-browser", action="store_true", help="Do not open the browser automatically"
    )
    p.add_argument("--rescan", action="store_true", help="Force rebuild of the FTS index")
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose HTTP request logging (default: error only)",
    )
    p.add_argument(
        "--version", action="version", version=f"WhatsApp Backup Tools - Viewer v{get_version()}"
    )
    args = p.parse_args()
    if not args.output_root:
        p.error("output_root is required")
    return args


def resolve_browser_url(host: str, port: int) -> str:
    """Resolve the URL to open in the local browser.

    Wildcard bind addresses (0.0.0.0 and ::) bind to all network interfaces,
    which modern web browsers reject as non-routable destination addresses.
    They are replaced with loopback 127.0.0.1 for local browser launching.
    Specific hostnames and IP addresses are preserved verbatim.
    """
    browser_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    return f"http://{browser_host}:{port}"


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------


def create_app(output_root: Path, rescan: bool = False):
    if Flask is None:
        raise ImportError("Flask is not installed. Run: pip install flask")
    app = Flask(__name__)

    archive_db_path = get_archive_db_path(output_root)
    source_type, wa_db_path = _detect_source_db(output_root)
    if not wa_db_path:
        raise FileNotFoundError(
            f"WhatsApp database not found in {output_root} (msgstore.db or ChatStorage.sqlite)"
        )
    preserved_db_path = _detect_preserved_db(output_root)
    _ensure_writable(archive_db_path)
    _ensure_writable(Path(wa_db_path))
    cache_conn = _open_cache_db(output_root)
    _indexing_state: dict = {}
    _indexing_lock = threading.Lock()
    _bulk_index_state: dict = {"running": False, "indexed_msgs": 0, "total_msgs": 0}

    app._indexing_lock = _indexing_lock
    app._bulk_index_state = _bulk_index_state

    schema_alert = None
    source_is_changed = False
    has_prior_stamp = False
    with contextlib.closing(sqlite3.connect(str(wa_db_path))) as chk_conn:
        schema_res = validate_viewer_schema(chk_conn.cursor(), source_type, get_version())
        if not schema_res.is_valid:
            print(schema_res.banner)
            schema_alert = {
                "summary": schema_res.summary,
                "issue_url": schema_res.issue_url,
                "platform": source_type,
                "is_legacy": schema_res.is_legacy,
            }
            source_type = None

    app.config["SCHEMA_ALERT"] = schema_alert
    app.json = SoftbankEmojiJSONProvider(app)

    if source_type is None:
        print(
            "[wab_viewer] Notice: WhatsApp schema incompatibility detected. Chats cannot be loaded."
        )
    else:
        print(f"[wab_viewer] Source DB: {source_type} at {wa_db_path}")

        _ensure_writable(Path(wa_db_path))
        _ensure_wa_indexes(wa_db_path)

        has_prior_stamp = bool(
            cache_conn.execute("SELECT 1 FROM sync_meta WHERE key = 'source_mtime'").fetchone()
        )
        source_is_changed = bool(rescan or _source_changed(cache_conn, wa_db_path))
        if source_is_changed:
            _clear_fts_index(cache_conn)
            cache_conn.execute("VACUUM")
            _save_source_stamp(cache_conn, wa_db_path)
            print("[wab_viewer] FTS cache cleared — chats will be indexed on first open")
        else:
            count = cache_conn.execute("SELECT COUNT(*) FROM indexed_chats").fetchone()[0]
            print(f"[wab_viewer] {count} chat(s) already indexed")

    caps = detect_schema_capabilities(wa_db_path, source_type, output_root)
    queries = build_viewer_queries(caps, source_type)

    _archive_conn, _ = open_and_migrate_archive_db(
        archive_db_path, rescan, source_is_changed, has_prior_stamp
    )
    _run_db_maintenance(cache_conn, _archive_conn)

    ctx = ViewerContext(
        output_root=output_root,
        source_type=source_type,
        wa_db_path=wa_db_path,
        archive_db_path=archive_db_path,
        preserved_db_path=preserved_db_path,
        cache_conn=cache_conn,
        archive_conn=_archive_conn,
        indexing_state=_indexing_state,
        indexing_lock=_indexing_lock,
        bulk_index_state=_bulk_index_state,
        schema_alert=schema_alert,
        caps=caps,
        queries=queries,
    )
    app.extensions["viewer"] = ctx

    @app.teardown_appcontext
    def _close_wa_conn(exc):
        ctx.close_wa_conn(exc)

    from .routes import chats as chats_routes
    from .routes import collections as collections_routes
    from .routes import indexing as indexing_routes
    from .routes import media as media_routes
    from .routes import messages as messages_routes

    app.register_blueprint(chats_routes.bp)
    app.register_blueprint(collections_routes.bp)
    app.register_blueprint(indexing_routes.bp)
    app.register_blueprint(media_routes.bp)
    app.register_blueprint(messages_routes.bp)

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def validate_output_root(output_root: Path) -> None:
    hint = (
        "Run 'wab-archiver' first to create an archive, or pass a different path as the argument."
    )

    if not output_root.exists():
        print(f"Error: folder does not exist: {output_root}", file=sys.stderr)
        print(f"Hint:  {hint}", file=sys.stderr)
        sys.exit(1)

    if not output_root.is_dir():
        print(f"Error: path is not a directory: {output_root}", file=sys.stderr)
        print(f"Hint:  {hint}", file=sys.stderr)
        sys.exit(1)

    archive_db = get_archive_db_path(output_root)
    if not archive_db.exists():
        print(f"Error: archive database not found: {archive_db}", file=sys.stderr)
        print(f"Hint:  {hint}", file=sys.stderr)
        sys.exit(1)

    source_type, wa_db_path = _detect_source_db(output_root)
    preserved_db_path = _detect_preserved_db(output_root)
    if not wa_db_path and not preserved_db_path:
        print(f"Error: WhatsApp database not found in {output_root}", file=sys.stderr)
        print(
            "Hint:  Ensure 'msgstore.db' (Android), 'ChatStorage.sqlite' (iOS), or "
            "'preserved_messages.db' is present in the archive (e.g. in 'Whatsapp Databases' subfolder or root).",
            file=sys.stderr,
        )
        sys.exit(1)


def main():
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                with contextlib.suppress(Exception):
                    stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    print(f"=== WhatsApp Backup Tools - Viewer v{get_version()} ===")
    raw_path = args.output_root
    output_root = Path(raw_path).expanduser().resolve()

    validate_output_root(output_root)

    log_level = logging.INFO if args.verbose else logging.ERROR
    logging.getLogger("werkzeug").setLevel(log_level)

    print(f"[wab_viewer] Opening archive: {output_root}")

    app = create_app(output_root, rescan=args.rescan)

    url = f"http://{args.host}:{args.port}"
    print(f"[wab_viewer] Starting server at {url}")
    print("[wab_viewer] Press Ctrl+C to stop")

    if not args.no_browser:
        browser_url = resolve_browser_url(args.host, args.port)
        webbrowser.open(browser_url)
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
