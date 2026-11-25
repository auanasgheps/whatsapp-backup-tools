"""Server-side locale dictionary loader and language resolution helpers."""

from __future__ import annotations

import json
import sqlite3
from functools import lru_cache
from pathlib import Path

SUPPORTED_LANGUAGES = ("en", "de", "fr", "it", "es")
DEFAULT_LANGUAGE = "en"
_LOCALES_DIR = Path(__file__).resolve().parent / "locales"


@lru_cache(maxsize=16)
def load_locale(lang_code: str) -> dict:
    """Load and cache a locale JSON dictionary as UTF-8, falling back to English."""
    normalized = (lang_code or "").split("-")[0].lower()
    if normalized not in SUPPORTED_LANGUAGES:
        normalized = DEFAULT_LANGUAGE
    target = _LOCALES_DIR / f"{normalized}.json"
    if not target.is_file():
        target = _LOCALES_DIR / f"{DEFAULT_LANGUAGE}.json"
    return json.loads(target.read_text(encoding="utf-8"))


def resolve_lang_from_header(accept_language: str | None) -> str:
    """Resolve the best supported language from an HTTP Accept-Language header."""
    if not accept_language:
        return DEFAULT_LANGUAGE
    candidates: list[tuple[float, int, str]] = []
    for idx, part in enumerate(accept_language.split(",")):
        part = part.strip()
        if not part:
            continue
        pieces = part.split(";")
        tag = pieces[0].strip()
        q = 1.0
        for p in pieces[1:]:
            p = p.strip()
            if p.startswith("q="):
                try:
                    q = float(p[2:])
                except ValueError:
                    q = 0.0
        base = tag.split("-")[0].lower()
        if base in SUPPORTED_LANGUAGES and q > 0:
            candidates.append((-q, idx, base))
    if not candidates:
        return DEFAULT_LANGUAGE
    candidates.sort()
    return candidates[0][2]


def resolve_request_lang(cache_conn: sqlite3.Connection | None, accept_language: str | None) -> str:
    """Resolve the active language from user_preferences, then Accept-Language, then 'en'."""
    if cache_conn is not None:
        try:
            row = cache_conn.execute(
                "SELECT value FROM user_preferences WHERE key = 'language'"
            ).fetchone()
            if row and row["value"] and row["value"] != "auto":
                val = str(row["value"]).split("-")[0].lower()
                if val in SUPPORTED_LANGUAGES:
                    return val
        except sqlite3.Error:
            pass
    return resolve_lang_from_header(accept_language)


def server_t(lang_code: str, key: str, **params: object) -> str:
    """Look up a dot-notation translation key with English fallback and {param} formatting."""
    data = load_locale(lang_code)
    val = _lookup_nested(data, key)
    if val is None and lang_code != DEFAULT_LANGUAGE:
        val = _lookup_nested(load_locale(DEFAULT_LANGUAGE), key)
    if not isinstance(val, str):
        return key
    if params:
        for k, v in params.items():
            val = val.replace(f"{{{k}}}", str(v))
    return val


def _lookup_nested(data: dict, key: str) -> object | None:
    cur: object = data
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur
