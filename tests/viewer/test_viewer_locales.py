"""Tests for wab_viewer multi-language (i18n) locale catalogs, resolution, and system events."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from wab_viewer.db_cache import VALID_PREF_VALUES
from wab_viewer.locales_loader import (
    SUPPORTED_LANGUAGES,
    load_locale,
    resolve_lang_from_header,
    resolve_request_lang,
    server_t,
)
from wab_viewer.system_events import (
    IOSEventType,
    SystemActionType,
    build_android_system_event,
    build_ios_system_event,
)

_LOCALES_DIR = Path(__file__).resolve().parents[2] / "wab_viewer" / "locales"


def _flatten_keys(d: dict, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for k, v in d.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            keys |= _flatten_keys(v, full)
        else:
            keys.add(full)
    return keys


def test_all_locales_exist_valid_utf8_and_match_en_keys() -> None:
    en_data = json.loads((_LOCALES_DIR / "en.json").read_text(encoding="utf-8"))
    en_keys = _flatten_keys(en_data)
    assert len(en_keys) > 100

    for lang in SUPPORTED_LANGUAGES:
        path = _LOCALES_DIR / f"{lang}.json"
        assert path.is_file(), f"Missing locale file: {path}"
        raw = path.read_text(encoding="utf-8")
        assert "\ufffd" not in raw, f"Replacement character found in {lang}.json"
        data = json.loads(raw)
        assert data["_meta"]["code"] == lang
        lang_keys = _flatten_keys(data)
        missing = en_keys - lang_keys
        extra = lang_keys - en_keys
        assert not missing, f"{lang}.json missing keys: {sorted(missing)}"
        assert not extra, f"{lang}.json extra keys: {sorted(extra)}"


def test_european_diacritics_present_and_intact() -> None:
    de_raw = (_LOCALES_DIR / "de.json").read_text(encoding="utf-8")
    fr_raw = (_LOCALES_DIR / "fr.json").read_text(encoding="utf-8")
    it_raw = (_LOCALES_DIR / "it.json").read_text(encoding="utf-8")
    es_raw = (_LOCALES_DIR / "es.json").read_text(encoding="utf-8")

    for ch in ("ä", "ö", "ü", "ß"):
        assert ch in de_raw
    for ch in ("é", "è", "ê", "à", "ç"):
        assert ch in fr_raw
    for ch in ("à", "è", "ò", "ù"):
        assert ch in it_raw
    assert json.loads(json.dumps({"it": "àèéìíòóùú"}, ensure_ascii=False))["it"] == "àèéìíòóùú"
    for ch in ("á", "é", "í", "ó", "ú", "ñ", "¿", "¡"):
        assert ch in es_raw


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, "en"),
        ("", "en"),
        ("de-DE,de;q=0.9,en-US;q=0.8", "de"),
        ("de-AT", "de"),
        ("fr-CH, fr;q=0.9, en;q=0.8", "fr"),
        ("it-IT,it;q=0.9", "it"),
        ("es-419,es;q=0.9", "es"),
        ("ja-JP,zh-CN;q=0.9", "en"),
        ("pt-BR,de-DE;q=0.8,en;q=0.7", "de"),
    ],
)
def test_resolve_lang_from_header(header: str | None, expected: str) -> None:
    assert resolve_lang_from_header(header) == expected


def test_resolve_request_lang_prefers_explicit_user_pref() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE user_preferences (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO user_preferences (key, value) VALUES ('language', 'auto')")
    assert resolve_request_lang(conn, "fr-FR,en;q=0.8") == "fr"

    conn.execute("INSERT OR REPLACE INTO user_preferences (key, value) VALUES ('language', 'it')")
    assert resolve_request_lang(conn, "de-DE") == "it"


def test_server_t_interpolation_and_fallback() -> None:
    load_locale.cache_clear()
    assert server_t("de", "settings.index_large_confirm", mb=250).startswith(
        "Die Datenbank ist groß (250 MB)"
    )
    assert server_t("fr", "nonexistent.key") == "nonexistent.key"


def test_valid_pref_values_includes_supported_languages() -> None:
    assert VALID_PREF_VALUES["language"] == {"auto", "en", "de", "fr", "it", "es"}


def test_system_events_emit_structured_descriptors() -> None:
    text, desc = build_android_system_event(SystemActionType.ICON_CHANGED, "Alice", [], "", {})
    assert text == "Alice changed this group's icon"
    assert desc == {"key": "system.icon_changed", "params": {"actor": "Alice"}}

    text_ios, desc_ios = build_ios_system_event(
        IOSEventType.ADMIN_PROMOTED, "Someone", ["You"], "", False
    )
    assert text_ios == "You're now an admin"
    assert desc_ios == {"key": "system.admin_promoted_you", "params": {}}


def test_flask_ssr_browser_detection_and_locale_api(tmp_path: Path) -> None:
    import wab_viewer.main as viewer
    from tests.conftest import make_android_db, make_archive_db, seed_android_db

    wc = make_android_db(tmp_path / "msgstore.db")
    ac = make_archive_db(tmp_path / ".wa_media_archiver.db")
    seed_android_db(wc, ac)
    wc.close()
    ac.close()

    app = viewer.create_app(tmp_path, rescan=False)
    app.config["TESTING"] = True
    client = app.test_client()

    for hdr, expected in [
        ("de-DE,en;q=0.8", "de"),
        ("fr-CH", "fr"),
        ("it-IT", "it"),
        ("es-419", "es"),
        ("ja-JP", "en"),
    ]:
        resp = client.get("/", headers={"Accept-Language": hdr})
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert f'<html lang="{expected}"' in html
        assert f'window.INITIAL_LOCALE = "{expected}"' in html

    # Explicit preference overrides Accept-Language
    pref_resp = client.post("/api/preferences", json={"key": "language", "value": "de"})
    assert pref_resp.status_code == 200
    override_html = client.get("/", headers={"Accept-Language": "fr-FR"}).get_data(as_text=True)
    assert '<html lang="de"' in override_html

    # Locale API returns UTF-8 JSON with diacritics intact, 404 on unknown
    loc_resp = client.get("/api/locales/de")
    assert loc_resp.status_code == 200
    assert "Einstellungen" in loc_resp.get_data(as_text=True)
    assert client.get("/api/locales/xx").status_code == 404
