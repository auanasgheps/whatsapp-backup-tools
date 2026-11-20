"""Legacy SoftBank emoji (private use area) converted to Unicode in the viewer's JSON responses."""

import sqlite3

from tests.conftest import make_android_db, make_archive_db, seed_android_db
from wab_viewer import main as viewer
from wab_viewer.softbank import SOFTBANK_TO_UNICODE, convert, convert_obj


def test_convert_maps_softbank_codes():
    assert convert("ok \ue404\ue00e") == "ok \U0001f601\U0001f44d"
    assert convert("\ue022") == "❤️"  # red heart, with its variation selector


def test_convert_leaves_everything_else_alone():
    text = "Ciao 👋 àèì \uf8ff \ue600"  # modern emoji, accents, Apple logo, private use outside the table
    assert convert(text) == text


def test_table_covers_the_softbank_range_only():
    assert len(SOFTBANK_TO_UNICODE) == 470
    assert all(0xE001 <= code <= 0xE537 for code in SOFTBANK_TO_UNICODE)


def test_convert_obj_walks_nested_structures():
    data = {"a": ["\ue404", {"b": "\ue415"}], "n": 3, "t": ("\ue00e",), "none": None}
    assert convert_obj(data) == {
        "a": ["\U0001f601", {"b": "\U0001f604"}],
        "n": 3,
        "t": ["\U0001f44d"],
        "none": None,
    }


def test_api_returns_unicode_emoji(tmp_path):
    wa_conn = make_android_db(tmp_path / "msgstore.db")
    archive_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
    seed_android_db(wa_conn, archive_conn)
    wa_conn.execute("UPDATE message SET text_data = 'Ciao \ue404' WHERE text_data = 'Hello world'")
    wa_conn.commit()
    wa_conn.close()
    archive_conn.close()

    app = viewer.create_app(tmp_path, rescan=False)
    app.config["TESTING"] = True
    client = app.test_client()
    messages = client.get("/api/messages?chat_id=123456789&chat_type=contact").get_json()
    texts = [m.get("text_body") for m in messages]
    assert "Ciao \U0001f601" in texts
    assert not any("\ue404" in (t or "") for t in texts)

    # The database keeps the original characters: only the response is converted.
    with sqlite3.connect(tmp_path / "msgstore.db") as conn:
        assert (
            conn.execute("SELECT count(*) FROM message WHERE text_data = 'Ciao \ue404'").fetchone()[
                0
            ]
            == 1
        )
