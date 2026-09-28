import logging
import sqlite3
from pathlib import Path

import pytest

import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_script_path = Path(_ROOT) / "Extras" / "generate_demo_output.py"
_spec = importlib.util.spec_from_file_location("generate_demo_output", _script_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

generate_demo = _mod.generate_demo
configure_logger = _mod.configure_logger
from wab_viewer.main import create_app


@pytest.fixture
def demo_dir(tmp_path: Path) -> Path:
    logger = configure_logger("test_demo_output")
    out_dir = tmp_path / "Demo Output"
    generate_demo(out_dir, logger)
    return out_dir


def test_demo_databases_created(demo_dir: Path):
    wa_db = demo_dir / "Whatsapp Databases" / "msgstore.db"
    arch_db = demo_dir / ".wa_media_archiver.db"

    assert wa_db.exists()
    assert arch_db.exists()

    with sqlite3.connect(str(wa_db)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "message" in tables
        assert "chat" in tables
        assert "jid" in tables
        assert "message_media" in tables
        assert "message_quoted" in tables
        assert "message_add_on" in tables
        assert "message_add_on_reaction" in tables
        assert "message_system" in tables
        assert "group_participant_user" in tables

    with sqlite3.connect(str(arch_db)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "contacts" in tables
        assert "groups" in tables
        assert "files" in tables
        assert "archive_copies" in tables


def test_viewer_api_chats(demo_dir: Path):
    app = create_app(demo_dir, rescan=True)
    app.config["TESTING"] = True

    with app.test_client() as client:
        res = client.get("/api/chats")
        assert res.status_code == 200
        chats = res.get_json()
        assert len(chats) == 5

        chat_names = {c["display_name"] for c in chats}
        assert "Alpine Hikers" in chat_names
        assert "Tech & Coffee Meetup" in chat_names
        assert "Sarah Jenkins" in chat_names
        assert "Dr. Marcus Vance" in chat_names
        assert "WhatsApp" in chat_names


def test_viewer_chat_info_group(demo_dir: Path):
    app = create_app(demo_dir, rescan=True)
    app.config["TESTING"] = True

    with app.test_client() as client:
        res = client.get("/api/chat-info?chat_id=1&chat_type=group")
        assert res.status_code == 200
        info = res.get_json()

        assert info["display_name"] == "Alpine Hikers"
        assert "Weekend trail guides" in info["description"]
        member_names = {m["name"] for m in info["members"]}
        assert "Elena Rostova" in member_names
        assert "Lucas Silva" in member_names
        assert "Maya Lin" in member_names
        assert "You" in member_names


def test_viewer_messages_and_features(demo_dir: Path):
    app = create_app(demo_dir, rescan=True)
    app.config["TESTING"] = True

    with app.test_client() as client:
        # Group 1: Alpine Hikers
        res = client.get("/api/messages?chat_id=1&chat_type=group&limit=50")
        assert res.status_code == 200
        msgs = res.get_json()
        assert len(msgs) >= 10

        media_types = {m["media_type"] for m in msgs}
        assert "service" in media_types
        assert "text" in media_types
        assert "link" in media_types
        assert "image" in media_types

        # Check reactions
        has_reactions = any(m.get("reactions") for m in msgs)
        assert has_reactions

        # Check quotes
        has_quote = any(m.get("quoted_text") for m in msgs)
        assert has_quote

        # Chat 4: Dr. Marcus Vance (Document and Audio)
        res_vance = client.get("/api/messages?chat_id=15550102&chat_type=contact&limit=50")
        assert res_vance.status_code == 200
        vance_msgs = res_vance.get_json()
        vance_types = {m["media_type"] for m in vance_msgs}
        assert "document" in vance_types
        assert "audio" in vance_types


def test_viewer_media_serving(demo_dir: Path):
    app = create_app(demo_dir, rescan=True)
    app.config["TESTING"] = True

    with app.test_client() as client:
        # List media for Alpine Hikers
        res = client.get("/api/media?chat_id=1&chat_type=group&limit=50")
        assert res.status_code == 200
        media_list = res.get_json()
        assert len(media_list) >= 4

        # Serve first media
        first_rel = media_list[0]["archive_path"]
        serve_res = client.get(f"/media/{first_rel}")
        assert serve_res.status_code == 200
        assert len(serve_res.data) > 0

        # Range request support
        range_res = client.get(f"/media/{first_rel}", headers={"Range": "bytes=0-100"})
        assert range_res.status_code == 206
        assert len(range_res.data) == 101


def test_viewer_search(demo_dir: Path):
    app = create_app(demo_dir, rescan=True)
    app.config["TESTING"] = True

    with app.test_client() as client:
        # Load messages first to trigger indexing or trigger search
        client.get("/api/messages?chat_id=1&chat_type=group&limit=50")
        client.get("/api/messages?chat_id=15550101&chat_type=contact&limit=50")

        # Allow background indexing a moment if needed
        import time
        time.sleep(0.5)

        res = client.get("/api/search?q=summit")
        assert res.status_code == 200
        data = res.get_json()
        assert "results" in data
        assert len(data["results"]) >= 1


def test_viewer_readonly_database(demo_dir: Path):
    import stat
    wa_db = demo_dir / "Whatsapp Databases" / "msgstore.db"
    cache_db = demo_dir / ".wa_viewer.db"
    arch_db = demo_dir / ".wa_media_archiver.db"

    # Pre-create cache db
    app_pre = create_app(demo_dir, rescan=False)

    target_files = [wa_db, cache_db, arch_db]
    for base in [wa_db, cache_db, arch_db]:
        for p in (base.with_name(base.name + "-wal"), base.with_name(base.name + "-shm")):
            if p.exists():
                target_files.append(p)

    try:
        for p in target_files:
            if p.exists():
                p.chmod(stat.S_IREAD)

        app = create_app(demo_dir, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            res = client.get("/api/chats")
            assert res.status_code == 200
            chats = res.get_json()
            assert len(chats) == 5
    finally:
        for p in target_files:
            if p.exists():
                p.chmod(stat.S_IREAD | stat.S_IWRITE)


