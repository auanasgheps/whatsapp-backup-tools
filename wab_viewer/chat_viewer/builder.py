"""Bundle assembler for wab_viewer modular JavaScript sources."""

from pathlib import Path

_CHAT_VIEWER_DIR = Path(__file__).resolve().parent
_JS_DIR = _CHAT_VIEWER_DIR / "js"

MODULE_ORDER = [
    "00_state.js",
    "01_utils_text.js",
    "02_utils_ui.js",
    "03_navigation.js",
    "04_message_window.js",
    "05_grouping.js",
    "06_scroll_engine.js",
    "07_bubble_render.js",
    "08_bubble_media.js",
    "09_player_video_notes.js",
    "10_player_voice_notes.js",
    "11_bubble_media_archived.js",
    "12_lightbox.js",
    "13_gallery_virtualization.js",
    "14_gallery_video_probe.js",
    "15_gallery_audio_and_cards.js",
    "16_gallery_render.js",
    "17_gallery_filters_and_stream.js",
    "18_gallery_archive.js",
    "19_search.js",
    "20_date_picker.js",
    "21_panel_chat_info.js",
    "22_panel_starred_chat.js",
    "23_panel_pinned.js",
    "24_panel_starred_sidebar.js",
    "25_calls.js",
    "26_jump_engine.js",
    "27_modal_message_details.js",
    "28_settings_and_indexing.js",
    "29_modal_map.js",
    "30_bootstrap.js",
]

_CACHED_BUNDLE: str | None = None


def assemble_app_js() -> str:
    """Concatenate modular JS files in exact order within the top-level IIFE."""
    parts = [
        "// WhatsApp Backup Tools - Web Viewer Application Bundle\n",
        "// Assembled in-memory from wab_viewer/chat_viewer/js/*.js.\n\n",
        "(function () {\n  'use strict';\n\n",
    ]
    for mod_name in MODULE_ORDER:
        file_path = _JS_DIR / mod_name
        if not file_path.is_file():
            raise FileNotFoundError(f"Missing required JS module: {mod_name}")
        content = file_path.read_text(encoding="utf-8").strip()
        parts.append(
            "  // ============================================================================\n"
        )
        parts.append(f"  // MODULE: {mod_name}\n")
        parts.append(
            "  // ============================================================================\n\n"
        )
        parts.append(content)
        parts.append("\n\n")
    parts.append("})();\n")
    return "".join(parts)


def get_bundle_content(debug: bool) -> str:
    """Return the assembled JavaScript bundle, caching in memory when not in debug mode."""
    global _CACHED_BUNDLE
    if debug or _CACHED_BUNDLE is None:
        _CACHED_BUNDLE = assemble_app_js()
    return _CACHED_BUNDLE
