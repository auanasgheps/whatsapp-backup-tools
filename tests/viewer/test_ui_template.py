"""Tests for wab_viewer."""

from __future__ import annotations

import os

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

import os
import re
import sqlite3
from pathlib import Path

import pytest
from tests.conftest import (
    make_android_db,
    make_archive_db,
    make_ios_db,
    seed_android_db,
)
from tests.fixtures.protobuf_factory import (
    encode_varint,
    lid_field1_bytes,
    make_compact_receipt_blob,
    make_ios_reaction_blob,
    make_receipt_blob,
    make_zreceipt_blob_with_reactions,
)
from tests.viewer.conftest import (
    _get_app_css,
    _get_app_js,
)
from wab_viewer import main as viewer
from wab_viewer.chat_viewer.template import HTML_TEMPLATE as VIEWER_HTML_TEMPLATE
from wab_viewer.softbank import SOFTBANK_TO_UNICODE, convert, convert_obj

# Protobuf wire builder aliases for compatibility
_encode_varint = encode_varint
_lid_field1_bytes = lid_field1_bytes
_make_receipt_blob = make_receipt_blob
_make_compact_receipt_blob = make_compact_receipt_blob
_make_zreceipt_blob_with_reactions = make_zreceipt_blob_with_reactions
_make_ios_reaction_blob = make_ios_reaction_blob


class TestHtmlTemplate:
    def test_app_js_bundle_assembles_cleanly(self):
        """Guarantee that builder.assemble_app_js() cleanly concatenates all 31 modules."""
        from wab_viewer.chat_viewer import builder

        js = builder.assemble_app_js()
        assert js.startswith("// WhatsApp Backup Tools - Web Viewer Application Bundle")
        assert js.strip().endswith("})();")
        assert len(js) > 300000

    def test_app_css_bundle_assembles_cleanly(self):
        """Guarantee that builder.assemble_app_css() cleanly concatenates all 19 modules."""
        from wab_viewer.chat_viewer import builder

        css = builder.assemble_app_css()
        assert css.startswith("/* WhatsApp Backup Tools - Web Viewer Stylesheet Bundle */")
        assert len(css) > 100000
        for mod in builder.CSS_MODULE_ORDER:
            assert f"/* MODULE: {mod} */" in css

    def test_css_modules_integrity_and_line_limits(self):
        """Ensure all CSS modules exist, match CSS_MODULE_ORDER, and remain under 600 lines."""
        from wab_viewer.chat_viewer import builder

        css_dir = builder._CSS_DIR
        actual_files = sorted(f.name for f in css_dir.glob("*.css"))
        expected_files = sorted(builder.CSS_MODULE_ORDER)
        assert actual_files == expected_files, (
            f"CSS directory mismatch. In dir: {actual_files}, In CSS_MODULE_ORDER: {expected_files}"
        )
        for mod in builder.CSS_MODULE_ORDER:
            fpath = css_dir / mod
            line_count = len(fpath.read_text(encoding="utf-8").splitlines())
            assert line_count <= 600, f"Module {mod} exceeds 600-line budget: {line_count} lines"

    def test_all_getElementById_targets_exist_before_script(self):
        """Every getElementById('id') in app.js must refer to an element defined
        in the HTML before the external script tag.  A missing or late-placed
        element causes a TypeError that silently kills the entire IIFE."""
        template = VIEWER_HTML_TEMPLATE

        script_start = template.index("<script>")
        html_before_script = template[:script_start]

        script_body = _get_app_js()

        ids_in_html = set(re.findall(r'\bid=["\']([^"\']+)["\']', html_before_script))
        ids_accessed = set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", script_body))

        # ids created dynamically at runtime (not in static HTML) are expected
        dynamic_ids = {"img-lightbox"}
        missing = ids_accessed - ids_in_html - dynamic_ids
        assert not missing, (
            f"getElementById called for IDs not present in HTML before <script>: {sorted(missing)}"
        )

    def test_load_gallery_page_does_not_push_lightbox_items(self):
        """renderGalleryItem owns lightboxItems.push — _loadGalleryPage must not do it
        too or every item ends up double-counted, breaking lightbox indices."""
        script = _get_app_js()

        # isolate the _loadGalleryPage function body
        fn_start = script.index("async function _loadGalleryPage(")
        # find the end: next top-level 'async function' or plain 'function' at col 2
        import re as _re

        next_fn = _re.search(r"\n  (async )?function ", script[fn_start + 1 :])
        fn_body = script[fn_start : fn_start + 1 + (next_fn.start() if next_fn else len(script))]

        assert "lightboxItems.push" not in fn_body, (
            "_loadGalleryPage must not push to lightboxItems — renderGalleryItem handles that"
        )

    def test_msg_media_aspect_ratio_preservation_css(self):
        """Orientation-locked media boxes must scale with Display scale tokens, clamp aspect ratios,
        keep inline video/stickers uncropped, and contain captions/quotes within media width."""
        css = _get_app_css()
        tpl_path = Path(_ROOT) / "wab_viewer" / "chat_viewer" / "template.py"
        tpl = tpl_path.read_text(encoding="utf-8")

        assert "Display scale" in tpl, "Settings modal must label the scale control 'Display scale'"

        rule_match = re.search(r"\.msg-media\s+img[^{]*\{([^}]+)\}", css)
        assert rule_match, ".msg-media img rule missing in app.css"
        rule_body = rule_match.group(1)
        assert "width: auto" in rule_body, (
            "Fallback media rule must use width: auto to avoid unguarded horizontal stretch"
        )
        assert "object-fit: contain" in rule_body, (
            "Fallback media rule must use object-fit: contain"
        )

        wrap_match = re.search(r"(?<!-)\.msg-media\s*\{([^}]+)\}", css)
        assert wrap_match, ".msg-media rule missing in app.css"
        wrap_body = wrap_match.group(1)
        assert "max-width: var(--media-w-land)" in wrap_body, (
            "Media container must clamp max-width to var(--media-w-land)"
        )
        assert "width: fit-content" in wrap_body, (
            "Media container must use width: fit-content to prevent empty wasted space"
        )

        assert ".msg-media.is-landscape" in css and "width: var(--media-w-land)" in css
        assert ".msg-media.is-portrait" in css and "width: var(--media-w-port)" in css
        assert "object-fit: cover;" in css
        assert "contain: inline-size;" in css, (
            "Captions and quotes in media bubbles must use contain: inline-size"
        )

    def test_lightbox_wheel_zoom_implemented(self):
        """app.js must provide lightbox wheel-to-zoom and pan functionality."""
        script = _get_app_js()

        assert "_setupLightboxZoom" in script, "_setupLightboxZoom helper must be present in app.js"
        assert "lb.addEventListener('wheel'" in script, "Lightbox wheel listener must be registered"
        assert "scale" in script and "panX" in script, "Zoom scale and pan state must be tracked"
        assert "lb-zoom-badge" in script, "Zoom reset indicator badge must be supported"

    def test_lightbox_smooth_transition_implemented(self):
        """Lightbox must provide persistent stage transitions, directional motion, and preloading."""
        script = _get_app_js()
        css = _get_app_css()

        assert "lb-stage" in script, "Lightbox must use .lb-stage container for media transitions"
        assert "_transitionLightboxMedia" in script, "Smooth transition coordinator must be present"
        assert "_preloadAdjacentLightboxMedia" in script, (
            "Adjacent media preloading must be present"
        )
        assert ".lb-stage" in css, ".lb-stage CSS rule must be defined"
        assert ".lb-media-entering" in css and ".lb-media-exiting" in css, (
            "Entering and exiting transition classes must be defined in app.css"
        )
        assert "prefers-reduced-motion" in css, "Reduced motion must be supported for transitions"

    def test_lightbox_transition_safety_and_missing_skip(self):
        """Lightbox transition coordinator must cancel fallback timer and skip missing media."""
        script = _get_app_js()

        assert "clearTimeout(finalizeTimer)" in script, (
            "Finalize timer must be cleared on transitionend to prevent duplicate execution"
        )
        assert "clearTimeout(cleanTimer)" in script, (
            "Outgoing clean timer must be cleared on transitionend to prevent duplicate execution"
        )
        assert "item.is_missing" in script, "Lightbox navigation must skip media flagged as missing"
        assert "msg.is_missing = true" in script, (
            "buildMissingGalleryCard must mark message as missing"
        )
        assert "stage.clientWidth" in script, (
            "clampPan must use stage bounding dimensions rather than pure window dimensions"
        )


class TestReactionsFrontendLayout:
    def test_reactions_css_corner_alignment(self):
        """Reactions must be anchored on the receiver's corner:
        sent (from-me) -> right, recv (from-them) -> left."""
        css = _get_app_css()

        sent_match = re.search(r"\.msg-row\.sent\s+\.msg-reactions\s*\{([^}]+)\}", css)
        assert sent_match, ".msg-row.sent .msg-reactions rule missing in app.css"
        assert "right:" in sent_match.group(1), ".msg-row.sent .msg-reactions must align right"

        recv_match = re.search(r"\.msg-row\.recv\s+\.msg-reactions\s*\{([^}]+)\}", css)
        assert recv_match, ".msg-row.recv .msg-reactions rule missing in app.css"
        assert "left:" in recv_match.group(1), ".msg-row.recv .msg-reactions must align left"

    def test_reactions_css_no_split_badge(self):
        """app.css must not contain opposite-corner split rules (rx-badge-2)."""
        css = _get_app_css()
        assert "rx-badge-2" not in css, "Obsolete rx-badge-2 rule still found in app.css"

    def test_reactions_js_no_split_logic(self):
        """app.js must not split reactions into rx-badge-2."""
        js = _get_app_js()
        assert "rx-badge-2" not in js, "Obsolete rx-badge-2 logic still found in app.js"

    def test_reactions_js_marks_has_reactions(self):
        """app.js must add 'has-reactions' class to message rows with reactions."""
        js = _get_app_js()
        assert "has-reactions" in js

    def test_gallery_month_headers_synced_on_open_and_load(self):
        """openMediaGallery and _loadGalleryPage must synchronize month headers."""
        js = _get_app_js()
        assert "_syncMonthHeaders();\n    _syncGalleryNotices();\n  }" in js
        assert (
            "_appendToArchiveView(items, activeArchiveDir());\n      }\n      _syncMonthHeaders();"
            in js
        )

    def test_gallery_month_headers_chronological_insertion(self):
        """Gallery grid items and month headers must be inserted in strictly descending
        chronological order without stale global month state."""
        js = _get_app_js()
        assert "function _insertGalleryGridItem(grid, msg)" in js
        assert "galleryLastMonthKey" not in js, (
            "Stale galleryLastMonthKey state must not exist in app.js"
        )
        assert "monthHdr.dataset.month = String(mk);" in js
        assert "cell.dataset.ts = String(msg.timestamp_ms" in js

    def test_gallery_link_only_chat_initial_load_and_caching(self):
        """When opening a chat without images or videos, the gallery must switch to first available type (e.g. links)
        without getting stuck on checking media or dropping link loading."""
        js = _get_app_js()
        assert "targetAvailable === 'link'" in js
        assert "_loadMoreLinks();" in js
        assert "gallery-checking-loading" in js
        assert "gallerySentinel && gallerySentinel.parentNode === grid" in js
        assert "cachedHasImagesOrVideos" in js


class TestTrack2UIShell:
    @pytest.fixture
    def app_and_tmp(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client, tmp_path

    def test_header_top_right_settings_and_platform_badge(self, app_and_tmp):
        client, tmp_path = app_and_tmp
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'id="header"' in html
        assert 'id="header-brand"' in html
        assert 'id="settings-btn"' in html
        assert "platform-badge" in html
        assert "Android" in html

    def test_sidebar_search_and_bottom_navbar(self, app_and_tmp):
        client, tmp_path = app_and_tmp
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'id="sidebar-bottom-bar"' in html
        assert 'id="tab-btn-chats"' in html
        assert 'id="tab-btn-calls"' in html
        assert 'id="tab-btn-starred"' in html
        assert 'id="tab-btn-archived"' in html
        assert 'data-tab="chats"' in html
        assert 'aria-selected="true"' in html

    def test_contact_header_avatar_and_subtitles(self, app_and_tmp):
        client, tmp_path = app_and_tmp
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'id="chat-header-info"' in html
        assert 'id="chat-header-avatar"' in html
        assert 'id="chat-title"' in html
        assert 'id="chat-subtitle"' in html

    def test_msg_bubble_elevation_and_tokens(self, app_and_tmp):
        client, tmp_path = app_and_tmp
        resp = client.get("/static/app.css")
        assert resp.status_code == 200
        css = resp.get_data(as_text=True)
        assert "--bubble-shadow" in css
        assert "box-shadow: var(--bubble-shadow);" in css
        assert "border: none;" in css
        assert "#chat-header-info" in css
        assert "#sidebar-bottom-bar" in css

    def test_chat_list_avatar_styling_and_script(self, app_and_tmp):
        client, tmp_path = app_and_tmp
        resp_css = client.get("/static/app.css")
        assert resp_css.status_code == 200
        css = resp_css.get_data(as_text=True)
        assert ".chat-item-avatar" in css
        assert ".chat-item-body" in css

        resp_js = client.get("/static/app.js")
        assert resp_js.status_code == 200
        js = resp_js.get_data(as_text=True)
        assert "chat-item-avatar" in js
        assert "chat-item-body" in js


class TestTrack3FeaturePanes:
    """Tests for Track 3: Interactive feature panes and mockup fidelity."""

    @pytest.fixture
    def app_and_tmp(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)

        wa_conn.execute("ALTER TABLE message ADD COLUMN starred INTEGER DEFAULT 0")
        wa_conn.execute("UPDATE message SET starred = 1 WHERE _id = 1")
        wa_conn.commit()

        # Create preserved_messages.db in tmp_path
        pres_db = tmp_path / "preserved_messages.db"
        pres_conn = sqlite3.connect(str(pres_db))
        pres_conn.execute("""
            CREATE TABLE preserved_messages (
                rowid INTEGER PRIMARY KEY,
                msg_id INTEGER,
                chat_id TEXT NOT NULL,
                chat_type TEXT NOT NULL,
                from_me INTEGER NOT NULL,
                key_id TEXT NOT NULL,
                sender_jid TEXT,
                sender_name TEXT,
                timestamp_ms INTEGER NOT NULL,
                text_body TEXT,
                media_type TEXT,
                archive_path TEXT,
                file_size INTEGER,
                is_starred INTEGER DEFAULT 0,
                is_pinned INTEGER DEFAULT 0
            );
        """)
        pres_conn.execute("""
            INSERT INTO preserved_messages (rowid, msg_id, chat_id, chat_type, from_me, key_id, sender_jid, sender_name, timestamp_ms, text_body, media_type, archive_path, file_size, is_starred, is_pinned)
            VALUES (
                50, 50, '10', 'contact', 0, 'PRES1', '15551234567@s.whatsapp.net', 'Alice',
                1700000050000, 'Preserved secret', NULL, NULL, 0, 1, 0
            );
        """)
        pres_conn.commit()

        wa_conn.close()
        archive_conn.close()
        pres_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client, tmp_path

    def test_contact_header_no_role_button_and_css_reset(self, app_and_tmp):
        """Contact header must not have role='button' (which triggers Pico blue button styling) and has transparent background."""
        client, _ = app_and_tmp
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        # Verify #chat-header-info is clean without role="button"
        assert '<div id="chat-header-info" tabindex="0"' in html
        assert 'id="chat-header-info" role="button"' not in html

        css_resp = client.get("/static/app.css")
        assert css_resp.status_code == 200
        css = css_resp.get_data(as_text=True)
        assert "#chat-header-info" in css
        assert "background: transparent !important;" in css

    def test_right_slide_over_drawers_inside_chat_pane(self, app_and_tmp):
        """Drawers must be docked inside #chat-pane with right-slide-over layout matching prototype mockup."""
        client, _ = app_and_tmp
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert '<aside id="chat-starred-drawer" class="chat-drawer"' in html
        assert '<aside id="chat-info-panel" class="chat-drawer"' in html
        # Both drawers must be within #chat-pane (preceding </div> of #chat-pane)
        pane_idx = html.find('id="chat-pane"')
        starred_idx = html.find('id="chat-starred-drawer"')
        info_idx = html.find('id="chat-info-panel"')
        assert pane_idx != -1 and starred_idx > pane_idx and info_idx > pane_idx

        css_resp = client.get("/static/app.css")
        assert css_resp.status_code == 200
        css = css_resp.get_data(as_text=True)
        assert ".chat-drawer" in css
        assert "position: absolute;" in css
        assert "right: 0;" in css
        assert "width: 340px;" in css

    def test_starred_messages_flyout_card_bubbles_and_tokens(self, app_and_tmp):
        """Starred flyout elements, cards, and design tokens must match prototype."""
        client, _ = app_and_tmp
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'id="chat-starred-btn"' in html
        assert 'id="chat-starred-badge"' in html
        assert 'id="chat-starred-header-count"' in html
        assert 'id="chat-starred-close"' in html

        css_resp = client.get("/static/app.css")
        assert css_resp.status_code == 200
        css = css_resp.get_data(as_text=True)
        assert ".starred-card" in css
        assert ".starred-card-top" in css
        assert ".starred-card-meta" in css
        assert ".starred-card-chevron" in css
        assert ".starred-card-snippet" in css
        assert ".chat-starred-subheader" in css

    def test_chat_info_hero_elements(self, app_and_tmp):
        """Chat info drawer hero elements must match mockup."""
        client, _ = app_and_tmp
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'id="chat-info-hero"' in html
        assert 'id="chat-info-avatar"' in html
        assert 'id="chat-info-title"' in html
        assert 'id="chat-info-phone"' in html
        assert 'id="chat-info-details"' in html
        assert 'id="chat-info-header-heading"' in html

        css_resp = client.get("/static/app.css")
        assert css_resp.status_code == 200
        css = css_resp.get_data(as_text=True)
        assert ".chat-info-hero" in css
        assert ".chat-info-avatar" in css
        assert ".chat-info-name" in css
        assert ".chat-info-phone" in css
        assert ".chat-info-loading" in css
        assert ".chat-info-spinner" in css

    def test_chat_menu_and_info_card_elements(self, app_and_tmp):
        """Chat 3-dot overflow menu and info card container elements must be present."""
        client, _ = app_and_tmp
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'id="chat-menu-container"' in html
        assert 'id="chat-menu-btn"' in html
        assert 'id="chat-menu-dropdown"' in html
        assert 'id="chat-starred-btn"' in html
        assert 'id="chat-info-btn"' in html
        assert 'id="chat-info-menu-label"' in html

        starred_idx = html.find('id="chat-starred-btn"')
        info_idx = html.find('id="chat-info-btn"')
        assert starred_idx < info_idx, "Starred messages must precede Info in the 3-dot menu"

        css_resp = client.get("/static/app.css")
        assert css_resp.status_code == 200
        css = css_resp.get_data(as_text=True)
        assert ".chat-menu-container" in css
        assert "#chat-menu-btn" in css
        assert ".chat-menu-dropdown" in css
        assert ".chat-menu-item" in css
        assert "#chat-starred-btn.has-starred" in css
        assert ".chat-info-card" in css
        assert ".chat-info-card-title" in css

        # Verify chat header stacking order exceeds pinned banner strip
        header_match = re.search(r"#chat-header\s*\{[^}]*z-index:\s*(\d+)", css)
        pinned_match = re.search(r"#pinned-banner\s*\{[^}]*z-index:\s*(\d+)", css)
        assert header_match and pinned_match, (
            "Both #chat-header and #pinned-banner must declare z-index"
        )
        assert int(header_match.group(1)) > int(pinned_match.group(1)), (
            "Chat header z-index must exceed pinned banner z-index so dropdown menu renders on top"
        )

        js_resp = client.get("/static/app.js")
        assert js_resp.status_code == 200
        js = js_resp.get_data(as_text=True)
        assert "openChatMenu" in js
        assert "closeChatMenu" in js
        assert "toggleChatMenu" in js
        assert "has-starred" in js

    def test_pulsing_glow_animations(self, app_and_tmp):
        """Pulsing gold and emerald glow keyframes must be present in app.css."""
        client, _ = app_and_tmp
        resp = client.get("/static/app.css")
        assert resp.status_code == 200
        css = resp.get_data(as_text=True)
        assert "@keyframes pulseGlowGold" in css
        assert "@keyframes pulseGlowEmerald" in css
        assert ".jump-highlight-gold" in css
        assert ".jump-highlight-emerald" in css

    def test_api_messages_at_resolution_by_msg_id(self, app_and_tmp):
        """api_messages_at resolves target message by msg_id directly without requiring ts."""
        client, _ = app_and_tmp
        # Query msg_id=1 (Android live message) with resolved chat_id=10
        resp = client.get("/api/messages/at?chat_id=10&chat_type=contact&msg_id=1")
        assert resp.status_code == 200
        msgs = resp.get_json()
        assert len(msgs) > 0
        assert any(m["msg_id"] == 1 for m in msgs)

    def test_api_messages_at_preserved_by_negative_msg_id(self, app_and_tmp):
        """api_messages_at resolves preserved message by negative msg_id."""
        client, _ = app_and_tmp
        resp = client.get("/api/messages/at?msg_id=-50")
        assert resp.status_code == 200
        msgs = resp.get_json()
        assert len(msgs) > 0
        assert any(m["msg_id"] == -50 for m in msgs)


class TestPhase3InBubbleIndicators:
    """Tests for Phase 3: In-Bubble Pinned & Starred Indicators."""

    @pytest.fixture
    def app_and_tmp(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)

        # Ensure message table has starred column
        msg_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(message)").fetchall()}
        if "starred" not in msg_cols:
            wa_conn.execute("ALTER TABLE message ADD COLUMN starred INTEGER DEFAULT 0")

        # Insert message 2; message 1 will be starred, message 2 will be pinned
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type, starred) "
            "VALUES (2, 10, 0, 1700000001000, 'Second message pinned', 0, 0)"
        )
        wa_conn.execute("UPDATE message SET starred = 1 WHERE _id = 1")

        # Create message_add_on and insert pin for message 2
        wa_conn.execute("""
            CREATE TABLE IF NOT EXISTS message_add_on (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_message_row_id INTEGER NOT NULL,
                message_add_on_type INTEGER NOT NULL,
                expiry_timestamp INTEGER,
                timestamp INTEGER
            )
        """)
        wa_conn.execute("""
            INSERT INTO message_add_on (parent_message_row_id, message_add_on_type, expiry_timestamp, timestamp)
            VALUES (2, 79, 9999999999999, 1700000001000)
        """)
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client, tmp_path

    def test_android_messages_projection_includes_starred_and_pinned(self, app_and_tmp):
        """GET /api/messages returns is_starred and is_pinned integer flags."""
        client, _ = app_and_tmp
        resp = client.get("/api/messages?chat_id=10&chat_type=contact")
        assert resp.status_code == 200
        msgs = resp.get_json()
        assert len(msgs) >= 2
        for m in msgs:
            assert "is_starred" in m
            assert "is_pinned" in m
        msg1 = next((m for m in msgs if m["msg_id"] == 1), None)
        assert msg1 is not None
        assert msg1["is_starred"] == 1
        assert msg1["is_pinned"] == 0
        msg2 = next((m for m in msgs if m["msg_id"] == 2), None)
        assert msg2 is not None
        assert msg2["is_pinned"] == 1
        assert msg2["is_starred"] == 0

    def test_messages_at_projection_includes_starred_and_pinned(self, app_and_tmp):
        """GET /api/messages/at returns is_starred and is_pinned."""
        client, _ = app_and_tmp
        resp = client.get("/api/messages/at?chat_id=10&chat_type=contact&ts=1700000000000")
        assert resp.status_code == 200
        msgs = resp.get_json()
        assert len(msgs) >= 1
        for m in msgs:
            assert "is_starred" in m
            assert "is_pinned" in m

    def test_preserved_messages_projection_includes_starred_and_pinned(self, tmp_path):
        """GET /api/messages correctly passes is_starred and is_pinned from preserved_messages.db."""
        wa_conn = make_android_db(tmp_path / "msgstore.db")
        wa_conn.close()

        pres_db = tmp_path / "preserved_messages.db"
        pres_conn = sqlite3.connect(str(pres_db))
        pres_conn.execute("""
            CREATE TABLE preserved_messages (
                rowid INTEGER PRIMARY KEY,
                msg_id INTEGER,
                chat_id TEXT NOT NULL,
                chat_type TEXT NOT NULL,
                from_me INTEGER NOT NULL,
                key_id TEXT NOT NULL,
                sender_jid TEXT,
                sender_name TEXT,
                timestamp_ms INTEGER NOT NULL,
                text_body TEXT,
                media_type TEXT,
                archive_path TEXT,
                file_size INTEGER,
                is_starred INTEGER DEFAULT 0,
                is_pinned INTEGER DEFAULT 0
            );
        """)
        pres_conn.execute(
            """INSERT INTO preserved_messages (
                rowid, msg_id, chat_id, chat_type, from_me, key_id,
                sender_name, timestamp_ms, text_body, media_type, is_starred, is_pinned
            ) VALUES (
                1, 101, 'test_chat', 'contact', 0, 'st_pres_1',
                'Alice', 1700000005000, 'Preserved starred pin', 'text', 1, 1
            )"""
        )
        pres_conn.commit()
        pres_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=test_chat&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 1
            assert msgs[0]["is_starred"] == 1
            assert msgs[0]["is_pinned"] == 1
            assert msgs[0]["is_preserved"] == 1

    def test_js_in_bubble_indicator_markup(self, app_and_tmp):
        """Verify helper functions and classes for in-bubble indicators in app.js."""
        client, _ = app_and_tmp
        resp = client.get("/static/app.js")
        assert resp.status_code == 200
        js = resp.get_data(as_text=True)
        assert "function createMetaPinIcon()" in js
        assert "function createMetaStarIcon()" in js
        assert "msg-pinned-icon" in js
        assert "msg-starred-icon" in js
        assert "msg-meta-icon" in js
        assert "Pinned message" in js
        assert "Starred message" in js

    def test_android_pinned_unpinned_state_exclusion(self, tmp_path):
        """Android message with pin_in_chat_state = 2 (unpinned) must have is_pinned = 0."""
        wa_conn = make_android_db(tmp_path / "msgstore.db")
        archive_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        seed_android_db(wa_conn, archive_conn)

        wa_conn.execute("""
            CREATE TABLE message_add_on (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_message_row_id INTEGER NOT NULL,
                message_add_on_type INTEGER NOT NULL,
                expiry_timestamp INTEGER,
                timestamp INTEGER
            );
        """)
        wa_conn.execute("""
            CREATE TABLE message_add_on_pin_in_chat (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_add_on_row_id INTEGER NOT NULL,
                pin_in_chat_state INTEGER NOT NULL
            );
        """)
        wa_conn.execute(
            "INSERT INTO message_add_on (_id, parent_message_row_id, message_add_on_type) VALUES (1, 1, 79)"
        )
        wa_conn.execute(
            "INSERT INTO message_add_on_pin_in_chat (message_add_on_row_id, pin_in_chat_state) VALUES (1, 2)"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=10&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) >= 1
            msg1 = next(m for m in msgs if m["msg_id"] == 1)
            assert msg1["is_pinned"] == 0

    def test_historical_backup_pin_not_expired(self, tmp_path):
        """Pinned messages in older backups must not be expired by host computer clock."""
        wa_conn = make_android_db(tmp_path / "msgstore.db")
        archive_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        seed_android_db(wa_conn, archive_conn)

        wa_conn.execute("""
            CREATE TABLE message_add_on (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_message_row_id INTEGER NOT NULL,
                message_add_on_type INTEGER NOT NULL,
                expiry_timestamp INTEGER,
                timestamp INTEGER
            );
        """)
        wa_conn.execute("""
            INSERT INTO message_add_on (_id, parent_message_row_id, message_add_on_type, expiry_timestamp, timestamp)
            VALUES (1, 1, 79, 1580000000000, 1570000000000)
        """)
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=10&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) >= 1
            msg1 = next(m for m in msgs if m["msg_id"] == 1)
            assert msg1["is_pinned"] == 1

    def test_ios_messages_starred_and_pinned(self, tmp_path):
        """GET /api/messages on iOS projects is_starred and is_pinned."""
        conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        conn.execute("ALTER TABLE ZWAMESSAGE ADD COLUMN ZSTARRED INTEGER DEFAULT 0")
        conn.execute("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (1, '123456789@s.whatsapp.net', 'Alice', 0)
        """)
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTARRED)
            VALUES (1, 1, 0, 720000000, 'Starred msg', 0, 1)
        """)
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTARRED)
            VALUES (2, 1, 0, 720000010, 'Pinned target', 0, 0)
        """)
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZMESSAGETYPE, ZPARENTMESSAGE)
            VALUES (3, 1, 0, 720000020, 66, 2)
        """)
        conn.commit()
        conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) >= 2
            msg1 = next(m for m in msgs if m["msg_id"] == 1)
            assert msg1["is_starred"] == 1
            assert msg1["is_pinned"] == 0

            msg2 = next(m for m in msgs if m["msg_id"] == 2)
            assert msg2["is_starred"] == 0
            assert msg2["is_pinned"] == 1

    def test_ios_ext_chat_pinned_messages(self, tmp_path):
        """GET /api/messages on iOS projects is_pinned when pinned in ExtChatDatabase.sqlite."""
        conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        conn.execute("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (1, '123456789@s.whatsapp.net', 'Alice', 0)
        """)
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID)
            VALUES (10, 1, 0, 720000000, 'Ext pinned msg', 0, 'ext_stanza_pin_1')
        """)
        conn.commit()
        conn.close()

        ext_db = tmp_path / "ExtChatDatabase.sqlite"
        ext_conn = sqlite3.connect(str(ext_db))
        ext_conn.executescript("""
            CREATE TABLE thread_messages (
                rowid INTEGER PRIMARY KEY,
                chat_jid TEXT NOT NULL,
                key_id TEXT NOT NULL,
                pin_timestamp INTEGER,
                thread_type INTEGER
            );
            INSERT INTO thread_messages (rowid, chat_jid, key_id, pin_timestamp, thread_type)
            VALUES (1, '123456789@s.whatsapp.net', 'ext_stanza_pin_1', 1700000000, 0);
        """)
        ext_conn.commit()
        ext_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 1
            assert msgs[0]["msg_id"] == 10
            assert msgs[0]["is_pinned"] == 1

    def test_context_jump_banner_reset_helper(self, app_and_tmp):
        """Verify showContextJumpBanner helper resets banner icon and text and preserves active nav tab."""
        client, _ = app_and_tmp
        resp = client.get("/static/app.js")
        assert resp.status_code == 200
        js = resp.get_data(as_text=True)
        assert "function showContextJumpBanner(icon, text, onSwitch)" in js
        assert "showContextJumpBanner('⭐', 'Viewing message in context'" in js
        assert "showContextJumpBanner('📞', `Viewing Call Moment in context" in js
        assert "if (currentNavTab === 'chats')" in js
        assert "el.dataset.msgId = String(m.id);" in js
        # openChatAtMessage must not prematurely reset filter/render chats while in Starred tab
        fn_start = js.index("async function openChatAtMessage(")
        fn_end = js.index("async function openChatAtTimestamp(")
        open_msg_body = js[fn_start:fn_end]
        banner_idx = open_msg_body.index("showContextJumpBanner(")
        assert "resetFilterTo('all')" not in open_msg_body[:banner_idx]

    def test_css_indicator_styles(self, app_and_tmp):
        """Verify CSS classes for meta indicator icons in app.css."""
        client, _ = app_and_tmp
        resp = client.get("/static/app.css")
        assert resp.status_code == 200
        css = resp.get_data(as_text=True)
        assert ".msg-meta-icon" in css
        assert ".msg-pinned-icon" in css
        assert ".msg-starred-icon" in css
        assert "var(--star-color" in css


class TestChatJumpHudPlacement:
    """Tests for chat jump HUD positioning aware of header height and pinned banners."""

    @pytest.fixture
    def app_client(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client

    def test_jump_hud_css_rules_and_transition(self, app_client):
        """CSS must define variable-backed top offset and smooth vertical transition."""
        resp = app_client.get("/static/app.css")
        assert resp.status_code == 200
        css = resp.get_data(as_text=True)

        assert ".chat-jump-hud" in css
        assert "top: var(--jump-hud-top, 72px);" in css
        assert "transition: opacity 0.2s ease, transform 0.2s ease;" in css
        assert "z-index: 25;" in css

    def test_jump_hud_dom_and_js_engine(self, app_client):
        """DOM and app.js must wire dynamic chat space offset calculation."""
        resp = app_client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)

        assert 'id="chat-jump-hud"' in html
        assert 'id="pinned-banner"' in html
        assert 'id="message-scroll"' in html

        js_resp = app_client.get("/static/app.js")
        assert js_resp.status_code == 200
        js = js_resp.get_data(as_text=True)

        assert "function updateJumpHudPosition()" in js
        assert "scroll.offsetTop" in js
        assert "topOffset + 12" in js
        # Ensure wired into showJumpHud, showJumpHudError, renderPinnedBanner, and showContextJumpBanner
        assert "updateJumpHudPosition();" in js

    def test_jump_to_message_and_pinned_banner_resilience(self, app_client):
        """Verify jumpToMessage and jumpToTimestamp anchor messages and prevent bottom-snap."""
        js_resp = app_client.get("/static/app.js")
        assert js_resp.status_code == 200
        js = js_resp.get_data(as_text=True)

        # jumpToMessage must invalidate _loadGen, cancel bottom-anchor, and call _pinToMsg
        assert "async function jumpToMessage(msg, highlightType)" in js
        assert "await _pinToMsg(inWin, gen);" in js
        assert "isUserAtBottom = false;" in js

        # jumpToTimestamp must ensure isUserAtBottom is false and chat-loading is hidden
        assert "async function jumpToTimestamp(ts, exactMsgId, isExactTarget, highlightType)" in js
        assert "await _pinToMsg(existing, gen);" in js

        # pinned banner must have z-index: 12 so it remains interactive and unblocked
        css_resp = app_client.get("/static/app.css")
        assert css_resp.status_code == 200
        css = css_resp.get_data(as_text=True)
        assert "#pinned-banner" in css
        assert "z-index: 12;" in css

    def test_android_pinned_without_expiry_timestamp_column(self, tmp_path):
        """Android pinned query succeeds on schemas lacking message_add_on.expiry_timestamp."""
        wa_path = tmp_path / "msgstore.db"
        with sqlite3.connect(str(wa_path)) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
                CREATE TABLE message (
                    _id INTEGER PRIMARY KEY,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    timestamp INTEGER,
                    text_data TEXT,
                    message_type INTEGER,
                    key_id TEXT
                );
                CREATE TABLE message_add_on (
                    _id INTEGER PRIMARY KEY,
                    parent_message_row_id INTEGER,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    key_id TEXT,
                    timestamp INTEGER,
                    message_add_on_type INTEGER,
                    status INTEGER
                );
                CREATE TABLE message_add_on_pin_in_chat (
                    message_add_on_row_id INTEGER PRIMARY KEY,
                    pin_in_chat_state INTEGER,
                    sender_timestamp INTEGER
                );
                CREATE TABLE message_media (
                    message_row_id INTEGER PRIMARY KEY,
                    file_path TEXT,
                    mime_type TEXT,
                    media_duration INTEGER,
                    media_name TEXT
                );

                INSERT INTO jid VALUES (1, '1203630000000001', 'g.us', '1203630000000001@g.us');
                INSERT INTO chat VALUES (10, 1, 'Group Without Expiry Col');
                INSERT INTO message VALUES (50, 10, 0, NULL, 1793970000000, 'Legacy schema pin', 0, 'PIN_1');
                INSERT INTO message_add_on VALUES (1, 50, 10, 0, NULL, 'ADDON_1', 1793970500000, 79, 0);
                INSERT INTO message_add_on_pin_in_chat VALUES (1, 1, 1793970500000);
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/10/pinned?chat_type=group")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 1
            assert pins[0]["id"] == 50
            assert pins[0]["text_body"] == "Legacy schema pin"

    def test_android_pinned_privacy_lid_contact_resolution(self, tmp_path):
        """1-to-1 Privacy LID contact chats resolve pinned messages queried by phone number."""
        wa_path = tmp_path / "msgstore.db"
        with sqlite3.connect(str(wa_path)) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
                CREATE TABLE jid_map (lid_row_id INTEGER PRIMARY KEY, jid_row_id INTEGER);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
                CREATE TABLE message (
                    _id INTEGER PRIMARY KEY,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    timestamp INTEGER,
                    text_data TEXT,
                    message_type INTEGER,
                    key_id TEXT
                );
                CREATE TABLE message_add_on (
                    _id INTEGER PRIMARY KEY,
                    parent_message_row_id INTEGER,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    key_id TEXT,
                    timestamp INTEGER,
                    message_add_on_type INTEGER,
                    status INTEGER
                );

                -- Phone JID 1, LID JID 2
                INSERT INTO jid VALUES (1, '15551234', 's.whatsapp.net', '15551234@s.whatsapp.net');
                INSERT INTO jid VALUES (2, '999888777', 'lid', '999888777@lid');
                INSERT INTO jid_map VALUES (2, 1);
                -- Chat uses Privacy LID JID 2
                INSERT INTO chat VALUES (5, 2, NULL);
                INSERT INTO message VALUES (60, 5, 0, NULL, 1793970000000, 'Pinned in LID chat', 0, 'PIN_LID');
                INSERT INTO message_add_on VALUES (1, 60, 5, 0, NULL, 'ADDON_LID', 1793970500000, 79, 0);
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/15551234/pinned?chat_type=contact")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 1
            assert pins[0]["id"] == 60
            assert pins[0]["text_body"] == "Pinned in LID chat"

    def test_android_pinned_1to1_sender_name_resolved(self, tmp_path):
        """Incoming pinned messages in 1-to-1 chats resolve sender name from chat contact."""
        wa_path = tmp_path / "msgstore.db"
        arch_path = tmp_path / ".wa_media_archiver.db"

        with sqlite3.connect(str(wa_path)) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
                CREATE TABLE message (
                    _id INTEGER PRIMARY KEY,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    timestamp INTEGER,
                    text_data TEXT,
                    message_type INTEGER,
                    key_id TEXT
                );
                CREATE TABLE message_add_on (
                    _id INTEGER PRIMARY KEY,
                    parent_message_row_id INTEGER,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    key_id TEXT,
                    timestamp INTEGER,
                    message_add_on_type INTEGER,
                    status INTEGER
                );

                INSERT INTO jid VALUES (1, '15559876', 's.whatsapp.net', '15559876@s.whatsapp.net');
                INSERT INTO chat VALUES (3, 1, NULL);
                -- Incoming message (from_me = 0, sender_jid_row_id = NULL)
                INSERT INTO message VALUES (70, 3, 0, NULL, 1793970000000, '1-to-1 note', 0, 'PIN_1TO1');
                INSERT INTO message_add_on VALUES (1, 70, 3, 0, NULL, 'ADDON_70', 1793970500000, 79, 0);
            """)
            conn.commit()

        with sqlite3.connect(str(arch_path)) as arch_conn:
            arch_conn.executescript("""
                CREATE TABLE contacts (
                    number TEXT PRIMARY KEY,
                    folder TEXT,
                    display_name TEXT
                );
                INSERT INTO contacts VALUES ('15559876', 'Dr. Vance', 'Dr. Vance');
            """)
            arch_conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/15559876/pinned?chat_type=contact")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 1
            assert pins[0]["sender_name"] == "Dr. Vance"

    def test_android_pinned_group_lid_sender_name_resolved(self, tmp_path):
        """Group chat pinned messages from Privacy LID senders resolve via lid_display_name."""
        wa_path = tmp_path / "msgstore.db"
        with sqlite3.connect(str(wa_path)) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
                CREATE TABLE lid_display_name (lid_row_id INTEGER PRIMARY KEY, display_name TEXT, username TEXT);
                CREATE TABLE message (
                    _id INTEGER PRIMARY KEY,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    timestamp INTEGER,
                    text_data TEXT,
                    message_type INTEGER,
                    key_id TEXT
                );
                CREATE TABLE message_add_on (
                    _id INTEGER PRIMARY KEY,
                    parent_message_row_id INTEGER,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    key_id TEXT,
                    timestamp INTEGER,
                    message_add_on_type INTEGER,
                    status INTEGER
                );

                INSERT INTO jid VALUES (1, '1203630000000001', 'g.us', '1203630000000001@g.us');
                INSERT INTO jid VALUES (2, '999111222', 'lid', '999111222@lid');
                INSERT INTO chat VALUES (10, 1, 'Alpine Trail');
                INSERT INTO lid_display_name VALUES (2, 'Maya Lin', 'mayalin');
                INSERT INTO message VALUES (80, 10, 0, 2, 1793970000000, 'Group pin from LID', 0, 'PIN_GRP_LID');
                INSERT INTO message_add_on VALUES (1, 80, 10, 0, 2, 'ADDON_80', 1793970500000, 79, 0);
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/10/pinned?chat_type=group")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 1
            assert pins[0]["sender_name"] == "Maya Lin"

    def test_android_pinned_unpinned_by_subsequent_addon_excluded(self, tmp_path):
        """A message pinned and subsequently unpinned in a later add-on row is excluded."""
        wa_conn = make_android_db(tmp_path / "msgstore.db")
        wa_conn.executescript("""
            CREATE TABLE IF NOT EXISTS message_add_on (
                _id INTEGER PRIMARY KEY,
                parent_message_row_id INTEGER,
                chat_row_id INTEGER,
                from_me INTEGER,
                sender_jid_row_id INTEGER,
                key_id TEXT,
                timestamp INTEGER,
                message_add_on_type INTEGER,
                status INTEGER
            );
            CREATE TABLE IF NOT EXISTS message_add_on_pin_in_chat (
                message_add_on_row_id INTEGER PRIMARY KEY,
                pin_in_chat_state INTEGER,
                sender_timestamp INTEGER
            );

            INSERT INTO jid (_id, user, raw_string) VALUES (10, '1203630000000001', '1203630000000001@g.us');
            INSERT INTO chat (_id, jid_row_id, subject) VALUES (10, 10, 'Lifecycle Chat');
            INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type, key_id)
            VALUES (90, 10, 0, 1793970000000, 'Unpinned message', 0, 'PIN_90');

            -- Event 1: Pinned (state = 1)
            INSERT INTO message_add_on (_id, parent_message_row_id, chat_row_id, from_me, message_add_on_type, timestamp)
            VALUES (1, 90, 10, 0, 79, 1793970500000);
            INSERT INTO message_add_on_pin_in_chat VALUES (1, 1, 1793970500000);

            -- Event 2: Unpinned (state = 2, higher _id)
            INSERT INTO message_add_on (_id, parent_message_row_id, chat_row_id, from_me, message_add_on_type, timestamp)
            VALUES (2, 90, 10, 0, 79, 1793970600000);
            INSERT INTO message_add_on_pin_in_chat VALUES (2, 2, 1793970600000);
        """)
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/10/pinned?chat_type=group")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 0

            # Also verify /api/messages projection
            m_resp = client.get("/api/messages?chat_id=10&chat_type=group")
            assert m_resp.status_code == 200
            msgs = m_resp.get_json()
            assert msgs[0]["is_pinned"] == 0

    def test_preserved_pinned_historical_backup_not_wallclock_expired(self, tmp_path):
        """Preserved pinned messages older than 30 days are retained without wall-clock expiry."""
        make_ios_db(tmp_path / "ChatStorage.sqlite")
        pres_db = tmp_path / "preserved_messages.db"
        with sqlite3.connect(str(pres_db)) as conn:
            conn.executescript("""
                CREATE TABLE preserved_messages (
                    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                    msg_id INTEGER,
                    chat_id TEXT,
                    chat_type TEXT,
                    from_me INTEGER,
                    sender_name TEXT,
                    timestamp_ms INTEGER,
                    text_body TEXT,
                    is_pinned INTEGER DEFAULT 1,
                    pin_expiry_ts INTEGER,
                    media_type TEXT
                );

                -- Preserved pin with pin_expiry_ts from the past (2020)
                INSERT INTO preserved_messages (msg_id, chat_id, chat_type, from_me, sender_name, timestamp_ms, text_body, is_pinned, pin_expiry_ts, media_type)
                VALUES (99, '10', 'group', 0, 'Alice', 1580000000000, 'Old preserved pin', 1, 1582592000000, 'text');
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/10/pinned?chat_type=group")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 1
            assert pins[0]["text_body"] == "Old preserved pin"

    def test_android_pinned_after_chat_messages_opened_resolution(self, tmp_path):
        """Pinned query succeeds when messages endpoint ran first, populating temp resolution tables."""
        wa_conn = make_android_db(tmp_path / "msgstore.db")
        wa_conn.executescript("""
            CREATE TABLE IF NOT EXISTS message_add_on (
                _id INTEGER PRIMARY KEY,
                parent_message_row_id INTEGER,
                chat_row_id INTEGER,
                from_me INTEGER,
                sender_jid_row_id INTEGER,
                key_id TEXT,
                timestamp INTEGER,
                message_add_on_type INTEGER,
                status INTEGER
            );
            CREATE TABLE IF NOT EXISTS message_add_on_pin_in_chat (
                message_add_on_row_id INTEGER PRIMARY KEY,
                pin_in_chat_state INTEGER,
                sender_timestamp INTEGER
            );

            INSERT INTO jid (_id, user, raw_string) VALUES (5, '15551234', '15551234@s.whatsapp.net');
            INSERT INTO chat (_id, jid_row_id, subject) VALUES (5, 5, NULL);
            INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type, key_id)
            VALUES (101, 5, 0, 1793970000000, 'Order of execution pin', 0, 'PIN_101');

            INSERT INTO message_add_on (_id, parent_message_row_id, chat_row_id, from_me, message_add_on_type, timestamp)
            VALUES (10, 101, 5, 0, 79, 1793970500000);
            INSERT INTO message_add_on_pin_in_chat VALUES (10, 1, 1793970500000);
        """)
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            # 1. Fetch messages first (runs _resolve_android_chat_row_ids before pinned endpoint)
            m_resp = client.get("/api/messages?chat_id=15551234&chat_type=contact")
            assert m_resp.status_code == 200

            # 2. Fetch pinned messages banner (must not fail on missing _contacts_resolved)
            resp = client.get("/api/chat/15551234/pinned?chat_type=contact")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 1
            assert pins[0]["id"] == 101
            assert pins[0]["text_body"] == "Order of execution pin"
            assert pins[0]["is_pinned"] is True

    def test_android_pinned_pin_table_left_join_null_state(self, tmp_path):
        """Active pin is returned when message_add_on_pin_in_chat row is absent or has null state."""
        wa_conn = make_android_db(tmp_path / "msgstore.db")
        wa_conn.executescript("""
            CREATE TABLE IF NOT EXISTS message_add_on (
                _id INTEGER PRIMARY KEY,
                parent_message_row_id INTEGER,
                chat_row_id INTEGER,
                from_me INTEGER,
                sender_jid_row_id INTEGER,
                key_id TEXT,
                timestamp INTEGER,
                message_add_on_type INTEGER,
                status INTEGER
            );
            CREATE TABLE IF NOT EXISTS message_add_on_pin_in_chat (
                message_add_on_row_id INTEGER PRIMARY KEY,
                pin_in_chat_state INTEGER,
                sender_timestamp INTEGER
            );

            INSERT INTO jid (_id, user, raw_string) VALUES (6, '15559999', '15559999@s.whatsapp.net');
            INSERT INTO chat (_id, jid_row_id, subject) VALUES (6, 6, NULL);
            INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type, key_id)
            VALUES (102, 6, 0, 1793970000000, 'Orphaned pin row', 0, 'PIN_102');

            -- Add-on exists with type 79, but no row in message_add_on_pin_in_chat
            INSERT INTO message_add_on (_id, parent_message_row_id, chat_row_id, from_me, message_add_on_type, timestamp)
            VALUES (11, 102, 6, 0, 79, 1793970500000);
        """)
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/15559999/pinned?chat_type=contact")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 1
            assert pins[0]["id"] == 102
            assert pins[0]["text_body"] == "Orphaned pin row"
            assert pins[0]["is_pinned"] is True

    def test_android_and_ios_audio_and_voice_note_separation(self, tmp_path):
        """Audio files and voice messages are correctly separated and flagged on both Android and iOS."""
        # 1. Test Android
        android_dir = tmp_path / "android"
        android_dir.mkdir()
        wa_conn = make_android_db(android_dir / "msgstore.db")
        arc_conn = make_archive_db(android_dir / ".wa_media_archiver.db")
        seed_android_db(wa_conn, arc_conn)

        # Ensure message has origin column and message_media has duration
        wa_conn.execute("ALTER TABLE message ADD COLUMN origin INTEGER")
        wa_conn.execute("ALTER TABLE message_media ADD COLUMN media_duration INTEGER")

        # Insert Voice Message (origin = 1, in WhatsApp Voice Notes folder)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type, origin) "
            "VALUES (101, 10, 0, 1700000001000, NULL, 2, 1)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name, media_duration) "
            "VALUES (101, 'Media/WhatsApp Voice Notes/202601/audio1.opus', 'audio1.opus', 42)"
        )

        # Insert Audio File (origin = 0, in WhatsApp Audio folder)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type, origin) "
            "VALUES (102, 10, 1, 1700000002000, NULL, 2, 0)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name, media_duration) "
            "VALUES (102, 'Media/WhatsApp Audio/track.mp3', 'CoolSong.mp3', 180)"
        )

        # Record archive copies
        arc_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES "
            "('Media/WhatsApp Voice Notes/202601/audio1.opus', 'Alice (00123456789)/audio1.opus'), "
            "('Media/WhatsApp Audio/track.mp3', 'Alice (00123456789)/track.mp3')"
        )
        wa_conn.commit()
        arc_conn.commit()
        wa_conn.close()
        arc_conn.close()

        app = viewer.create_app(android_dir, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            # Check messages endpoint
            resp = client.get("/api/messages?chat_id=123456789&chat_type=contact").get_json()
            msgs = {m["msg_id"]: m for m in resp}
            assert msgs[101]["is_voice_note"] == 1
            assert msgs[101]["media_name"] == "Voice message"
            assert msgs[101]["media_duration"] == 42
            assert msgs[102]["is_voice_note"] == 0
            assert msgs[102]["media_name"] == "CoolSong.mp3"
            assert msgs[102]["media_duration"] == 180

            # Check media count breakdown has separate voice and audio
            cnt = client.get("/api/media/count?chat_id=123456789&chat_type=contact").get_json()
            assert cnt["by_type"]["voice"]["count"] == 1
            assert cnt["by_type"]["audio"]["count"] == 1

            # Check /api/media filtering by types=voice vs types=audio
            voice_media = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=voice"
            ).get_json()
            assert len(voice_media) == 1
            assert voice_media[0]["msg_id"] == 101

            audio_media = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=audio"
            ).get_json()
            assert len(audio_media) == 1
            assert audio_media[0]["msg_id"] == 102

        # 2. Test iOS
        ios_dir = tmp_path / "ios"
        ios_dir.mkdir()
        ios_db = make_ios_db(ios_dir / "ChatStorage.sqlite")
        ios_arc = make_archive_db(ios_dir / ".wa_media_archiver.db")
        ios_db.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN) "
            "VALUES (1, '123456789@s.whatsapp.net', 'Alice', 0)"
        )

        try:
            ios_db.execute("ALTER TABLE ZWAMEDIAITEM ADD COLUMN ZMEDIAORIGIN INTEGER")
        except Exception:
            pass
        try:
            ios_db.execute("ALTER TABLE ZWAMEDIAITEM ADD COLUMN ZMOVIEDURATION INTEGER")
        except Exception:
            pass

        # Insert iOS Voice note (ZMESSAGETYPE = 3, ZMEDIAORIGIN = 1, .opus)
        ios_db.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZMEDIAORIGIN, ZMOVIEDURATION) "
            "VALUES (501, 'Media/chat/voice.opus', 1, 35)"
        )
        ios_db.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGETYPE, ZMESSAGEDATE, ZMEDIAITEM) "
            "VALUES (201, 1, 0, 3, 700000001, 501)"
        )

        # Insert iOS Audio track (ZMESSAGETYPE = 3, ZMEDIAORIGIN = 0, ZTITLE = 'Song')
        ios_db.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZMEDIAORIGIN, ZMOVIEDURATION, ZTITLE) "
            "VALUES (502, 'Media/chat/track.m4a', 0, 210, 'Great Song')"
        )
        ios_db.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGETYPE, ZMESSAGEDATE, ZMEDIAITEM) "
            "VALUES (202, 1, 1, 3, 700000002, 502)"
        )

        ios_arc.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES "
            "('Message/Media/chat/voice.opus', 'Alice/voice.opus'), "
            "('Message/Media/chat/track.m4a', 'Alice/track.m4a')"
        )
        ios_db.commit()
        ios_arc.commit()
        ios_db.close()
        ios_arc.close()

        app_ios = viewer.create_app(ios_dir, rescan=False)
        app_ios.config["TESTING"] = True
        with app_ios.test_client() as client:
            resp = client.get("/api/messages?chat_id=1&chat_type=contact").get_json()
            msgs = {m["msg_id"]: m for m in resp}
            assert msgs[201]["is_voice_note"] == 1
            assert msgs[201]["media_name"] == "Voice message"
            assert msgs[201]["media_duration"] == 35
            assert msgs[202]["is_voice_note"] == 0
            assert msgs[202]["media_name"] == "Great Song"
            assert msgs[202]["media_duration"] == 210

            cnt = client.get("/api/media/count?chat_id=1&chat_type=contact").get_json()
            assert cnt["by_type"]["voice"]["count"] == 1
            assert cnt["by_type"]["audio"]["count"] == 1

            voice_media = client.get(
                "/api/media?chat_id=1&chat_type=contact&types=voice"
            ).get_json()
            assert len(voice_media) == 1
            assert voice_media[0]["msg_id"] == 201

            audio_media = client.get(
                "/api/media?chat_id=1&chat_type=contact&types=audio"
            ).get_json()
            assert len(audio_media) == 1
            assert audio_media[0]["msg_id"] == 202


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


def test_convert_obj_keeps_paths_and_urls():
    # Archive folders keep the original characters: a converted path would point to a missing file.
    data = {
        "chat_name": "Trip \ue11c",
        "archive_path": "Groups/Trip \ue11c/2014/IMG.jpg",
        "original_path": "Media/\ue11c.jpg",
        "link_url": "https://example.com/\ue11c",
        "items": [{"sender_name": "\ue404", "live_photo_path": "Groups/Trip \ue11c/x.mov"}],
    }
    assert convert_obj(data) == {
        "chat_name": "Trip \U0001f480",
        "archive_path": "Groups/Trip \ue11c/2014/IMG.jpg",
        "original_path": "Media/\ue11c.jpg",
        "link_url": "https://example.com/\ue11c",
        "items": [{"sender_name": "\U0001f601", "live_photo_path": "Groups/Trip \ue11c/x.mov"}],
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
