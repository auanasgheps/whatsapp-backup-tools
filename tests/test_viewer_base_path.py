"""Serving the viewer under a URL prefix (--base-path), behind a reverse proxy."""

import re

import pytest
from tests.conftest import make_android_db, make_archive_db, seed_android_db
from wab_viewer import main as viewer
from wab_viewer.chat_viewer.builder import assemble_app_js


@pytest.fixture
def make_client(tmp_path):
    wa_conn = make_android_db(tmp_path / "msgstore.db")
    archive_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
    seed_android_db(wa_conn, archive_conn)
    wa_conn.close()
    archive_conn.close()
    (tmp_path / "Contacts").mkdir()
    (tmp_path / "Contacts" / "IMG.jpg").write_bytes(b"photo")

    def _make(base_path):
        app = viewer.create_app(tmp_path, rescan=False, base_path=base_path)
        app.config["TESTING"] = True
        return app.test_client()

    return _make


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("/", ""),
        (None, ""),
        ("whatsapp", "/whatsapp"),
        ("/whatsapp/", "/whatsapp"),
        ("/a/b", "/a/b"),
    ],
)
def test_normalize_base_path(raw, expected):
    assert viewer.normalize_base_path(raw) == expected


def test_page_and_routes_under_prefix(make_client):
    client = make_client("/whatsapp/")
    page = client.get("/whatsapp/")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'src="/whatsapp/static/app.js' in html
    assert 'href="/whatsapp/static/app.css' in html
    assert 'window.BASE_PATH = "/whatsapp"' in html
    assert client.get("/whatsapp/static/app.js").status_code == 200
    assert client.get("/whatsapp/api/chats").status_code == 200
    media = client.get("/whatsapp/media/Contacts/IMG.jpg")
    assert media.status_code == 200
    assert media.data == b"photo"


def test_prefix_without_trailing_slash(make_client):
    client = make_client("/whatsapp")
    assert client.get("/whatsapp").status_code == 200


def test_requests_without_prefix_still_work(make_client):
    # A proxy that strips the prefix (or a direct local connection) keeps working.
    client = make_client("/whatsapp")
    assert client.get("/api/chats").status_code == 200


def test_root_by_default(make_client):
    client = make_client("")
    html = client.get("/").get_data(as_text=True)
    assert 'src="/static/app.js' in html
    assert 'window.BASE_PATH = ""' in html


def test_app_js_bundle_has_no_bare_viewer_urls():
    # Every request to the viewer's own routes must go through API / MEDIA, or it breaks under a prefix.
    js = assemble_app_js()
    # A literal that starts with /api, /media or /static is fine only after "+ " (API + '/media/links').
    bare = re.findall(r"""(?<!\+ )['"`]/(?:api|media|static)\b""", js)
    assert bare == [], bare
