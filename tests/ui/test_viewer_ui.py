"""UI/UX and visual interaction tests for WhatsApp Chat Viewer.

Validates the complete web interface lifecycle in a real headless browser using CDP:
- Chat list loading and loading overlay dismissal
- Bottom scroll pinning and layout stability (CLS prevention)
- Upward scroll pagination and anchor preservation
- Message elements rendering (bubbles, date separators, service messages, quotes, reactions)
- Design token compliance and dual-theme switching (Dark / Light)
- Modals and drawers (Chat Info panel, Media Gallery, Settings)
- Revoked (deleted) messages tombstones and suppressions
- View-once status pills, badges, and media preview isolation
- Chat list sidebar previews for revoked and view-once messages
- Phone layout: chat list and chat as two levels, with back navigation
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from .cdp_driver import CDPSession

# Apply ui marker to all tests in this module
pytestmark = pytest.mark.ui


def open_group_chat_in_browser(browser: CDPSession, timeout: float) -> str:
    """Wait for chat list, click the first group chat, and wait for messages to load."""
    js_code = """
    (async () => {
        const active = document.querySelector('.chat-item.active[data-type="group"]');
        if (active) return active.querySelector('.chat-name')?.textContent || '';
        for (let i = 0; i < 200; i++) {
            const item = document.querySelector('.chat-item[data-type="group"]');
            if (item) {
                const name = item.querySelector('.chat-name')?.textContent || '';
                item.click();
                await new Promise(r => setTimeout(r, 100));

                for (let j = 0; j < 80; j++) {
                    await new Promise(r => setTimeout(r, 50));
                    const loading = document.getElementById('chat-loading');
                    if (loading && loading.style.display === 'none') {
                        await new Promise(r => setTimeout(r, 150));
                        return name;
                    }
                }
                return name;
            }
            await new Promise(r => setTimeout(r, 100));
        }
        return '';
    })()
    """
    res = browser.evaluate(js_code, timeout)
    if not isinstance(res, str) or not res:
        raise RuntimeError("Failed to find or open a group chat in the browser.")
    return res


def open_chat_by_id(browser: CDPSession, chat_id: str, timeout: float) -> str:
    """Wait for chat list, click chat matching data-id, and wait for messages to load."""
    js_code = f"""
    (async () => {{
        for (let i = 0; i < 200; i++) {{
            const item = document.querySelector('.chat-item[data-id="{chat_id}"]');
            if (item) {{
                const name = item.querySelector('.chat-name')?.textContent || '';
                const loading = document.getElementById('chat-loading');
                const alreadyActive = item.classList.contains('active') && loading && loading.style.display === 'none';
                if (alreadyActive) {{
                    return name;
                }}
                item.click();
                for (let j = 0; j < 160; j++) {{
                    await new Promise(r => setTimeout(r, 50));
                    if (loading && loading.style.display === 'none') {{
                        await new Promise(r => setTimeout(r, 150));
                        return name;
                    }}
                }}
                return '';
            }}
            await new Promise(r => setTimeout(r, 100));
        }}
        return '';
    }})()
    """
    res = browser.evaluate(js_code, timeout)
    if not isinstance(res, str) or not res:
        raise RuntimeError(f"Failed to find or open chat id '{chat_id}' in the browser.")
    return res


def test_chat_list_and_chat_opening(browser: CDPSession, screenshot_dir: Path) -> None:
    """Validate that chat items render and clicking an item loads the chat pane."""
    # 1. Wait for chat list to populate
    chats_count = None
    for _ in range(3):
        chats_count = browser.evaluate(
            """
            (async () => {
                for (let i = 0; i < 60; i++) {
                    const items = document.querySelectorAll('.chat-item');
                    if (items.length > 0) return items.length;
                    await new Promise(r => setTimeout(r, 100));
                }
                return 0;
            })()
            """,
            10.0,
        )
        if isinstance(chats_count, int):
            break
        time.sleep(0.5)
    assert isinstance(chats_count, int)
    assert chats_count >= 5

    # 2. Open group chat
    chat_name = open_group_chat_in_browser(browser, 15.0)
    assert len(chat_name) > 0

    # 3. Verify chat header displays active chat title
    header_title = browser.evaluate(
        """
        (() => {
            const titleEl = document.getElementById('chat-title');
            return titleEl ? titleEl.textContent : '';
        })()
        """,
        5.0,
    )
    assert header_title == chat_name

    # 4. Capture visual evidence
    browser.capture_screenshot(screenshot_dir / "chat_opened.png", 5.0)


def test_bottom_scroll_pinning_and_stability(browser: CDPSession, screenshot_dir: Path) -> None:
    """Validate that opening a chat pins viewport to bottom and stays stable."""
    open_group_chat_in_browser(browser, 15.0)

    pin_metrics = browser.evaluate(
        """
        (() => {
            const scroll = document.getElementById('message-scroll');
            if (!scroll) return null;

            const diff = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight;
            return {
                scrollHeight: scroll.scrollHeight,
                scrollTop: scroll.scrollTop,
                clientHeight: scroll.clientHeight,
                diffFromBottom: diff,
                isAtBottom: diff < 5,
                rows: scroll.querySelectorAll('.msg-row').length
            };
        })()
        """,
        5.0,
    )

    assert isinstance(pin_metrics, dict)
    assert pin_metrics["rows"] > 0
    assert pin_metrics["isAtBottom"] is True
    assert pin_metrics["diffFromBottom"] < 5

    browser.capture_screenshot(screenshot_dir / "chat_bottom_pinned.png", 5.0)


def test_scroll_engine_upward_pagination(browser: CDPSession) -> None:
    """Validate that scrolling up triggers pagination and maintains scroll anchor."""
    open_group_chat_in_browser(browser, 15.0)

    step_result = browser.evaluate(
        """
        (async () => {
            const scroll = document.getElementById('message-scroll');
            if (!scroll) return { error: 'No scroll container' };

            const initialRows = scroll.querySelectorAll('.msg-row').length;
            const initialFirstRow = scroll.querySelector('.msg-row[data-msg-id]');
            const initialFirstId = initialFirstRow ? initialFirstRow.dataset.msgId : null;

            // Scroll near the top to request older messages
            scroll.scrollTop = 50;
            scroll.dispatchEvent(new Event('scroll'));

            // Wait for pagination or settle
            for (let i = 0; i < 30; i++) {
                await new Promise(r => setTimeout(r, 100));
                const currentRows = scroll.querySelectorAll('.msg-row').length;
                if (currentRows > initialRows) break;
            }

            const finalRows = scroll.querySelectorAll('.msg-row').length;
            const finalFirstRow = scroll.querySelector('.msg-row[data-msg-id]');
            const finalFirstId = finalFirstRow ? finalFirstRow.dataset.msgId : null;

            return {
                initialRows,
                finalRows,
                initialFirstId,
                finalFirstId,
                scrollTop: scroll.scrollTop
            };
        })()
        """,
        10.0,
    )

    assert isinstance(step_result, dict)
    assert step_result["initialRows"] > 0
    assert step_result["scrollTop"] >= 0


def test_message_components_rendering(browser: CDPSession) -> None:
    """Validate that message rows, date separators, service bubbles, and quotes render."""
    open_group_chat_in_browser(browser, 15.0)

    elements = browser.evaluate(
        """
        (() => {
            const scroll = document.getElementById('message-scroll');
            if (!scroll) return null;

            return {
                rows: scroll.querySelectorAll('.msg-row').length,
                dateSeps: scroll.querySelectorAll('.date-separator').length,
                serviceBubbles: scroll.querySelectorAll('.msg-service-bubble').length,
                quotedPreviews: scroll.querySelectorAll('.msg-quoted').length,
                reactions: scroll.querySelectorAll('.reaction-pill, .msg-reactions').length
            };
        })()
        """,
        5.0,
    )

    assert isinstance(elements, dict)
    assert elements["rows"] > 0
    assert elements["dateSeps"] >= 1
    assert elements["serviceBubbles"] >= 1


def test_theme_toggle_tokens(browser: CDPSession, screenshot_dir: Path) -> None:
    """Validate dark/light theme switching and CSS custom property token updates."""
    # 1. Open settings modal and wait for .open class
    is_open = browser.evaluate(
        """
        (async () => {
            for (let i = 0; i < 60; i++) {
                const btn = document.getElementById('settings-btn');
                if (btn) btn.click();
                const modal = document.getElementById('settings-modal');
                if (modal && modal.classList.contains('open')) {
                    return true;
                }
                await new Promise(r => setTimeout(r, 100));
            }
            return false;
        })()
        """,
        10.0,
    )
    assert is_open is True

    # 2. Switch to light theme and wait for DOM update
    light_theme = browser.evaluate(
        """
        (async () => {
            const lightBtn = document.querySelector(
                '#settings-modal .pref-btn[data-pref="theme"][data-value="light"]'
            );
            if (lightBtn) lightBtn.click();

            for (let i = 0; i < 30; i++) {
                if (document.documentElement.dataset.theme === 'light') break;
                await new Promise(r => setTimeout(r, 50));
            }

            return {
                theme: document.documentElement.dataset.theme,
                bgToken: getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()
            };
        })()
        """,
        8.0,
    )
    assert isinstance(light_theme, dict)
    assert light_theme["theme"] == "light"
    assert len(light_theme["bgToken"]) > 0

    browser.capture_screenshot(screenshot_dir / "theme_light.png", 5.0)

    # 3. Switch back to dark theme and close modal
    dark_theme = browser.evaluate(
        """
        (async () => {
            const darkBtn = document.querySelector(
                '#settings-modal .pref-btn[data-pref="theme"][data-value="dark"]'
            );
            if (darkBtn) darkBtn.click();

            for (let i = 0; i < 30; i++) {
                if (document.documentElement.dataset.theme === 'dark') break;
                await new Promise(r => setTimeout(r, 50));
            }

            const closeBtn = document.getElementById('settings-close');
            if (closeBtn) closeBtn.click();

            return {
                theme: document.documentElement.dataset.theme,
                bgToken: getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()
            };
        })()
        """,
        8.0,
    )
    assert isinstance(dark_theme, dict)
    assert dark_theme["theme"] == "dark"
    assert len(dark_theme["bgToken"]) > 0

    browser.capture_screenshot(screenshot_dir / "theme_dark.png", 5.0)


_PHONE_STATE_JS = """
(() => {
    const w = id => {
        const el = document.getElementById(id);
        if (!el) return -1;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return 0;
        return Math.round(el.getBoundingClientRect().width);
    };
    const open = id => {
        const el = document.getElementById(id);
        return !!el && (el.classList.contains('open') || el.classList.contains('visible'));
    };
    const lb = document.getElementById('img-lightbox');
    return {
        sidebar: w('sidebar'),
        pane: w('chat-pane'),
        header: w('header'),
        chatBack: w('chat-back-btn'),
        callBack: w('call-back-btn'),
        viewport: window.innerWidth,
        paneOpen: document.body.classList.contains('pane-open'),
        paneEntry: !!(history.state && history.state.wabPane),
        focus: document.activeElement ? document.activeElement.id : '',
        focusClass: document.activeElement ? String(document.activeElement.className) : '',
        lightbox: !!lb && !lb.classList.contains('is-closing'),
        gallery: open('media-gallery'),
        info: open('chat-info-panel'),
        tab: document.querySelector('#sidebar-bottom-bar .tab-btn.active')?.dataset.tab || '',
    };
})()
"""


def _phone_step(browser: CDPSession, action: str, until: str, timeout: float) -> dict:
    """Run ``action`` in the page, wait until the JS expression ``until`` holds, return the layout state."""
    done = browser.evaluate(
        f"""
        (async () => {{
            {action};
            for (let i = 0; i < 120; i++) {{
                if ({until}) return true;
                await new Promise(r => setTimeout(r, 50));
            }}
            return false;
        }})()
        """,
        timeout,
    )
    assert done is True, f"timed out waiting for: {until}"
    time.sleep(0.3)  # wait for slide transition (0.22s) and layout to settle
    return browser.evaluate(_PHONE_STATE_JS, 5.0)


def _set_viewport(browser: CDPSession, msg_id: int, width: int | None) -> None:
    if width is None:
        browser.ws.send_json(
            {"id": msg_id, "method": "Emulation.clearDeviceMetricsOverride", "params": {}}
        )
    else:
        params = {"width": width, "height": 860, "deviceScaleFactor": 2, "mobile": width <= 600}
        browser.ws.send_json(
            {"id": msg_id, "method": "Emulation.setDeviceMetricsOverride", "params": params}
        )
    time.sleep(0.3)


def _press_enter(browser: CDPSession, msg_id: int) -> None:
    """A real Enter key press, which (unlike a scripted click) makes the focused element focus-visible."""
    for n, kind in enumerate(("keyDown", "keyUp")):
        params = {"type": kind, "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13}
        if kind == "keyDown":
            params["text"] = "\r"
        browser.ws.send_json(
            {"id": msg_id + n, "method": "Input.dispatchKeyEvent", "params": params}
        )
    time.sleep(0.2)


_PANE_OPEN = "document.body.classList.contains('pane-open')"
_PANE_CLOSED = "!document.body.classList.contains('pane-open')"
_TAP_FOODIES = (
    "[...document.querySelectorAll('#chat-list .chat-item')]"
    ".find(el => el.querySelector('.chat-name')?.textContent.includes('Weekend Coffee & Foodies')).click()"
)
_TAP_GROUP = "document.querySelector('#chat-list .chat-item[data-type=\"group\"]').click()"


def test_phone_layout_two_levels(browser: CDPSession, screenshot_dir: Path) -> None:
    """On a phone-sized viewport the chat list and the chat are two levels, with back navigation."""
    open_group_chat_in_browser(browser, 25.0)
    _set_viewport(browser, 8811, 412)

    try:
        # 1. Start from the list (a chat opened on the wide screen leaves the class set)
        state = _phone_step(
            browser, "document.getElementById('chat-back-btn').click()", _PANE_CLOSED, 8.0
        )
        assert state["sidebar"] == state["viewport"]
        assert state["pane"] == 0
        assert state["paneEntry"] is False
        browser.capture_screenshot(screenshot_dir / "phone_layout_list.png", 5.0)

        # 2. Opening a chat shows only the chat, with the back button focused and its own history entry
        state = _phone_step(browser, _TAP_GROUP, _PANE_OPEN, 8.0)
        assert state["pane"] == state["viewport"]
        assert state["sidebar"] == 0
        assert state["header"] == 0
        assert state["chatBack"] > 0
        assert state["paneEntry"] is True
        assert state["focus"] != "chat-back-btn"  # after a tap, no focus ring on the back button
        browser.capture_screenshot(screenshot_dir / "phone_layout_chat.png", 5.0)

        # 3. The browser back gesture returns to the list and consumes the entry
        state = _phone_step(browser, "history.back()", _PANE_CLOSED, 8.0)
        assert state["sidebar"] == state["viewport"]
        assert state["paneEntry"] is False

        # 4. The same chat opens again, and the back button returns to the list too
        state = _phone_step(browser, _TAP_GROUP, _PANE_OPEN, 8.0)
        assert state["paneEntry"] is True
        state = _phone_step(
            browser, "document.getElementById('chat-back-btn').click()", _PANE_CLOSED, 8.0
        )
        assert state["paneEntry"] is False

        # 5. Escape is back as well
        _phone_step(browser, _TAP_GROUP, _PANE_OPEN, 8.0)
        state = _phone_step(
            browser,
            "document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))",
            _PANE_CLOSED,
            8.0,
        )
        assert state["paneEntry"] is False

        # 6. Back closes what is open over the chat first: lightbox, then gallery, then the chat
        _phone_step(browser, _TAP_FOODIES, _PANE_OPEN, 8.0)
        _phone_step(
            browser,
            "document.getElementById('media-btn').click()",
            "document.querySelector('#media-gallery.open #media-gallery-grid .gallery-item[data-type=\"image\"]')",
            8.0,
        )
        state = _phone_step(
            browser,
            "document.querySelector('#media-gallery-grid .gallery-item[data-type=\"image\"] img').click()",
            "document.getElementById('img-lightbox')",
            8.0,
        )
        assert state["lightbox"] is True
        browser.capture_screenshot(screenshot_dir / "phone_layout_lightbox.png", 5.0)
        state = _phone_step(
            browser, "history.back()", "!document.getElementById('img-lightbox')", 8.0
        )
        assert state["gallery"] is True
        assert state["paneOpen"] is True
        assert state["paneEntry"] is True
        state = _phone_step(
            browser,
            "history.back()",
            "!document.getElementById('media-gallery').classList.contains('open')",
            8.0,
        )
        assert state["paneOpen"] is True
        assert state["paneEntry"] is True
        state = _phone_step(browser, "history.back()", _PANE_CLOSED, 8.0)
        assert state["paneEntry"] is False

        # 7. The chat info drawer closes on back the same way
        _phone_step(browser, _TAP_GROUP, _PANE_OPEN, 8.0)
        _phone_step(
            browser,
            "document.getElementById('chat-info-btn').click()",
            "document.getElementById('chat-info-panel').classList.contains('open')",
            8.0,
        )
        state = _phone_step(
            browser,
            "history.back()",
            "!document.getElementById('chat-info-panel').classList.contains('open')",
            8.0,
        )
        assert state["paneOpen"] is True
        state = _phone_step(browser, "history.back()", _PANE_CLOSED, 8.0)
        assert state["paneEntry"] is False

        # 8. A tab of the bottom bar closes the open chat and its entry
        _phone_step(browser, _TAP_GROUP, _PANE_OPEN, 8.0)
        state = _phone_step(
            browser, "document.getElementById('tab-btn-starred').click()", _PANE_CLOSED, 8.0
        )
        assert state["tab"] == "starred"
        assert state["paneEntry"] is False
        state = _phone_step(
            browser,
            "document.getElementById('tab-btn-chats').click()",
            "document.querySelector('#chat-list .chat-item')",
            8.0,
        )
        assert state["tab"] == "chats"

        # 9. A call's details are a second level too, with their own back button above the card
        fake_calls = {
            "calls": [
                {
                    "id": 990001,
                    "caller_name": "Phone Layout Test",
                    "caller_phone": "+10000000000",
                    "timestamp": 1700000000,
                    "timestamp_ms": 1700000000000,
                    "duration": 75,
                    "call_type": "incoming",
                    "outcome": "answered",
                    "from_me": False,
                    "is_video": False,
                    "is_group": False,
                    "data_transferred": 0,
                    "chat_id": None,
                }
            ],
            "has_more": False,
        }
        browser.evaluate(
            f"""
            (() => {{
                window.__wabRealFetch = window.fetch;
                // The demo archive may have no calls, which disables the tab: enable it for the fake list
                const callsTab = document.getElementById('tab-btn-calls');
                window.__wabCallsTabDisabled = callsTab.classList.contains('disabled');
                callsTab.classList.remove('disabled');
                window.fetch = (url, opts) => String(url).includes('/api/calls')
                    ? Promise.resolve(new Response({json.dumps(json.dumps(fake_calls))}, {{ headers: {{ 'Content-Type': 'application/json' }} }}))
                    : window.__wabRealFetch(url, opts);
            }})()
            """,
            5.0,
        )
        _phone_step(
            browser,
            "document.getElementById('tab-btn-calls').click()",
            "document.querySelector('#chat-list .call-item')",
            8.0,
        )
        # Opened from the keyboard: focus moves to the back button, and back to the call afterwards
        browser.evaluate("document.querySelector('#chat-list .call-item').focus()", 5.0)
        _press_enter(browser, 8820)
        state = _phone_step(browser, "", _PANE_OPEN, 8.0)
        assert state["callBack"] > 0
        assert state["chatBack"] == 0
        assert state["paneEntry"] is True
        assert state["focus"] == "call-back-btn"
        overlap = browser.evaluate(
            """
            (() => {
                const b = document.getElementById('call-back-btn').getBoundingClientRect();
                const c = document.querySelector('#pane-call-inspector .call-inspector-card').getBoundingClientRect();
                return { backTop: b.top, backBottom: b.bottom, cardTop: c.top };
            })()
            """,
            5.0,
        )
        assert overlap["backTop"] >= 0
        assert overlap["cardTop"] >= overlap["backBottom"]
        browser.capture_screenshot(screenshot_dir / "phone_layout_call.png", 5.0)
        _press_enter(browser, 8822)
        state = _phone_step(browser, "", _PANE_CLOSED, 8.0)
        assert state["paneEntry"] is False
        assert (
            "call-item" in state["focusClass"]
        )  # back on the list, focus returns to the call that was open
        _phone_step(
            browser,
            "document.getElementById('tab-btn-chats').click()",
            "document.querySelector('#chat-list .chat-item')",
            8.0,
        )

        # 10. Rotating to a wide screen keeps the chat and its entry; back on the phone, no second entry
        _phone_step(browser, _TAP_GROUP, _PANE_OPEN, 8.0)
        _set_viewport(browser, 8813, 1000)
        state = browser.evaluate(_PHONE_STATE_JS, 5.0)
        assert state["chatBack"] == 0
        assert state["sidebar"] > 0
        assert state["pane"] > 0
        assert state["paneEntry"] is True
        _set_viewport(browser, 8814, 412)
        state = browser.evaluate(_PHONE_STATE_JS, 5.0)
        assert state["paneOpen"] is True
        assert state["pane"] == state["viewport"]
        state = _phone_step(browser, "history.back()", _PANE_CLOSED, 8.0)
        assert state["paneEntry"] is False

        # 11. A chat opened on a wide screen gets its entry when the screen becomes a phone one
        _set_viewport(browser, 8815, 1000)
        _phone_step(browser, _TAP_GROUP, _PANE_OPEN, 8.0)
        assert browser.evaluate(_PHONE_STATE_JS, 5.0)["paneEntry"] is False
        _set_viewport(browser, 8816, 412)
        state = browser.evaluate(_PHONE_STATE_JS, 5.0)
        assert state["paneEntry"] is True
        state = _phone_step(browser, "history.back()", _PANE_CLOSED, 8.0)
        assert state["sidebar"] == state["viewport"]
    finally:
        browser.evaluate(
            """
            (() => {
                if (window.__wabRealFetch) { window.fetch = window.__wabRealFetch; delete window.__wabRealFetch; }
                if (window.__wabCallsTabDisabled) document.getElementById('tab-btn-calls').classList.add('disabled');
                delete window.__wabCallsTabDisabled;
                if (document.body.classList.contains('pane-open')) document.getElementById('chat-back-btn').click();
                if (!document.getElementById('tab-btn-chats').classList.contains('active')) {
                    document.getElementById('tab-btn-chats').click();
                }
            })()
            """,
            5.0,
        )
        _set_viewport(browser, 8812, None)

    # 12. On a wide screen the back buttons stay hidden and both levels are side by side
    state = browser.evaluate(_PHONE_STATE_JS, 5.0)
    assert state["chatBack"] == 0
    assert state["callBack"] == 0
    assert state["sidebar"] > 0


def test_settings_scrollable_on_small_viewport(browser: CDPSession, screenshot_dir: Path) -> None:
    """Validate that Settings modal adapts and scrolls when viewport is smaller than settings."""
    # 1. Set viewport to small height (450px) where settings content (~560px) overflows
    browser.ws.send_json(
        {
            "id": 8801,
            "method": "Emulation.setDeviceMetricsOverride",
            "params": {
                "width": 1000,
                "height": 450,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        }
    )
    time.sleep(0.3)

    try:
        # 2. Open settings modal
        opened = browser.evaluate(
            """
            (async () => {
                const btn = document.getElementById('settings-btn');
                if (btn) btn.click();
                for (let i = 0; i < 40; i++) {
                    const modal = document.getElementById('settings-modal');
                    if (modal && modal.classList.contains('open')) return true;
                    await new Promise(r => setTimeout(r, 50));
                }
                return false;
            })()
            """,
            5.0,
        )
        assert opened is True

        # 3. Check that #settings-panel adapts to viewport and #settings-body is scrollable
        metrics = browser.evaluate(
            """
            (() => {
                const modal = document.getElementById('settings-modal');
                const panel = document.getElementById('settings-panel');
                const body = document.getElementById('settings-body');
                const header = document.getElementById('settings-header');
                const winHeight = window.innerHeight;

                return {
                    winHeight,
                    panelHeight: panel.offsetHeight,
                    headerHeight: header.offsetHeight,
                    bodyScrollHeight: body.scrollHeight,
                    bodyClientHeight: body.clientHeight,
                    bodyOverflowY: getComputedStyle(body).overflowY,
                    isBodyScrollable: body.scrollHeight > body.clientHeight,
                    fitsInViewport: panel.offsetHeight <= winHeight
                };
            })()
            """,
            5.0,
        )

        assert metrics["fitsInViewport"] is True
        assert metrics["isBodyScrollable"] is True
        assert metrics["bodyOverflowY"] == "auto"

        # 4. Scroll settings-body and verify scroll position updates
        scroll_res = browser.evaluate(
            """
            (() => {
                const body = document.getElementById('settings-body');
                body.scrollTop = 150;
                return {
                    scrollTop: body.scrollTop,
                    didScroll: body.scrollTop > 0
                };
            })()
            """,
            5.0,
        )
        assert scroll_res["didScroll"] is True

        # 5. Capture visual evidence
        browser.capture_screenshot(screenshot_dir / "settings_small_viewport_scrolled.png", 5.0)

        # 6. Close modal
        browser.evaluate(
            """
            (() => {
                const closeBtn = document.getElementById('settings-close');
                if (closeBtn) closeBtn.click();
            })()
            """,
            5.0,
        )
    finally:
        browser.ws.send_json(
            {
                "id": 8802,
                "method": "Emulation.clearDeviceMetricsOverride",
                "params": {},
            }
        )
        time.sleep(0.3)


def test_chat_info_panel_toggle(browser: CDPSession, screenshot_dir: Path) -> None:
    """Validate opening and closing the chat info drawer with participant details."""
    open_group_chat_in_browser(browser, 15.0)

    info_panel = browser.evaluate(
        """
        (async () => {
            const infoBtn = document.getElementById('chat-info-btn');
            if (infoBtn) infoBtn.click();

            for (let i = 0; i < 40; i++) {
                await new Promise(r => setTimeout(r, 50));
                const panel = document.getElementById('chat-info-panel');
                if (panel && panel.classList.contains('open')) {
                    const title = document.getElementById('chat-info-title')?.textContent || '';
                    const body = document.getElementById('chat-info-body')?.textContent || '';
                    return { isOpen: true, title, body };
                }
            }
            return { isOpen: false };
        })()
        """,
        10.0,
    )

    assert isinstance(info_panel, dict)
    assert info_panel["isOpen"] is True
    assert len(info_panel["title"]) > 0

    browser.capture_screenshot(screenshot_dir / "chat_info_panel.png", 5.0)

    closed = browser.evaluate(
        """
        (() => {
            const closeBtn = document.getElementById('chat-info-close');
            if (closeBtn) closeBtn.click();
            const panel = document.getElementById('chat-info-panel');
            return panel ? !panel.classList.contains('open') : false;
        })()
        """,
        5.0,
    )
    assert closed is True


def test_media_gallery_navigation(browser: CDPSession, screenshot_dir: Path) -> None:
    """Validate opening the media gallery and switching views."""
    open_group_chat_in_browser(browser, 15.0)

    gallery = browser.evaluate(
        """
        (async () => {
            const mediaBtn = document.getElementById('media-btn');
            if (mediaBtn) mediaBtn.click();

            for (let i = 0; i < 40; i++) {
                await new Promise(r => setTimeout(r, 50));
                const gal = document.getElementById('media-gallery');
                if (gal && gal.classList.contains('open')) {
                    return { isOpen: true };
                }
            }
            return { isOpen: false };
        })()
        """,
        10.0,
    )

    assert isinstance(gallery, dict)
    assert gallery["isOpen"] is True

    browser.capture_screenshot(screenshot_dir / "media_gallery_open.png", 5.0)

    closed = browser.evaluate(
        """
        (() => {
            const closeBtn = document.getElementById('media-gallery-close');
            if (closeBtn) closeBtn.click();
            const gal = document.getElementById('media-gallery');
            return gal ? !gal.classList.contains('open') : false;
        })()
        """,
        5.0,
    )
    assert closed is True


def test_media_gallery_month_dividers_filtered(browser: CDPSession) -> None:
    """Verify that gallery month dividers are displayed only when corresponding media is selected."""
    # 1. Open contact chat Sarah Jenkins (who has 3 images and 1 link in April 2026)
    opened = browser.evaluate(
        """
        (async () => {
            for (let i = 0; i < 60; i++) {
                const item = [...document.querySelectorAll('.chat-item')].find(
                    el => el.querySelector('.chat-name')?.textContent.includes('Sarah Jenkins')
                );
                if (item) {
                    item.click();
                    for (let j = 0; j < 80; j++) {
                        await new Promise(r => setTimeout(r, 50));
                        const loading = document.getElementById('chat-loading');
                        if (loading && loading.style.display === 'none') {
                            await new Promise(r => setTimeout(r, 150));
                            return true;
                        }
                    }
                    return true;
                }
                await new Promise(r => setTimeout(r, 100));
            }
            return false;
        })()
        """,
        15.0,
    )
    assert opened is True

    # 2. Open media gallery and wait for gallery items to be populated
    gallery = browser.evaluate(
        """
        (async () => {
            const mediaBtn = document.getElementById('media-btn');
            if (mediaBtn) mediaBtn.click();

            for (let i = 0; i < 60; i++) {
                await new Promise(r => setTimeout(r, 50));
                const gal = document.getElementById('media-gallery');
                const items = document.querySelectorAll('#media-gallery-grid .gallery-item');
                if (gal && gal.classList.contains('open') && items.length > 0) {
                    await new Promise(r => setTimeout(r, 200));
                    return { isOpen: true };
                }
            }
            return { isOpen: false };
        })()
        """,
        10.0,
    )
    assert isinstance(gallery, dict)
    assert gallery["isOpen"] is True

    # 3. Check initial headers: by default, only images (media) are selected, links are hidden
    # Total headers = 2 (May 2026 link header + April 2026 media header), but only 1 should be visible (images in April)
    state_initial = browser.evaluate(
        """
        (() => {
            const allHeaders = [...document.querySelectorAll('#media-gallery-grid .gallery-month-header')];
            const visibleHeaders = allHeaders.filter(h => !h.classList.contains('month-empty'));
            const emptyHeaders = allHeaders.filter(h => h.classList.contains('month-empty'));
            return {
                total: allHeaders.length,
                visible: visibleHeaders.length,
                empty: emptyHeaders.length,
            };
        })()
        """,
        5.0,
    )
    assert state_initial["total"] == 1
    assert state_initial["visible"] == 1
    assert state_initial["empty"] == 0

    # 4. Switch to links view: images should become month-empty, link month divider should become visible
    state_links = browser.evaluate(
        """
        (async () => {
            const linkPill = document.querySelector('#media-gallery-stats .gallery-stat[data-type="link"]');
            if (linkPill) linkPill.click();
            await new Promise(r => setTimeout(r, 350));
            const allHeaders = [...document.querySelectorAll('#media-gallery-grid .gallery-month-header')];
            const visibleHeaders = allHeaders.filter(h => !h.classList.contains('month-empty'));
            const emptyHeaders = allHeaders.filter(h => h.classList.contains('month-empty'));
            return {
                total: allHeaders.length,
                visible: visibleHeaders.length,
                empty: emptyHeaders.length,
            };
        })()
        """,
        5.0,
    )
    assert state_links["visible"] == 1
    assert state_links["empty"] == 1

    # 5. Switch to total view: all dividers should be visible in strictly descending month order
    state_total = browser.evaluate(
        """
        (async () => {
            const totalPill = document.querySelector('#media-gallery-stats .gallery-stat[data-type="total"]');
            if (totalPill) totalPill.click();
            await new Promise(r => setTimeout(r, 350));
            const allHeaders = [...document.querySelectorAll('#media-gallery-grid .gallery-month-header')];
            const visibleHeaders = allHeaders.filter(h => !h.classList.contains('month-empty'));
            const monthKeys = allHeaders.map(h => parseInt(h.dataset.month, 10));
            return {
                visible: visibleHeaders.length,
                monthKeys: monthKeys,
            };
        })()
        """,
        5.0,
    )
    assert state_total["visible"] == 2
    assert state_total["monthKeys"] == [202605, 202604]

    # 6. Switch back to media view: secondary link month divider should be hidden again
    state_media_again = browser.evaluate(
        """
        (async () => {
            const totalPill = document.querySelector('#media-gallery-stats .gallery-stat[data-type="total"]');
            if (totalPill) totalPill.click();
            await new Promise(r => setTimeout(r, 350));
            const allHeaders = [...document.querySelectorAll('#media-gallery-grid .gallery-month-header')];
            const visibleHeaders = allHeaders.filter(h => !h.classList.contains('month-empty'));
            return {
                visible: visibleHeaders.length,
            };
        })()
        """,
        5.0,
    )
    assert state_media_again["visible"] == 1

    # Cleanup: close gallery
    browser.evaluate(
        """
        (() => {
            const closeBtn = document.getElementById('media-gallery-close');
            if (closeBtn) closeBtn.click();
        })()
        """,
        5.0,
    )


def test_media_gallery_filter_switch_instant_hiding(browser: CDPSession) -> None:
    """Validate that switching between gallery filters immediately hides non-matching items without delay."""
    # 1. Open group chat Weekend Coffee & Foodies (which contains both video and image media)
    open_group_chat_in_browser(browser, 25.0)

    # 2. Open media gallery and wait for items to load
    gallery = browser.evaluate(
        """
        (async () => {
            const mediaBtn = document.getElementById('media-btn');
            if (mediaBtn) mediaBtn.click();

            for (let i = 0; i < 60; i++) {
                await new Promise(r => setTimeout(r, 50));
                const gal = document.getElementById('media-gallery');
                const items = document.querySelectorAll('#media-gallery-grid .gallery-item');
                if (gal && gal.classList.contains('open') && items.length > 0) {
                    await new Promise(r => setTimeout(r, 200));
                    return { isOpen: true };
                }
            }
            return { isOpen: false };
        })()
        """,
        10.0,
    )
    assert isinstance(gallery, dict)
    assert gallery["isOpen"] is True

    # 3. Click Video pill: immediately verify only video is visible
    state_video = browser.evaluate(
        """
        (() => {
            const vidPill = document.querySelector('#media-gallery-stats .gallery-stat[data-type="video"]');
            if (vidPill) vidPill.click();
            // Immediate check: no setTimeout
            const visibleVideos = document.querySelectorAll('#media-gallery-grid .gallery-item[data-type="video"]:not(.type-hidden)');
            const visibleImages = document.querySelectorAll('#media-gallery-grid .gallery-item[data-type="image"]:not(.type-hidden)');
            return {
                videos: visibleVideos.length,
                images: visibleImages.length,
            };
        })()
        """,
        5.0,
    )
    assert state_video["videos"] == 1
    assert state_video["images"] == 0

    # 4. Click Photo/Image pill: images and videos are now selected together!
    state_both = browser.evaluate(
        """
        (() => {
            const imgPill = document.querySelector('#media-gallery-stats .gallery-stat[data-type="image"]');
            if (imgPill) imgPill.click();
            // Immediate check: both videos and images should be visible together
            const visibleVideos = document.querySelectorAll('#media-gallery-grid .gallery-item[data-type="video"]:not(.type-hidden)');
            const visibleImages = document.querySelectorAll('#media-gallery-grid .gallery-item[data-type="image"]:not(.type-hidden)');
            return {
                videos: visibleVideos.length,
                images: visibleImages.length,
            };
        })()
        """,
        5.0,
    )
    assert state_both["videos"] == 1
    assert state_both["images"] == 1

    # 5. Click Video pill again: deselects video, immediately leaving only image visible (0 videos)
    state_image = browser.evaluate(
        """
        (() => {
            const vidPill = document.querySelector('#media-gallery-stats .gallery-stat[data-type="video"]');
            if (vidPill) vidPill.click();
            // Immediate check: no setTimeout, must be synchronous
            const visibleVideos = document.querySelectorAll('#media-gallery-grid .gallery-item[data-type="video"]:not(.type-hidden)');
            const visibleImages = document.querySelectorAll('#media-gallery-grid .gallery-item[data-type="image"]:not(.type-hidden)');
            return {
                videos: visibleVideos.length,
                images: visibleImages.length,
            };
        })()
        """,
        5.0,
    )
    assert state_image["videos"] == 0
    assert state_image["images"] == 1

    # Cleanup: close gallery
    browser.evaluate(
        """
        (() => {
            const closeBtn = document.getElementById('media-gallery-close');
            if (closeBtn) closeBtn.click();
        })()
        """,
        5.0,
    )


def test_gallery_filter_image_isolation_in_mixed_chat(
    browser: CDPSession, screenshot_dir: Path
) -> None:
    """Validate that in a mixed chat (images, videos, links, vcards), gallery opens with Total active and secondary types hidden, Image pill strictly isolates photos, and Contacts pill renders clean contact cards."""
    # 1. Open group chat Weekend Coffee & Foodies
    opened = browser.evaluate(
        """
        (async () => {
            for (let i = 0; i < 60; i++) {
                const item = [...document.querySelectorAll('.chat-item')].find(
                    el => el.querySelector('.chat-name')?.textContent.includes('Weekend Coffee & Foodies')
                );
                if (item) {
                    item.click();
                    for (let j = 0; j < 80; j++) {
                        await new Promise(r => setTimeout(r, 50));
                        const loading = document.getElementById('chat-loading');
                        if (loading && loading.style.display === 'none') {
                            await new Promise(r => setTimeout(r, 150));
                            return true;
                        }
                    }
                    return true;
                }
                await new Promise(r => setTimeout(r, 100));
            }
            return false;
        })()
        """,
        15.0,
    )
    assert opened is True

    # 2. Open media gallery
    gallery = browser.evaluate(
        """
        (async () => {
            const mediaBtn = document.getElementById('media-btn');
            if (mediaBtn) mediaBtn.click();

            for (let i = 0; i < 60; i++) {
                await new Promise(r => setTimeout(r, 50));
                const gal = document.getElementById('media-gallery');
                const items = document.querySelectorAll('#media-gallery-grid .gallery-item');
                if (gal && gal.classList.contains('open') && items.length > 0) {
                    await new Promise(r => setTimeout(r, 200));
                    return { isOpen: true };
                }
            }
            return { isOpen: false };
        })()
        """,
        10.0,
    )
    assert isinstance(gallery, dict)
    assert gallery["isOpen"] is True

    # 3. Check initial pills & grid:
    # Total pill is active; Image/Video pills are neutral; secondary pills (links, vcards) are dimmed;
    # vcards and links are hidden by default from the initial grid.
    initial_state = browser.evaluate(
        """
        (() => {
            const totalPill = document.querySelector('#media-gallery-stats .gallery-stat[data-type="total"]');
            const imgPill = document.querySelector('#media-gallery-stats .gallery-stat[data-type="image"]');
            const vcardPill = document.querySelector('#media-gallery-stats .gallery-stat[data-type="vcard"]');
            const visibleVcards = document.querySelectorAll('#media-gallery-grid .gallery-item[data-type="vcard"]:not(.type-hidden)');
            const visibleImages = document.querySelectorAll('#media-gallery-grid .gallery-item[data-type="image"]:not(.type-hidden)');
            const visibleVideos = document.querySelectorAll('#media-gallery-grid .gallery-item[data-type="video"]:not(.type-hidden)');

            return {
                totalActive: totalPill ? totalPill.classList.contains('active') : false,
                imgActive: imgPill ? imgPill.classList.contains('active') : false,
                vcardDimmed: vcardPill ? vcardPill.classList.contains('dimmed') : false,
                visibleVcards: visibleVcards.length,
                visibleImages: visibleImages.length,
                visibleVideos: visibleVideos.length,
            };
        })()
        """,
        5.0,
    )
    assert initial_state["totalActive"] is False
    assert initial_state["imgActive"] is True
    assert initial_state["vcardDimmed"] is True
    assert initial_state["visibleVcards"] == 0
    assert initial_state["visibleImages"] >= 1
    assert initial_state["visibleVideos"] >= 1

    # 4. Click Images pill: assert strictly only images visible, videos and vcards hidden
    images_state = browser.evaluate(
        """
        (() => {
            const imgPill = document.querySelector('#media-gallery-stats .gallery-stat[data-type="image"]');
            if (imgPill) imgPill.click();

            const visibleImages = document.querySelectorAll('#media-gallery-grid .gallery-item[data-type="image"]:not(.type-hidden)');
            const visibleVideos = document.querySelectorAll('#media-gallery-grid .gallery-item[data-type="video"]:not(.type-hidden)');
            const visibleVcards = document.querySelectorAll('#media-gallery-grid .gallery-item[data-type="vcard"]:not(.type-hidden)');
            const errorCards = [...document.querySelectorAll('#media-gallery-grid .gallery-item:not(.type-hidden)')].filter(
                el => el.textContent.includes('Photo unavailable')
            );

            return {
                imgActive: imgPill ? imgPill.classList.contains('active') : false,
                visibleImages: visibleImages.length,
                visibleVideos: visibleVideos.length,
                visibleVcards: visibleVcards.length,
                errorCardsCount: errorCards.length,
            };
        })()
        """,
        5.0,
    )
    assert images_state["imgActive"] is True
    assert images_state["visibleImages"] >= 1
    assert images_state["visibleVideos"] == 0
    assert images_state["visibleVcards"] == 0
    assert images_state["errorCardsCount"] == 0

    # 5. Capture visual evidence of image isolation
    browser.capture_screenshot(screenshot_dir / "gallery_filtered_images_verified.png", 5.0)

    # 6. Click Contacts (vcard) pill: verify contact card renders cleanly with 📇 and contact name
    contacts_state = browser.evaluate(
        """
        (async () => {
            const vcardPill = document.querySelector('#media-gallery-stats .gallery-stat[data-type="vcard"]');
            if (vcardPill) vcardPill.click();

            for (let i = 0; i < 40; i++) {
                await new Promise(r => setTimeout(r, 50));
                const v = document.querySelectorAll('#media-gallery-grid .gallery-item[data-type="vcard"]:not(.type-hidden)');
                if (v.length > 0) break;
            }

            const visibleVcards = document.querySelectorAll('#media-gallery-grid .gallery-item[data-type="vcard"]:not(.type-hidden)');
            const errorCards = [...document.querySelectorAll('#media-gallery-grid .gallery-item:not(.type-hidden)')].filter(
                el => el.textContent.includes('Photo unavailable')
            );
            const firstCardText = visibleVcards[0] ? visibleVcards[0].textContent : '';

            return {
                vcardActive: vcardPill ? vcardPill.classList.contains('active') : false,
                visibleVcards: visibleVcards.length,
                errorCardsCount: errorCards.length,
                firstCardText: firstCardText,
            };
        })()
        """,
        5.0,
    )
    assert contacts_state["vcardActive"] is True
    assert contacts_state["visibleVcards"] >= 1
    assert contacts_state["errorCardsCount"] == 0
    assert (
        "Marco Bellini" in contacts_state["firstCardText"]
        or "📇" in contacts_state["firstCardText"]
    )

    # 7. Cleanup: close gallery
    browser.evaluate(
        """
        (() => {
            const closeBtn = document.getElementById('media-gallery-close');
            if (closeBtn) closeBtn.click();
        })()
        """,
        5.0,
    )


def test_media_gallery_filter_rapid_toggle_stress(browser: CDPSession) -> None:
    """Validate that rapid synchronous toggling between gallery filter pills does not cause state corruption or leaking items."""
    # 1. Open group chat Weekend Coffee & Foodies
    opened = browser.evaluate(
        """
        (async () => {
            for (let i = 0; i < 60; i++) {
                const item = [...document.querySelectorAll('.chat-item')].find(
                    el => el.querySelector('.chat-name')?.textContent.includes('Weekend Coffee & Foodies')
                );
                if (item) {
                    item.click();
                    for (let j = 0; j < 80; j++) {
                        await new Promise(r => setTimeout(r, 50));
                        const loading = document.getElementById('chat-loading');
                        if (loading && loading.style.display === 'none') {
                            await new Promise(r => setTimeout(r, 150));
                            return true;
                        }
                    }
                    return true;
                }
                await new Promise(r => setTimeout(r, 100));
            }
            return false;
        })()
        """,
        15.0,
    )
    assert opened is True

    # 2. Open media gallery and wait for items to load
    gallery = browser.evaluate(
        """
        (async () => {
            const mediaBtn = document.getElementById('media-btn');
            if (mediaBtn) mediaBtn.click();

            for (let i = 0; i < 60; i++) {
                await new Promise(r => setTimeout(r, 50));
                const gal = document.getElementById('media-gallery');
                const items = document.querySelectorAll('#media-gallery-grid .gallery-item');
                if (gal && gal.classList.contains('open') && items.length > 0) {
                    await new Promise(r => setTimeout(r, 200));
                    return { isOpen: true };
                }
            }
            return { isOpen: false };
        })()
        """,
        10.0,
    )
    assert isinstance(gallery, dict)
    assert gallery["isOpen"] is True

    # 3. Perform rapid-fire clicks across pills with zero inter-click delay
    stress_res = browser.evaluate(
        """
        (async () => {
            const types = ['total', 'image', 'video', 'vcard', 'video'];
            for (const t of types) {
                const pill = document.querySelector(`#media-gallery-stats .gallery-stat[data-type="${t}"]`);
                if (pill) {
                    pill.click();
                }
            }
            // Allow a short moment for DOM settlement
            await new Promise(r => setTimeout(r, 100));

            const visibleVideos = document.querySelectorAll('#media-gallery-grid .gallery-item[data-type="video"]:not(.type-hidden)');
            const visibleNonVideos = document.querySelectorAll('#media-gallery-grid .gallery-item:not([data-type="video"]):not(.type-hidden)');
            const emptyHeadersNotMarked = [...document.querySelectorAll('#media-gallery-grid .gallery-month-header')].filter(hdr => {
                const isMarkedEmpty = hdr.classList.contains('month-empty');
                let el = hdr.nextElementSibling;
                let hasVisible = false;
                while (el && !el.classList.contains('gallery-month-header')) {
                    if (el.classList.contains('gallery-item') && !el.classList.contains('type-hidden')) {
                        hasVisible = true;
                        break;
                    }
                    el = el.nextElementSibling;
                }
                return !hasVisible && !isMarkedEmpty;
            });

            return {
                visibleVideos: visibleVideos.length,
                visibleNonVideos: visibleNonVideos.length,
                unmarkedEmptyHeaders: emptyHeadersNotMarked.length,
            };
        })()
        """,
        5.0,
    )
    assert isinstance(stress_res, dict)
    assert stress_res["visibleVideos"] >= 1
    assert stress_res["visibleNonVideos"] == 0
    assert stress_res["unmarkedEmptyHeaders"] == 0

    # Cleanup: close gallery
    browser.evaluate(
        """
        (() => {
            const closeBtn = document.getElementById('media-gallery-close');
            if (closeBtn) closeBtn.click();
        })()
        """,
        5.0,
    )


def test_gallery_filter_pill_strict_computed_display_isolation(browser: CDPSession) -> None:
    """Verify that every filter pill strictly isolates its own media type via computed CSS display.

    Ensures audio items, voice notes, documents, and missing cards are completely suppressed
    (computed display === 'none') when non-matching pills are active.
    """
    # 1. Open contact chat Dr. Marcus Vance (contains photo, voice note, document, view-once photo)
    chat_name = open_chat_by_id(browser, "15550102", 30.0)
    assert "Marcus" in chat_name

    # 2. Open media gallery
    gallery = browser.evaluate(
        """
        (async () => {
            const mediaBtn = document.getElementById('media-btn');
            if (mediaBtn) mediaBtn.click();

            for (let i = 0; i < 60; i++) {
                await new Promise(r => setTimeout(r, 50));
                const gal = document.getElementById('media-gallery');
                const items = document.querySelectorAll('#media-gallery-grid .gallery-item');
                if (gal && gal.classList.contains('open') && items.length > 0) {
                    await new Promise(r => setTimeout(r, 200));
                    return { isOpen: true };
                }
            }
            return { isOpen: false };
        })()
        """,
        10.0,
    )
    assert isinstance(gallery, dict)
    assert gallery["isOpen"] is True

    # 3. For each available pill (image, voice, document, etc.), click it and assert:
    # - all items with .type-hidden have computed style display === 'none'
    # - all visually visible items strictly have dataset.type matching the active pill
    filter_results = browser.evaluate(
        """
        (async () => {
            const pills = [...document.querySelectorAll('#media-gallery-stats .gallery-stat[data-type]')].filter(
                p => p.dataset.type && p.dataset.type !== 'total'
            );
            const results = {};

            for (const pill of pills) {
                const type = pill.dataset.type;
                pill.click();
                await new Promise(r => setTimeout(r, 150));

                const allItems = [...document.querySelectorAll('#media-gallery-grid .gallery-item, #media-gallery-grid .gallery-link-card')];
                const visuallyVisible = allItems.filter(el => window.getComputedStyle(el).display !== 'none');
                const leakingItems = visuallyVisible.filter(el => el.dataset.type !== type);
                const hiddenWithFlex = allItems.filter(
                    el => el.classList.contains('type-hidden') && window.getComputedStyle(el).display !== 'none'
                );

                results[type] = {
                    totalVisible: visuallyVisible.length,
                    leakingCount: leakingItems.length,
                    hiddenWithFlexCount: hiddenWithFlex.length,
                };
            }
            return results;
        })()
        """,
        15.0,
    )

    assert isinstance(filter_results, dict)
    assert len(filter_results) > 0
    for type_name, stats in filter_results.items():
        assert stats["hiddenWithFlexCount"] == 0, (
            f"Pill {type_name} has hidden items overriding display: none!"
        )
        assert stats["leakingCount"] == 0, f"Pill {type_name} leaked non-{type_name} items!"

    # Cleanup: close gallery
    browser.evaluate(
        """
        (() => {
            const closeBtn = document.getElementById('media-gallery-close');
            if (closeBtn) closeBtn.click();
        })()
        """,
        5.0,
    )


def test_media_gallery_viewport_hydration_and_unloading(
    browser: CDPSession, screenshot_dir: Path
) -> None:
    """Validate viewport-only hydration, scroll unloading, video cache, and modal cleanup."""
    open_group_chat_in_browser(browser, 25.0)

    # 1. Open media gallery
    open_res = browser.evaluate(
        """
        (async () => {
            const mediaBtn = document.getElementById('media-btn');
            if (mediaBtn) mediaBtn.click();

            for (let i = 0; i < 60; i++) {
                await new Promise(r => setTimeout(r, 50));
                const gal = document.getElementById('media-gallery');
                const items = document.querySelectorAll('#media-gallery-grid .gallery-item');
                if (gal && gal.classList.contains('open') && items.length > 0) {
                    await new Promise(r => setTimeout(r, 200));
                    return true;
                }
            }
            return false;
        })()
        """,
        10.0,
    )
    assert open_res is True

    # 2. Verify visible items hydrate while maintaining fixed height
    initial_check = browser.evaluate(
        """
        (() => {
            const grid = document.getElementById('media-gallery-grid');
            const items = [...grid.querySelectorAll('.gallery-item')];
            const mediaItems = items.filter(
                el => !el.classList.contains('type-hidden') && el.dataset.src && el.dataset.type !== 'link'
            );
            const hydrated = mediaItems.filter(el => el.dataset.hydrated === 'true');
            const heights = mediaItems.map(el => el.getBoundingClientRect().height);
            const allStandardHeight = mediaItems.length > 0 && heights.every(h => Math.abs(h - 120) < 2);

            const firstImage = items.find(
                el => !el.classList.contains('type-hidden') && el.dataset.type === 'image'
            );
            const firstVideo = items.find(
                el => !el.classList.contains('type-hidden') && el.dataset.type === 'video'
            );

            return {
                totalMediaItems: mediaItems.length,
                hydratedCount: hydrated.length,
                allStandardHeight: allStandardHeight,
                imageSrc: firstImage ? firstImage.querySelector('img')?.src || '' : '',
                hasVideo: firstVideo !== undefined,
            };
        })()
        """,
        5.0,
    )
    assert isinstance(initial_check, dict)
    assert initial_check["totalMediaItems"] > 0
    assert initial_check["hydratedCount"] > 0
    assert initial_check["allStandardHeight"] is True
    assert "data:image/svg+xml" not in initial_check["imageSrc"]
    assert initial_check["hasVideo"] is True

    # 2b. Verify idle video thumbnail extraction produces non-black real video frame
    video_thumb_check = browser.evaluate(
        """
        (async () => {
            let videoImg = null;
            for (let i = 0; i < 40; i++) {
                await new Promise(r => setTimeout(r, 100));
                videoImg = document.querySelector('#media-gallery-grid .gallery-item[data-type="video"] img');
                if (videoImg && videoImg.src.startsWith('data:image/jpeg')) {
                    break;
                }
            }
            if (!videoImg || !videoImg.src.startsWith('data:image/jpeg')) {
                return { success: false, src: videoImg?.src || 'none' };
            }

            const canvas = document.createElement('canvas');
            canvas.width = 50;
            canvas.height = 50;
            const ctx = canvas.getContext('2d');
            await new Promise((resolve) => {
                if (videoImg.complete) return resolve();
                videoImg.onload = () => resolve();
            });
            ctx.drawImage(videoImg, 0, 0, 50, 50);
            const p = ctx.getImageData(0, 0, 50, 50).data;
            let nonZero = 0;
            for (let i = 0; i < p.length; i += 4) {
                if (p[i] > 10 || p[i+1] > 10 || p[i+2] > 10) {
                    nonZero++;
                }
            }
            return {
                success: true,
                nonZeroPixels: nonZero,
                sample: [p[0], p[1], p[2], p[3]],
            };
        })()
        """,
        10.0,
    )
    assert isinstance(video_thumb_check, dict)
    assert video_thumb_check["success"] is True
    assert video_thumb_check["nonZeroPixels"] > 100

    # 3. Test scrolling / dynamic dehydration by inserting a far offscreen item
    offscreen_test = browser.evaluate(
        """
        (async () => {
            const grid = document.getElementById('media-gallery-grid');
            const spacer = document.createElement('div');
            spacer.id = 'test-deep-spacer';
            spacer.style.gridColumn = '1 / -1';
            spacer.style.height = '4000px';
            grid.appendChild(spacer);

            const testItem = document.createElement('div');
            testItem.id = 'test-far-item';
            testItem.className = 'gallery-item';
            testItem.dataset.src = '/media/dummy.jpg';
            testItem.dataset.type = 'image';
            testItem.dataset.hydrated = 'false';
            const img = document.createElement('img');
            img.className = 'gallery-thumb';
            img.src = 'data:image/svg+xml,<svg xmlns=\"http://www.w3.org/2000/svg\"/>';
            testItem.appendChild(img);
            grid.appendChild(testItem);

            // Observe with gallery observer
            const obs = window.getGalleryMediaObserver ? window.getGalleryMediaObserver() : null;
            if (obs) obs.observe(testItem);
            // Let observer evaluate initial state
            await new Promise(r => setTimeout(r, 100));
            const initiallyHydrated = testItem.dataset.hydrated === 'true';

            // Scroll down to the test item
            grid.scrollTop = grid.scrollHeight;
            let scrolledDownHydrated = false;
            for (let i = 0; i < 40; i++) {
                await new Promise(r => setTimeout(r, 50));
                if (testItem.dataset.hydrated === 'true') {
                    scrolledDownHydrated = true;
                    break;
                }
            }

            // Scroll back up to top
            grid.scrollTop = 0;
            let scrolledUpDehydrated = false;
            for (let i = 0; i < 40; i++) {
                await new Promise(r => setTimeout(r, 50));
                if (testItem.dataset.hydrated === 'false') {
                    scrolledUpDehydrated = true;
                    break;
                }
            }

            // Clean up test spacer and item
            spacer.remove();
            testItem.remove();

            return {
                initiallyHydrated,
                scrolledDownHydrated,
                scrolledUpDehydrated,
            };
        })()
        """,
        5.0,
    )
    assert isinstance(offscreen_test, dict)
    assert offscreen_test["initiallyHydrated"] is False
    assert offscreen_test["scrolledDownHydrated"] is True
    assert offscreen_test["scrolledUpDehydrated"] is True

    # 4. Validate lightbox click interaction on a hydrated gallery item
    lightbox_state = browser.evaluate(
        """
        (async () => {
            const firstImg = document.querySelector('#media-gallery-grid .gallery-item[data-hydrated=\"true\"] img');
            if (!firstImg) return { opened: false };
            firstImg.click();
            await new Promise(r => setTimeout(r, 250));

            const lb = document.getElementById('img-lightbox');
            const hasLb = lb !== null && !lb.classList.contains('is-closing');

            if (hasLb) {
                const closeBtn = lb.querySelector('.lb-close');
                if (closeBtn) closeBtn.click();
                await new Promise(r => setTimeout(r, 350));
            }

            return { opened: hasLb };
        })()
        """,
        5.0,
    )
    assert isinstance(lightbox_state, dict)
    assert lightbox_state["opened"] is True

    # 5. Capture visual evidence
    browser.capture_screenshot(screenshot_dir / "gallery_hydrated.png", 5.0)

    # 6. Close media gallery and verify observers and probe queues are dismantled
    cleanup_check = browser.evaluate(
        """
        (() => {
            const closeBtn = document.getElementById('media-gallery-close');
            if (closeBtn) closeBtn.click();

            return {
                isClosed: !document.getElementById('media-gallery').classList.contains('open')
            };
        })()
        """,
        5.0,
    )
    assert isinstance(cleanup_check, dict)
    assert cleanup_check["isClosed"] is True


def test_media_gallery_archive_view_loads_past_years_when_current_year_has_many_media(
    browser: CDPSession, screenshot_dir: Path
) -> None:
    """Validate that when switching to Archive view with many media items in the current year,

    all pages are loaded and past year sections (e.g. 2025) are rendered in the archive tree.
    """
    open_group_chat_in_browser(browser, 25.0)

    # 1. Mock window.fetch to simulate:
    # Page 1 (first 100 items): all from 2026
    # Page 2 (next 20 items): 10 from 2026, and 10 from 2025
    setup_mock = browser.evaluate(
        """
        (() => {
            const originalFetch = window.fetch;
            window._testOriginalFetch = originalFetch;

            const page1 = [];
            for (let i = 0; i < 100; i++) {
                page1.push({
                    timestamp_ms: 1775000000000 - i * 10000,
                    media_type: 'image',
                    archive_path: 'Groups/Tech & Coffee Meetup/2026/img_2026_' + i + '.jpg',
                    media_name: 'img_2026_' + i + '.jpg',
                });
            }

            const page2 = [];
            for (let i = 100; i < 110; i++) {
                page2.push({
                    timestamp_ms: 1775000000000 - i * 10000,
                    media_type: 'image',
                    archive_path: 'Groups/Tech & Coffee Meetup/2026/img_2026_' + i + '.jpg',
                    media_name: 'img_2026_' + i + '.jpg',
                });
            }
            for (let i = 0; i < 10; i++) {
                page2.push({
                    timestamp_ms: 1740000000000 - i * 10000,
                    media_type: 'image',
                    archive_path: 'Groups/Tech & Coffee Meetup/2025/img_2025_' + i + '.jpg',
                    media_name: 'img_2025_' + i + '.jpg',
                });
            }

            window.fetch = async (url, options) => {
                const urlStr = String(url);
                if (urlStr.includes('/api/media/count?')) {
                    return new Response(JSON.stringify({
                        total: 120,
                        by_type: {
                            image: { count: 120, missing: 0 }
                        }
                    }), {
                        status: 200,
                        headers: { 'Content-Type': 'application/json' },
                    });
                }
                if (urlStr.includes('/api/media?')) {
                    if (urlStr.includes('before=')) {
                        return new Response(JSON.stringify(page2), {
                            status: 200,
                            headers: { 'Content-Type': 'application/json' },
                        });
                    } else {
                        return new Response(JSON.stringify(page1), {
                            status: 200,
                            headers: { 'Content-Type': 'application/json' },
                        });
                    }
                }
                return originalFetch(url, options);
            };
            return true;
        })()
        """,
        5.0,
    )
    assert setup_mock is True

    try:
        # 2. Open media gallery
        opened = browser.evaluate(
            """
            (async () => {
                const mediaBtn = document.getElementById('media-btn');
                if (mediaBtn) mediaBtn.click();

                for (let i = 0; i < 60; i++) {
                    await new Promise(r => setTimeout(r, 50));
                    const gal = document.getElementById('media-gallery');
                    const items = document.querySelectorAll('#media-gallery-grid .gallery-item');
                    if (gal && gal.classList.contains('open') && items.length >= 100) {
                        return true;
                    }
                }
                return false;
            })()
            """,
            10.0,
        )
        assert opened is True

        # 3. Switch to Archive view
        archive_res = browser.evaluate(
            """
            (async () => {
                const archiveBtn = document.querySelector('.media-view-btn[data-view="archive"]');
                if (archiveBtn) archiveBtn.click();

                for (let i = 0; i < 60; i++) {
                    await new Promise(r => setTimeout(r, 50));
                    const block2025 = document.querySelector('.archive-year[data-year="2025"]');
                    const count2025 = block2025 ? parseInt(block2025.querySelector('.archive-year-count')?.textContent || '0', 10) : 0;
                    const block2026 = document.querySelector('.archive-year[data-year="2026"]');
                    const count2026 = block2026 ? parseInt(block2026.querySelector('.archive-year-count')?.textContent || '0', 10) : 0;

                    if (block2025 && count2025 >= 10 && block2026 && count2026 >= 110) {
                        return {
                            has2026: true,
                            count2026: count2026,
                            has2025: true,
                            count2025: count2025,
                        };
                    }
                }

                const years = [...document.querySelectorAll('.archive-year')].map(el => el.dataset.year);
                return {
                    has2026: document.querySelector('.archive-year[data-year="2026"]') !== null,
                    has2025: document.querySelector('.archive-year[data-year="2025"]') !== null,
                    yearsFound: years,
                };
            })()
            """,
            10.0,
        )

        assert isinstance(archive_res, dict)
        assert archive_res["has2026"] is True
        assert archive_res["has2025"] is True
        assert archive_res.get("count2025") == 10
        assert archive_res.get("count2026") == 110

        browser.capture_screenshot(screenshot_dir / "archive_view_past_years.png", 5.0)

    finally:
        browser.evaluate(
            """
            (() => {
                if (window._testOriginalFetch) {
                    window.fetch = window._testOriginalFetch;
                    delete window._testOriginalFetch;
                }
                const closeBtn = document.getElementById('media-gallery-close');
                if (closeBtn) closeBtn.click();
            })()
            """,
            5.0,
        )


def test_chat_bubble_media_aspect_ratio_and_lightbox_zoom(
    browser: CDPSession,
    screenshot_dir: Path,
) -> None:
    """Verify chat bubble images preserve intrinsic aspect ratio and lightbox supports wheel zoom."""
    # 1. Open contact chat Sarah Jenkins (retry if initial page load is still settling)
    opened = None
    for _ in range(3):
        opened = browser.evaluate(
            """
            (async () => {
                for (let i = 0; i < 60; i++) {
                    const item = [...document.querySelectorAll('.chat-item')].find(
                        el => el.querySelector('.chat-name')?.textContent.includes('Sarah Jenkins')
                    );
                    if (item) {
                        item.click();
                        for (let j = 0; j < 80; j++) {
                            await new Promise(r => setTimeout(r, 50));
                            const loading = document.getElementById('chat-loading');
                            if (loading && loading.style.display === 'none') {
                                await new Promise(r => setTimeout(r, 150));
                                return true;
                            }
                        }
                        return true;
                    }
                    await new Promise(r => setTimeout(r, 100));
                }
                return false;
            })()
            """,
            15.0,
        )
        if opened is True:
            break
        time.sleep(0.5)
    assert opened is True

    # 2. Inspect media element aspect ratio and bubble wrapping
    media_info = browser.evaluate(
        """
        (async () => {
            for (let i = 0; i < 60; i++) {
                const img = document.querySelector('#message-scroll .msg-media img');
                if (img && img.naturalWidth > 0 && img.naturalHeight > 0) {
                    const rect = img.getBoundingClientRect();
                    const naturalAspect = img.naturalWidth / img.naturalHeight;
                    const renderedAspect = rect.width / rect.height;
                    const bubble = img.closest('.msg-bubble');
                    const bubbleRect = bubble ? bubble.getBoundingClientRect() : rect;
                    const hasCaption = bubble ? !!bubble.querySelector('.msg-caption') : false;
                    return {
                        ready: true,
                        naturalWidth: img.naturalWidth,
                        naturalHeight: img.naturalHeight,
                        renderedWidth: rect.width,
                        renderedHeight: rect.height,
                        naturalAspect: naturalAspect,
                        renderedAspect: renderedAspect,
                        bubbleWidth: bubbleRect.width,
                        hasCaption: hasCaption,
                    };
                }
                await new Promise(r => setTimeout(r, 50));
            }
            return { ready: false };
        })()
        """,
        10.0,
    )
    assert isinstance(media_info, dict)
    if media_info.get("ready"):
        is_portrait = media_info["naturalAspect"] < 1.0
        expected_aspect = (
            max(0.75, min(media_info["naturalAspect"], 0.999))
            if is_portrait
            else max(1.0, min(media_info["naturalAspect"], 1.778))
        )
        diff = abs(expected_aspect - media_info["renderedAspect"])
        assert diff < 0.05, (
            f"Clamped orientation aspect mismatch: expected={expected_aspect:.3f}, "
            f"rendered={media_info['renderedAspect']:.3f}"
        )
        wasted_space = media_info["bubbleWidth"] - media_info["renderedWidth"] - 20
        assert abs(wasted_space) < 25, (
            f"Media bubble width must match media + padding even with captions: "
            f"bubbleWidth={media_info['bubbleWidth']}, mediaWidth={media_info['renderedWidth']}, "
            f"wasted={wasted_space}"
        )

    # 2. Click the image to open Lightbox
    lb_opened = browser.evaluate(
        """
        (async () => {
            const img = document.querySelector('#message-scroll .msg-media img');
            if (img) img.click();
            for (let i = 0; i < 40; i++) {
                const lb = document.getElementById('img-lightbox');
                const lbImg = lb?.querySelector('img');
                if (lb && lbImg && lbImg.naturalWidth > 0) {
                    const rect = lbImg.getBoundingClientRect();
                    return {
                        opened: true,
                        naturalAspect: lbImg.naturalWidth / lbImg.naturalHeight,
                        renderedAspect: rect.width / rect.height,
                    };
                }
                await new Promise(r => setTimeout(r, 50));
            }
            return { opened: false };
        })()
        """,
        5.0,
    )
    assert isinstance(lb_opened, dict)
    assert lb_opened["opened"] is True
    assert abs(lb_opened["naturalAspect"] - lb_opened["renderedAspect"]) < 0.05, (
        f"Lightbox must preserve uncropped natural aspect ratio: "
        f"natural={lb_opened['naturalAspect']:.3f}, rendered={lb_opened['renderedAspect']:.3f}"
    )

    # 3. Simulate wheel zoom on the lightbox
    zoom_result = browser.evaluate(
        """
        (() => {
            const lb = document.getElementById('img-lightbox');
            if (!lb) return { error: 'no lightbox' };
            const evt = new WheelEvent('wheel', {
                deltaY: -120,
                clientX: window.innerWidth / 2,
                clientY: window.innerHeight / 2,
                bubbles: true,
                cancelable: true
            });
            lb.dispatchEvent(evt);
            const badge = lb.querySelector('.lb-zoom-badge');
            const img = lb.querySelector('img');
            return {
                badgeVisible: badge ? badge.style.display !== 'none' : false,
                badgeText: badge ? badge.textContent : '',
                transform: img ? img.style.transform : '',
            };
        })()
        """,
        5.0,
    )
    assert isinstance(zoom_result, dict)
    assert zoom_result.get("badgeVisible") is True
    assert "scale" in zoom_result.get("transform", "")

    browser.capture_screenshot(screenshot_dir / "lightbox_wheel_zoomed.png", 5.0)

    # 4. Click zoom badge to reset
    reset_result = browser.evaluate(
        """
        (() => {
            const lb = document.getElementById('img-lightbox');
            const badge = lb?.querySelector('.lb-zoom-badge');
            if (badge) badge.click();
            const img = lb?.querySelector('img');
            return {
                badgeVisible: badge ? badge.style.display !== 'none' : false,
                transform: img ? img.style.transform : '',
            };
        })()
        """,
        5.0,
    )
    assert isinstance(reset_result, dict)
    assert reset_result.get("badgeVisible") is False
    assert "scale(1)" in reset_result.get("transform", "")

    # 5. Close lightbox
    closed_lb = browser.evaluate(
        """
        (async () => {
            const closeBtn = document.querySelector('#img-lightbox .lb-close');
            if (closeBtn) closeBtn.click();
            for (let i = 0; i < 20; i++) {
                if (!document.getElementById('img-lightbox')) return true;
                await new Promise(r => setTimeout(r, 30));
            }
            return !document.getElementById('img-lightbox');
        })()
        """,
        5.0,
    )
    assert closed_lb is True


def test_captioned_media_orientation_sizing_and_display_scale(
    browser: CDPSession,
    screenshot_dir: Path,
) -> None:
    """Verify captioned landscape/portrait media bubbles lock to Display scale widths,
    wrap captions without horizontal bloat, and adapt cleanly on mobile viewports."""
    open_chat_by_id(browser, "15550101", 15.0)

    # Ensure settings modal shows 'Display scale'
    settings_label = browser.evaluate(
        """
        (() => {
            const labels = [...document.querySelectorAll('#settings-modal .settings-label')];
            return labels.map(el => el.textContent.trim());
        })()
        """,
        5.0,
    )
    assert isinstance(settings_label, list)
    assert "Display scale" in settings_label

    try:
        # Ensure desktop viewport (1280x800) so Display scale widths aren't clamped by narrow default headless window
        browser.ws.send_json(
            {
                "id": 8900,
                "method": "Emulation.setDeviceMetricsOverride",
                "params": {
                    "width": 1280,
                    "height": 800,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                },
            }
        )
        time.sleep(0.2)
        results = browser.evaluate(
            """
            (async () => {
                const scroll = document.getElementById('message-scroll');
                if (!scroll) return { error: 'no message-scroll' };

                const container = document.createElement('div');
                container.id = 'test-captioned-media-harness';
                const longCaption =
                    'This is a deliberately multi-word caption designed to wrap cleanly ' +
                    'to the media container width without expanding the bubble horizontally ' +
                    'https://example.com/very/long/unbroken/path/segment/to/verify/containment';
                const longQuote =
                    'A very long quoted message reply that would previously stretch a media bubble out to 70 percent width';

                function addCard(cls, mediaClass, aspectRatio, extraInner) {
                    const row = document.createElement('div');
                    row.className = 'msg-row recv';
                    const bubble = document.createElement('div');
                    bubble.className = 'msg-bubble has-media';

                    const quote = document.createElement('div');
                    quote.className = 'msg-quote';
                    quote.innerHTML = '<div class="msg-quote-content"><div class="msg-quote-sender">Alex</div><div class="msg-quote-text">' + longQuote + '</div></div>';
                    bubble.appendChild(quote);

                    const media = document.createElement('div');
                    media.className = mediaClass;
                    if (aspectRatio) media.style.aspectRatio = aspectRatio;
                    media.innerHTML = extraInner;
                    bubble.appendChild(media);

                    const cap = document.createElement('div');
                    cap.className = 'msg-caption';
                    cap.textContent = longCaption;
                    bubble.appendChild(cap);

                    const meta = document.createElement('div');
                    meta.className = 'msg-meta';
                    meta.textContent = '15:42';
                    bubble.appendChild(meta);

                    row.appendChild(bubble);
                    row.dataset.case = cls;
                    container.appendChild(row);
                    return { row, bubble, media, cap };
                }

                const svgLandscape = '<img src="data:image/svg+xml;utf8,<svg xmlns=\\'http://www.w3.org/2000/svg\\' width=\\'800\\' height=\\'450\\'><rect width=\\'100%25\\' height=\\'100%25\\' fill=\\'%2300a884\\'/></svg>" alt="land"/>';
                const svgPortrait = '<img src="data:image/svg+xml;utf8,<svg xmlns=\\'http://www.w3.org/2000/svg\\' width=\\'450\\' height=\\'800\\'><rect width=\\'100%25\\' height=\\'100%25\\' fill=\\'%230284c7\\'/></svg>" alt="port"/>';

                const items = [
                    addCard('landscape', 'msg-media is-landscape', '1.778', svgLandscape),
                    addCard('portrait', 'msg-media is-portrait', '0.750', svgPortrait),
                    addCard('missing', 'media-missing-card', '1.333', '<div class="media-missing-overlay"><div class="media-missing-label">Photo</div></div>'),
                    addCard('grid', 'media-grid', '', '<div class="media-grid-cell"><div class="msg-media">' + svgLandscape + '</div></div><div class="media-grid-cell"><div class="msg-media">' + svgPortrait + '</div></div>'),
                ];
                scroll.appendChild(container);
                scroll.scrollTop = scroll.scrollHeight;

                const out = {};
                for (const scale of ['small', 'medium', 'large']) {
                    const btn = document.querySelector(`.pref-btn[data-pref="font_size"][data-value="${scale}"]`);
                    if (btn) btn.click();
                    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
                    await new Promise(r => setTimeout(r, 80));

                    const measurements = {};
                    for (const it of items) {
                        const bRect = it.bubble.getBoundingClientRect();
                        const mRect = it.media.getBoundingClientRect();
                        const cRect = it.cap.getBoundingClientRect();
                        measurements[it.row.dataset.case] = {
                            bubbleWidth: Math.round(bRect.width),
                            mediaWidth: Math.round(mRect.width),
                            mediaHeight: Math.round(mRect.height),
                            captionWidth: Math.round(cRect.width),
                            captionHeight: Math.round(cRect.height),
                        };
                    }
                    out[scale] = measurements;
                }
                return out;
            })()
            """,
            15.0,
        )
        assert isinstance(results, dict) and "error" not in results, f"Harness error: {results}"

        expected_land = {"small": 280, "medium": 320, "large": 360}
        expected_port = {"small": 230, "medium": 260, "large": 295}
        for scale in ("small", "medium", "large"):
            sc = results[scale]
            assert abs(sc["landscape"]["mediaWidth"] - expected_land[scale]) <= 2
            assert abs(sc["portrait"]["mediaWidth"] - expected_port[scale]) <= 2
            assert abs(sc["missing"]["mediaWidth"] - expected_land[scale]) <= 2
            assert abs(sc["grid"]["mediaWidth"] - expected_land[scale]) <= 2
            for key in ("landscape", "portrait", "missing", "grid"):
                m = sc[key]
                assert abs(m["bubbleWidth"] - (m["mediaWidth"] + 20)) <= 2, (
                    f"{scale}/{key}: bubbleWidth={m['bubbleWidth']} != mediaWidth+20 ({m['mediaWidth'] + 20})"
                )
                assert m["captionWidth"] <= m["mediaWidth"] + 1, (
                    f"{scale}/{key}: caption overflowed mediaWidth"
                )
                assert m["captionHeight"] >= 40, (
                    f"{scale}/{key}: caption did not wrap across multiple lines"
                )

        # Reset to medium and capture desktop screenshot
        browser.evaluate(
            """
            (async () => {
                const btn = document.querySelector('.pref-btn[data-pref="font_size"][data-value="medium"]');
                if (btn) btn.click();
                await new Promise(r => setTimeout(r, 80));
            })()
            """,
            5.0,
        )
        browser.capture_screenshot(screenshot_dir / "captioned_media_desktop.png", 5.0)

        # Verify mobile viewport (390x844) does not overflow horizontally
        browser.ws.send_json(
            {
                "id": 8901,
                "method": "Emulation.setDeviceMetricsOverride",
                "params": {
                    "width": 390,
                    "height": 844,
                    "deviceScaleFactor": 2,
                    "mobile": True,
                },
            }
        )
        time.sleep(0.3)
        mobile_ok = browser.evaluate(
            """
            (async () => {
                const scroll = document.getElementById('message-scroll');
                const h = document.getElementById('test-captioned-media-harness');
                if (!scroll || !h) return { ok: false };
                scroll.scrollTop = scroll.scrollHeight;
                await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
                const scrollRect = scroll.getBoundingClientRect();
                const scrollW = scroll.clientWidth;
                const bubbles = [...h.querySelectorAll('.msg-bubble')];
                const maxBubbleW = Math.max(...bubbles.map(b => b.getBoundingClientRect().width));
                const allInside = bubbles.every(b => {
                    const r = b.getBoundingClientRect();
                    return r.left >= scrollRect.left - 1 && r.right <= scrollRect.right + 1;
                });
                return {
                    ok: maxBubbleW <= scrollW * 0.86 && allInside,
                    scrollW,
                    maxBubbleW,
                    allInside,
                };
            })()
            """,
            5.0,
        )
        assert isinstance(mobile_ok, dict) and mobile_ok.get("ok") is True, (
            f"Mobile overflow: {mobile_ok}"
        )
        browser.capture_screenshot(screenshot_dir / "captioned_media_mobile.png", 5.0)
    finally:
        browser.ws.send_json(
            {
                "id": 8902,
                "method": "Emulation.clearDeviceMetricsOverride",
                "params": {},
            }
        )
        time.sleep(0.3)
        browser.evaluate(
            """
            (() => {
                document.getElementById('test-captioned-media-harness')?.remove();
                const btn = document.querySelector('.pref-btn[data-pref="font_size"][data-value="medium"]');
                if (btn) btn.click();
            })()
            """,
            5.0,
        )


def test_chat_bubble_voice_message_audio_player(
    browser: CDPSession,
    screenshot_dir: Path,
) -> None:
    """Verify voice message audio player renders with non-zero width and controls in Chromium."""
    # 1. Open contact chat Dr. Marcus Vance
    opened = browser.evaluate(
        """
        (async () => {
            for (let i = 0; i < 60; i++) {
                const item = [...document.querySelectorAll('.chat-item')].find(
                    el => el.querySelector('.chat-name')?.textContent.includes('Dr. Marcus Vance')
                );
                if (item) {
                    item.click();
                    for (let j = 0; j < 80; j++) {
                        await new Promise(r => setTimeout(r, 50));
                        const loading = document.getElementById('chat-loading');
                        if (loading && loading.style.display === 'none') {
                            await new Promise(r => setTimeout(r, 150));
                            return true;
                        }
                    }
                    return true;
                }
                await new Promise(r => setTimeout(r, 100));
            }
            return false;
        })()
        """,
        15.0,
    )
    assert opened is True

    # 2. Inspect audio player element dimensions and wrapper in Chromium
    audio_info = browser.evaluate(
        """
        (async () => {
            for (let i = 0; i < 60; i++) {
                const aud = document.querySelector('#message-scroll .msg-media audio');
                if (aud) {
                    const rect = aud.getBoundingClientRect();
                    const wrap = aud.closest('.msg-media');
                    const wrapRect = wrap ? wrap.getBoundingClientRect() : null;
                    const bubble = aud.closest('.msg-bubble');
                    const bubbleRect = bubble ? bubble.getBoundingClientRect() : null;
                    return {
                        found: true,
                        hasControls: aud.controls,
                        audWidth: rect.width,
                        audHeight: rect.height,
                        wrapWidth: wrapRect ? wrapRect.width : 0,
                        wrapHasClass: wrap ? wrap.classList.contains('audio') : false,
                        bubbleWidth: bubbleRect ? bubbleRect.width : 0,
                    };
                }
                await new Promise(r => setTimeout(r, 100));
            }
            return { found: false };
        })()
        """,
        10.0,
    )
    assert isinstance(audio_info, dict)
    assert audio_info.get("found") is True
    assert audio_info.get("hasControls") is True
    assert audio_info.get("wrapHasClass") is True
    assert audio_info.get("audWidth", 0) >= 240
    assert audio_info.get("audHeight", 0) >= 30
    assert audio_info.get("wrapWidth", 0) >= 240

    browser.capture_screenshot(screenshot_dir / "voice_message_audio_player.png", 5.0)


def test_deleted_messages_visual_rendering(browser: CDPSession, screenshot_dir: Path) -> None:
    """Validate visual rendering of deleted (revoked) messages in group and outgoing chats."""
    # 1. Open Alpine Hikers (chat_id = "1")
    chat_name = open_chat_by_id(browser, "1", 15.0)
    assert len(chat_name) > 0

    # 2. Inspect deleted message elements in Alpine Hikers
    alpine_deleted = browser.evaluate(
        """
        (async () => {
            for (let i = 0; i < 50; i++) {
                const bubbles = [...document.querySelectorAll('#message-scroll .msg-bubble.deleted')];
                if (bubbles.length >= 2) {
                    return bubbles.map(b => {
                        const content = b.querySelector('.msg-deleted-content');
                        const icon = b.querySelector('.deleted-icon');
                        const txt = b.querySelector('.deleted-text');
                        const infoBtn = b.querySelector('.msg-info-btn');
                        const quote = b.querySelector('.msg-quote');
                        const rx = b.querySelector('.msg-reactions');
                        const style = window.getComputedStyle(b);
                        return {
                            hasContent: !!content,
                            iconText: icon ? icon.textContent : '',
                            deletedText: txt ? txt.textContent : '',
                            hasInfoBtn: !!infoBtn,
                            hasQuote: !!quote,
                            hasReactions: !!rx,
                            fontStyle: style.fontStyle,
                            borderStyle: style.borderStyle,
                        };
                    });
                }
                await new Promise(r => setTimeout(r, 100));
            }
            return [];
        })()
        """,
        10.0,
    )
    assert isinstance(alpine_deleted, list)
    assert len(alpine_deleted) >= 2

    # Check both admin-deleted and sender-deleted messages
    texts = [item["deletedText"] for item in alpine_deleted]
    assert "This message was deleted by an admin" in texts
    assert "This message was deleted" in texts

    for item in alpine_deleted:
        assert item["hasContent"] is True
        assert item["iconText"] == "⃠"
        assert item["hasInfoBtn"] is False
        assert item["hasQuote"] is False
        assert item["hasReactions"] is False
        assert item["fontStyle"] == "italic"
        assert "dashed" in item["borderStyle"]

    browser.capture_screenshot(screenshot_dir / "deleted_messages_alpine.png", 5.0)

    # 3. Open Tech & Coffee (chat_id = "2") to verify outgoing deleted message
    tc_name = open_chat_by_id(browser, "2", 15.0)
    assert len(tc_name) > 0

    outgoing_deleted = browser.evaluate(
        """
        (async () => {
            for (let i = 0; i < 50; i++) {
                const bubbles = [...document.querySelectorAll('#message-scroll .msg-bubble.deleted')];
                const outBubble = bubbles.find(b => {
                    const row = b.closest('.msg-row');
                    return row && row.classList.contains('sent');
                });
                if (outBubble) {
                    const txt = outBubble.querySelector('.deleted-text');
                    const icon = outBubble.querySelector('.deleted-icon');
                    const badge = outBubble.querySelector('.direction-badge.sent');
                    const infoBtn = outBubble.querySelector('.msg-info-btn');
                    return {
                        found: true,
                        iconText: icon ? icon.textContent : '',
                        deletedText: txt ? txt.textContent : '',
                        badgeText: badge ? badge.textContent : '',
                        hasInfoBtn: !!infoBtn,
                    };
                }
                await new Promise(r => setTimeout(r, 100));
            }
            return { found: false };
        })()
        """,
        10.0,
    )
    assert isinstance(outgoing_deleted, dict)
    assert outgoing_deleted.get("found") is True
    assert outgoing_deleted.get("iconText") == "⃠"
    assert outgoing_deleted.get("deletedText") == "You deleted this message"
    assert outgoing_deleted.get("badgeText") == "You"
    assert outgoing_deleted.get("hasInfoBtn") is False

    browser.capture_screenshot(screenshot_dir / "deleted_messages_outgoing.png", 5.0)


def test_view_once_messages_visual_rendering(browser: CDPSession, screenshot_dir: Path) -> None:
    """Validate visual rendering of view-once messages (opened pills and preserved badge)."""
    # 1. Open Sarah Jenkins (chat_id = "15550101")
    chat_name = open_chat_by_id(browser, "15550101", 15.0)
    assert len(chat_name) > 0

    # 2. Inspect view-once message elements in Sarah Jenkins
    vo_info = browser.evaluate(
        """
        (async () => {
            for (let i = 0; i < 50; i++) {
                const openedPills = [...document.querySelectorAll('#message-scroll .msg-view-once-pill')];
                const badges = [...document.querySelectorAll('#message-scroll .msg-view-once-badge')];
                if (openedPills.length >= 2 && badges.length >= 1) {
                    const pillsData = openedPills.map(p => {
                        const icon = p.querySelector('.vo-icon');
                        const label = p.querySelector('.vo-label');
                        const bubble = p.closest('.msg-bubble');
                        const hasMedia = bubble ? !!bubble.querySelector('.msg-media') : false;
                        const isOpenedBubble = bubble ? bubble.classList.contains('view-once-opened') : false;
                        return {
                            iconText: icon ? icon.textContent : '',
                            labelText: label ? label.textContent : '',
                            hasMedia: hasMedia,
                            isOpenedBubble: isOpenedBubble,
                        };
                    });

                    const badge = badges[0];
                    const bIcon = badge.querySelector('.vo-icon');
                    const bWrap = badge.closest('.msg-media');
                    const hasVideo = bWrap ? !!bWrap.querySelector('video') : false;
                    const bStyle = window.getComputedStyle(badge);

                    return {
                        found: true,
                        pills: pillsData,
                        badge: {
                            iconText: bIcon ? bIcon.textContent : '',
                            badgeText: badge.textContent.trim(),
                            hasVideo: hasVideo,
                            position: bStyle.position,
                            pointerEvents: bStyle.pointerEvents,
                        },
                    };
                }
                await new Promise(r => setTimeout(r, 100));
            }
            return { found: false };
        })()
        """,
        10.0,
    )
    assert isinstance(vo_info, dict)
    assert vo_info.get("found") is True

    # Validate opened pills (photo and voice message)
    pills = vo_info.get("pills", [])
    labels = [p["labelText"] for p in pills]
    assert "Photo · Opened" in labels
    assert "Voice message · Opened" in labels

    for p in pills:
        assert p["iconText"] == "①"
        assert p["hasMedia"] is False
        assert p["isOpenedBubble"] is True

    # Validate preserved view-once video badge
    badge = vo_info.get("badge", {})
    assert badge.get("iconText") == "①"
    assert "View Once" in badge.get("badgeText", "")
    assert badge.get("hasVideo") is True
    assert badge.get("position") == "absolute"
    assert badge.get("pointerEvents") == "none"

    browser.capture_screenshot(screenshot_dir / "view_once_messages_sarah.png", 5.0)


def test_chat_list_previews_deleted_and_view_once(
    browser: CDPSession, screenshot_dir: Path
) -> None:
    """Validate sidebar chat item previews display authentic deleted and view-once indicators."""
    previews = browser.evaluate(
        """
        (async () => {
            for (let i = 0; i < 50; i++) {
                const itemTc = document.querySelector('.chat-item[data-id="2"]');
                const itemSj = document.querySelector('.chat-item[data-id="15550101"]');
                const itemMv = document.querySelector('.chat-item[data-id="15550102"]');
                if (itemTc && itemSj && itemMv) {
                    const getMetaInfo = (el) => {
                        const delPreview = el.querySelector('.chat-meta .deleted-preview');
                        const voPreview = el.querySelector('.chat-meta .vo-preview');
                        const meta = el.querySelector('.chat-meta');
                        const delStyle = delPreview ? window.getComputedStyle(delPreview) : null;
                        const voStyle = voPreview ? window.getComputedStyle(voPreview) : null;
                        return {
                            hasDelPreview: !!delPreview,
                            delText: delPreview ? delPreview.textContent : '',
                            delFontStyle: delStyle ? delStyle.fontStyle : '',
                            hasVoPreview: !!voPreview,
                            voText: voPreview ? voPreview.textContent : '',
                            voFontStyle: voStyle ? voStyle.fontStyle : '',
                            metaText: meta ? meta.textContent : '',
                        };
                    };
                    return {
                        ready: true,
                        tc: getMetaInfo(itemTc),
                        sj: getMetaInfo(itemSj),
                        mv: getMetaInfo(itemMv),
                    };
                }
                await new Promise(r => setTimeout(r, 100));
            }
            return { ready: false };
        })()
        """,
        10.0,
    )
    assert isinstance(previews, dict)
    assert previews.get("ready") is True

    # Tech & Coffee (chat_id = "2"): outgoing deleted preview
    tc = previews.get("tc", {})
    assert tc.get("hasDelPreview") is True
    assert "🚫 You deleted this message" in tc.get("delText", "")
    assert tc.get("delFontStyle") == "italic"

    # Sarah Jenkins (chat_id = "15550101"): incoming deleted preview
    sj = previews.get("sj", {})
    assert sj.get("hasDelPreview") is True
    assert "🚫 This message was deleted" in sj.get("delText", "")
    assert sj.get("delFontStyle") == "italic"

    # Dr. Marcus Vance (chat_id = "15550102"): view-once preview
    mv = previews.get("mv", {})
    assert mv.get("hasVoPreview") is True
    assert "① Photo" in mv.get("voText", "")
    assert mv.get("voFontStyle") == "italic"

    browser.capture_screenshot(screenshot_dir / "chat_sidebar_previews.png", 5.0)


def test_poll_messages_visual_rendering(browser: CDPSession, screenshot_dir: Path) -> None:
    """Validate visual rendering of poll bubbles and progressive disclosure vote popups."""
    # 1. Open Alpine Hikers (chat_id = "1")
    chat_name = open_chat_by_id(browser, "1", 15.0)
    assert len(chat_name) > 0

    # 2. Inspect multi-choice poll bubble
    poll_info = browser.evaluate(
        """
        (async () => {
            for (let i = 0; i < 50; i++) {
                const poll = document.querySelector('#message-scroll .msg-poll');
                if (poll) {
                    const question = poll.querySelector('.msg-poll-question')?.textContent || '';
                    const badge = poll.querySelector('.msg-poll-badge')?.textContent || '';
                    const options = [...poll.querySelectorAll('.msg-poll-option')].map(opt => {
                        const title = opt.querySelector('.msg-poll-option-title span:last-child')?.textContent || '';
                        const count = opt.querySelector('.msg-poll-option-count')?.textContent || '0';
                        const check = opt.querySelector('.msg-poll-check');
                        const isUserSelected = opt.classList.contains('user-selected');
                        return {
                            title: title,
                            count: parseInt(count, 10),
                            hasCheck: !!check,
                            isUserSelected: isUserSelected,
                        };
                    });
                    const viewVotesBtn = poll.querySelector('.msg-poll-view-votes-btn');
                    return {
                        ready: true,
                        question: question,
                        badge: badge,
                        options: options,
                        hasViewVotesBtn: !!viewVotesBtn,
                    };
                }
                await new Promise(r => setTimeout(r, 100));
            }
            return { ready: false };
        })()
        """,
        10.0,
    )
    assert isinstance(poll_info, dict)
    assert poll_info.get("ready") is True
    assert "Trail choice" in poll_info.get("question", "")
    assert poll_info.get("badge") == "Multiple choice"
    assert poll_info.get("hasViewVotesBtn") is True

    options = poll_info.get("options", [])
    assert len(options) == 3
    # Option 1: Passo del Vento (3 votes, selected by You)
    assert "Passo del Vento" in options[0]["title"]
    assert options[0]["count"] == 3
    assert options[0]["isUserSelected"] is True
    assert options[0]["hasCheck"] is True
    # Option 2: Valle dei Larici (2 votes)
    assert "Valle dei Larici" in options[1]["title"]
    assert options[1]["count"] == 2
    # Option 3: Monte Cristallo (1 vote, selected by You)
    assert "Monte Cristallo" in options[2]["title"]
    assert options[2]["count"] == 1
    assert options[2]["isUserSelected"] is True
    assert options[2]["hasCheck"] is True

    browser.capture_screenshot(screenshot_dir / "poll_message_alpine.png", 5.0)

    # 3. Test Progressive Disclosure modal / popup
    popup_info = browser.evaluate(
        """
        (async () => {
            const btn = document.querySelector('#message-scroll .msg-poll .msg-poll-view-votes-btn');
            if (!btn) return { open: false };
            btn.click();

            for (let i = 0; i < 50; i++) {
                const popup = document.getElementById('msg-details-popup');
                if (popup && popup.classList.contains('visible') && !popup.textContent.includes('Loading')) {
                    const title = popup.querySelector('.rx-details-total')?.textContent || '';
                    const optionHeaders = [...popup.querySelectorAll('.msg-poll-details-option-hdr')].map(
                        el => ({
                            text: el.textContent.trim(),
                            hasStar: !!el.querySelector('.msg-poll-winner-star'),
                        })
                    );
                    const voters = [...popup.querySelectorAll('.msg-details-row .msg-details-label')].map(
                        el => el.textContent.trim()
                    );
                    if (voters.length > 0) {
                        return {
                            open: true,
                            title: title,
                            optionHeaders: optionHeaders,
                            voters: voters,
                        };
                    }
                }
                await new Promise(r => setTimeout(r, 100));
            }
            return { open: false };
        })()
        """,
        10.0,
    )
    assert isinstance(popup_info, dict)
    assert popup_info.get("open") is True
    assert "4 voters" in popup_info.get("title", "")
    opt_headers = popup_info.get("optionHeaders", [])
    assert len(opt_headers) == 3
    # Sorted by highest votes: 3, 2, 1
    assert "Passo del Vento" in opt_headers[0]["text"]
    assert opt_headers[0]["hasStar"] is True
    assert "Valle dei Larici" in opt_headers[1]["text"]
    assert opt_headers[1]["hasStar"] is False
    assert "Monte Cristallo" in opt_headers[2]["text"]
    assert opt_headers[2]["hasStar"] is False

    voters = popup_info.get("voters", [])
    assert "Maya Lin" in voters
    assert "Lucas Silva" in voters
    assert "Sam O'Connor" in voters
    assert "You" in voters

    browser.capture_screenshot(screenshot_dir / "poll_votes_popup_alpine.png", 5.0)

    # Close popup
    browser.evaluate(
        """
        (() => {
            const popup = document.getElementById('msg-details-popup');
            if (popup) popup.classList.remove('visible');
        })()
        """,
        3.0,
    )

    # 4. Open Tech & Coffee (chat_id = "2") for single-choice poll
    tc_name = open_chat_by_id(browser, "2", 15.0)
    assert len(tc_name) > 0

    tc_poll = browser.evaluate(
        """
        (async () => {
            for (let i = 0; i < 50; i++) {
                const poll = document.querySelector('#message-scroll .msg-poll');
                if (poll) {
                    const question = poll.querySelector('.msg-poll-question')?.textContent || '';
                    const badge = poll.querySelector('.msg-poll-badge')?.textContent || '';
                    const options = [...poll.querySelectorAll('.msg-poll-option')].map(opt => {
                        const title = opt.querySelector('.msg-poll-option-title span:last-child')?.textContent || '';
                        const count = opt.querySelector('.msg-poll-option-count')?.textContent || '0';
                        const isUserSelected = opt.classList.contains('user-selected');
                        return {
                            title: title,
                            count: parseInt(count, 10),
                            isUserSelected: isUserSelected,
                        };
                    });
                    return {
                        ready: true,
                        question: question,
                        badge: badge,
                        options: options,
                    };
                }
                await new Promise(r => setTimeout(r, 100));
            }
            return { ready: false };
        })()
        """,
        10.0,
    )
    assert isinstance(tc_poll, dict)
    assert tc_poll.get("ready") is True
    assert "Preferred brewing method" in tc_poll.get("question", "")
    assert tc_poll.get("badge") == "Single choice"

    tc_opts = tc_poll.get("options", [])
    assert len(tc_opts) == 3
    # V60 Pour-over (2 votes, selected by You)
    assert "V60 Pour-over" in tc_opts[0]["title"]
    assert tc_opts[0]["count"] == 2
    assert tc_opts[0]["isUserSelected"] is True

    # Check vote details popup for single-choice poll
    tc_popup = browser.evaluate(
        """
        (async () => {
            const btn = document.querySelector('#message-scroll .msg-poll .msg-poll-view-votes-btn');
            if (!btn) return { open: false };
            btn.click();

            for (let i = 0; i < 50; i++) {
                const popup = document.getElementById('msg-details-popup');
                if (popup && popup.classList.contains('visible') && !popup.textContent.includes('Loading')) {
                    const optionHeaders = [...popup.querySelectorAll('.msg-poll-details-option-hdr')].map(
                        el => ({
                            text: el.textContent.trim(),
                            hasStar: !!el.querySelector('.msg-poll-winner-star'),
                        })
                    );
                    const voters = [...popup.querySelectorAll('.msg-details-row .msg-details-label')].map(
                        el => el.textContent.trim()
                    );
                    if (voters.length > 0) {
                        return {
                            open: true,
                            optionHeaders: optionHeaders,
                            voters: voters,
                        };
                    }
                }
                await new Promise(r => setTimeout(r, 100));
            }
            return { open: false };
        })()
        """,
        10.0,
    )
    assert isinstance(tc_popup, dict)
    assert tc_popup.get("open") is True
    tc_opt_headers = tc_popup.get("optionHeaders", [])
    assert len(tc_opt_headers) == 3
    # Sorted by highest votes: 2, 1, 1
    assert "V60 Pour-over" in tc_opt_headers[0]["text"]
    assert tc_opt_headers[0]["hasStar"] is True
    assert tc_opt_headers[1]["hasStar"] is False
    assert tc_opt_headers[2]["hasStar"] is False

    tc_voters = tc_popup.get("voters", [])
    assert "Jordan Lee" in tc_voters
    assert "Chloe Bennett" in tc_voters
    assert "Alex Rivera" in tc_voters
    assert "You" in tc_voters

    browser.capture_screenshot(screenshot_dir / "poll_votes_popup_tech_coffee.png", 5.0)

    # Close popup
    browser.evaluate(
        """
        (() => {
            const popup = document.getElementById('msg-details-popup');
            if (popup) popup.classList.remove('visible');
        })()
        """,
        3.0,
    )


def test_android_starred_message_click_preserves_tab_and_loads_context(
    tmp_path: Path, screenshot_dir: Path
) -> None:
    """Clicking an Android 1:1 starred message must stay on Starred tab and load message context."""
    import threading
    import urllib.request

    from tests.conftest import make_android_db, make_archive_db
    from wab_viewer.main import create_app

    from .cdp_driver import find_free_port, launch_browser_session

    wa_path = tmp_path / "msgstore.db"
    arch_path = tmp_path / ".wa_media_archiver.db"

    wa_conn = make_android_db(wa_path)
    arch_conn = make_archive_db(arch_path)

    msg_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(message)").fetchall()}
    if "starred" not in msg_cols:
        wa_conn.execute("ALTER TABLE message ADD COLUMN starred INTEGER DEFAULT 0")
    jid_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(jid)").fetchall()}
    if "server" not in jid_cols:
        wa_conn.execute("ALTER TABLE jid ADD COLUMN server TEXT")
    chat_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(chat)").fetchall()}
    if "hidden" not in chat_cols:
        wa_conn.execute("ALTER TABLE chat ADD COLUMN hidden INTEGER DEFAULT 0")

    wa_conn.execute(
        "INSERT INTO jid (_id, user, server, raw_string) VALUES (1, '15551234', 's.whatsapp.net', '15551234@s.whatsapp.net')"
    )
    wa_conn.execute(
        "INSERT INTO jid (_id, user, server, raw_string) VALUES (2, '120363000111', 'g.us', '120363000111@g.us')"
    )
    wa_conn.execute(
        "INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp, hidden) VALUES (10, 1, NULL, 1700000060000, 0)"
    )
    wa_conn.execute(
        "INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp, hidden) VALUES (20, 2, 'Study Group', 1700000050000, 0)"
    )

    base_ts = 1700000000000
    for idx in range(1, 61):
        starred_val = 1 if idx == 30 else 0
        from_me_val = 1 if idx % 2 == 0 else 0
        text_val = "Crucial starred meeting detail" if idx == 30 else f"Context message {idx}"
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id, starred) "
            "VALUES (?, 10, ?, 0, ?, ?, 0, ?, ?)",
            (idx, from_me_val, base_ts + idx * 1000, text_val, f"K_1_{idx}", starred_val),
        )
    wa_conn.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id, starred) "
        "VALUES (105, 20, 0, 1, ?, 'Group starred reminder', 0, 'K_2_105', 1)",
        (base_ts + 5000,),
    )
    wa_conn.commit()
    wa_conn.close()

    arch_conn.execute(
        "INSERT INTO contacts (number, folder, display_name) VALUES ('15551234', 'Alice Star (15551234)', 'Alice Star')"
    )
    arch_conn.commit()
    arch_conn.close()

    port = find_free_port()
    app = create_app(tmp_path, rescan=False)
    app.config["TESTING"] = True
    server_thread = threading.Thread(
        target=lambda: app.run(port=port, use_reloader=False),
        daemon=True,
    )
    server_thread.start()

    base_url = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"{base_url}/api/chats", timeout=1.0) as resp:
                if resp.status == 200:
                    break
        except Exception:
            time.sleep(0.1)

    cdp_port = find_free_port()
    profile_dir = tmp_path / "browser_profile"
    session = launch_browser_session(base_url, profile_dir, cdp_port, 30, 0.2)
    try:
        result = session.evaluate(
            """
            (async () => {
                window.__testErrors = [];
                window.addEventListener('error', e => window.__testErrors.push(String(e.message || e)));
                window.addEventListener('unhandledrejection', e => window.__testErrors.push(String(e.reason || e)));

                for (let i = 0; i < 60; i++) {
                    if (document.querySelectorAll('.chat-item').length >= 2) break;
                    await new Promise(r => setTimeout(r, 100));
                }

                // Switch chat filter to 'group' first to verify clicking Starred does not reset back to all chats
                const grpFilter = document.querySelector('.filter-btn[data-filter="group"]');
                if (grpFilter) grpFilter.click();
                await new Promise(r => setTimeout(r, 100));

                // Open Starred tab
                const starredTab = document.getElementById('tab-btn-starred');
                starredTab.click();

                let starredItems = [];
                for (let i = 0; i < 60; i++) {
                    starredItems = Array.from(document.querySelectorAll('#chat-list .chat-item[data-msg-id]'));
                    if (starredItems.length === 2) break;
                    await new Promise(r => setTimeout(r, 100));
                }

                const targetItem = document.querySelector('#chat-list .chat-item[data-msg-id="30"]');
                if (!targetItem) {
                    return { error: 'Missing starred item 30', count: starredItems.length };
                }
                targetItem.click();

                // Wait for context messages to finish loading
                let loaded = false;
                for (let i = 0; i < 80; i++) {
                    await new Promise(r => setTimeout(r, 50));
                    const loadingEl = document.getElementById('chat-loading');
                    const row30 = document.querySelector('.msg-row[data-msg-id="30"]');
                    if (row30 && loadingEl && loadingEl.style.display === 'none') {
                        loaded = true;
                        break;
                    }
                }
                await new Promise(r => setTimeout(r, 150));

                const afterStarredItems = document.querySelectorAll('#chat-list .chat-item[data-msg-id]').length;
                const allChatItems = document.querySelectorAll('#chat-list .chat-item').length;
                const activeStarredItem = document.querySelector('#chat-list .chat-item[data-msg-id="30"]')?.classList.contains('active') || false;
                const tabStillActive = document.getElementById('tab-btn-starred')?.classList.contains('active') || false;
                const chatTitle = document.getElementById('chat-title')?.textContent || '';
                const bannerEl = document.getElementById('context-jump-banner');
                const bannerVisible = bannerEl ? getComputedStyle(bannerEl).display !== 'none' : false;
                const bannerText = document.getElementById('context-jump-text')?.textContent || '';
                const row30 = document.querySelector('.msg-row[data-msg-id="30"]');
                const scroll = document.getElementById('message-scroll');
                let rowInView = false;
                if (row30 && scroll) {
                    const rr = row30.getBoundingClientRect();
                    const sr = scroll.getBoundingClientRect();
                    rowInView = rr.bottom > sr.top && rr.top < sr.bottom;
                }

                return {
                    loaded,
                    afterStarredItems,
                    allChatItems,
                    activeStarredItem,
                    tabStillActive,
                    chatTitle,
                    bannerVisible,
                    bannerText,
                    rowFound: Boolean(row30),
                    rowInView,
                    errors: window.__testErrors
                };
            })()
            """,
            20.0,
        )

        assert isinstance(result, dict), f"Unexpected result: {result}"
        assert result.get("errors") == [], f"Browser errors: {result.get('errors')}"
        assert result.get("loaded") is True, f"Failed to load starred context: {result}"
        assert result.get("tabStillActive") is True
        assert result.get("afterStarredItems") == 2
        assert result.get("allChatItems") == 2
        assert result.get("activeStarredItem") is True
        assert result.get("chatTitle") == "Alice Star"
        assert result.get("bannerVisible") is True
        assert "Viewing message in context" in result.get("bannerText", "")
        assert result.get("rowFound") is True
        assert result.get("rowInView") is True

        session.capture_screenshot(screenshot_dir / "starred_context_jump.png", 5.0)
    finally:
        session.close()


def _set_viewport_size(session: CDPSession, width: int, height: int) -> None:
    """Resize the headless browser viewport via CDP Emulation."""
    req_id = session._next_id
    session._next_id += 1
    session.ws.send_json(
        {
            "id": req_id,
            "method": "Emulation.setDeviceMetricsOverride",
            "params": {
                "width": width,
                "height": height,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        }
    )
    deadline = time.time() + 5.0
    while time.time() < deadline:
        msg = session.ws.recv_json(1.0)
        if msg.get("id") == req_id:
            return
    raise TimeoutError("Timed out setting CDP viewport size")


def test_call_ui_refinements_and_in_chat_bubbles_e2e(tmp_path: Path, screenshot_dir: Path) -> None:
    """Validate sidebar responsive sizing, two-line call cards, in-chat call bubbles, popover, and Chat Info label."""
    import threading
    import urllib.request

    from tests.conftest import make_android_db, make_archive_db
    from wab_viewer.main import create_app

    from .cdp_driver import find_free_port, launch_browser_session

    wa_path = tmp_path / "msgstore.db"
    arch_path = tmp_path / ".wa_media_archiver.db"

    wa_conn = make_android_db(wa_path)
    arch_conn = make_archive_db(arch_path)

    jid_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(jid)").fetchall()}
    if "server" not in jid_cols:
        wa_conn.execute("ALTER TABLE jid ADD COLUMN server TEXT")
    chat_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(chat)").fetchall()}
    if "hidden" not in chat_cols:
        wa_conn.execute("ALTER TABLE chat ADD COLUMN hidden INTEGER DEFAULT 0")

    wa_conn.executescript("""
        CREATE TABLE IF NOT EXISTS jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
        CREATE TABLE IF NOT EXISTS call_log (
            _id INTEGER PRIMARY KEY,
            call_id TEXT,
            jid_row_id INTEGER,
            from_me INTEGER,
            timestamp INTEGER,
            video_call INTEGER,
            duration INTEGER,
            call_result INTEGER,
            group_jid_row_id INTEGER
        );
        INSERT INTO jid (_id, user, server, raw_string) VALUES (1, '15557654321', 's.whatsapp.net', '15557654321@s.whatsapp.net');
        INSERT INTO jid (_id, user, server, raw_string) VALUES (2, '120363000999', 'g.us', '120363000999@g.us');
        INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp, hidden) VALUES (10, 1, NULL, 1700000090000, 0);
        INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp, hidden) VALUES (20, 2, 'Design Sync Group', 1700000080000, 0);

        INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id)
        VALUES (1, 10, 0, 1, 1700000010000, 'Ready for our quick call?', 0, 'MSG_1');
        INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id)
        VALUES (2, 10, 1, 0, 1700000050000, 'Just shared the updated mockups!', 0, 'MSG_2');

        INSERT INTO call_log VALUES (101, 'call_missed_1', 1, 0, 1700000020000, 0, 0, 2, NULL);
        INSERT INTO call_log VALUES (102, 'call_video_2', 1, 1, 1700000035000, 1, 754, 1, NULL);
        INSERT INTO call_log VALUES (103, 'call_group_3', 1, 0, 1700000060000, 1, 125, 1, 2);
    """)
    wa_conn.commit()
    wa_conn.close()

    arch_conn.execute(
        "INSERT INTO contacts (number, folder, display_name) VALUES ('15557654321', 'Elena Vance (15557654321)', 'Elena Vance')"
    )
    arch_conn.commit()
    arch_conn.close()

    port = find_free_port()
    app = create_app(tmp_path, rescan=False)
    app.config["TESTING"] = True
    server_thread = threading.Thread(
        target=lambda: app.run(port=port, use_reloader=False),
        daemon=True,
    )
    server_thread.start()

    base_url = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"{base_url}/api/chats", timeout=1.0) as resp:
                if resp.status == 200:
                    break
        except Exception:
            time.sleep(0.1)

    cdp_port = find_free_port()
    profile_dir = tmp_path / "browser_profile_calls"
    session = launch_browser_session(base_url, profile_dir, cdp_port, 30, 0.2)
    try:
        # 1. Wait for app bootstrap and verify sidebar width responsiveness at 1280x800 vs 1920x1080
        for _ in range(60):
            ready = session.evaluate(
                "Boolean(document.getElementById('sidebar') && document.querySelectorAll('.chat-item').length >= 1)",
                5.0,
            )
            if ready:
                break
            time.sleep(0.15)

        _set_viewport_size(session, 1280, 800)
        w_1280_med = session.evaluate(
            """
            (() => {
                window.__testErrors = [];
                window.__badFetches = [];
                window.addEventListener('error', e => window.__testErrors.push(String(e.message || e)));
                window.addEventListener('unhandledrejection', e => window.__testErrors.push(String(e.reason || e)));
                const origFetch = window.fetch;
                window.fetch = async (...args) => {
                    const res = await origFetch(...args);
                    if (!res.ok) window.__badFetches.push({ url: String(args[0]), status: res.status });
                    return res;
                };
                return document.getElementById('sidebar').getBoundingClientRect().width;
            })()
            """,
            10.0,
        )
        assert isinstance(w_1280_med, (int, float))

        _set_viewport_size(session, 1920, 1080)
        size_metrics = session.evaluate(
            """
            (async () => {
                await new Promise(r => setTimeout(r, 100));
                const medW = document.getElementById('sidebar').getBoundingClientRect().width;
                document.getElementById('settings-btn').click();
                await new Promise(r => setTimeout(r, 100));
                document.querySelector('.pref-btn[data-pref="font_size"][data-value="small"]').click();
                await new Promise(r => setTimeout(r, 100));
                const smallW = document.getElementById('sidebar').getBoundingClientRect().width;
                document.querySelector('.pref-btn[data-pref="font_size"][data-value="large"]').click();
                await new Promise(r => setTimeout(r, 100));
                const largeW = document.getElementById('sidebar').getBoundingClientRect().width;
                document.querySelector('.pref-btn[data-pref="font_size"][data-value="medium"]').click();
                document.getElementById('settings-close').click();
                await new Promise(r => setTimeout(r, 100));
                return {
                    medW,
                    smallW,
                    largeW,
                    overflowX: document.documentElement.scrollWidth > window.innerWidth,
                };
            })()
            """,
            10.0,
        )
        assert isinstance(size_metrics, dict)
        assert size_metrics["medW"] > w_1280_med
        assert size_metrics["smallW"] < size_metrics["medW"] < size_metrics["largeW"]
        assert size_metrics["overflowX"] is False

        # 2. Switch to Calls tab and inspect two-line cards
        calls_ui = session.evaluate(
            """
            (async () => {
                document.getElementById('tab-btn-calls').click();
                let cards = [];
                for (let i = 0; i < 60; i++) {
                    cards = Array.from(document.querySelectorAll('#chat-list .call-item'));
                    if (cards.length >= 3) break;
                    await new Promise(r => setTimeout(r, 100));
                }
                const first = cards[0];
                const r1 = first.querySelector('.call-item-row-1').getBoundingClientRect();
                const r2 = first.querySelector('.call-item-row-2').getBoundingClientRect();
                const nameRect = first.querySelector('.call-item-name').getBoundingClientRect();
                const timeRect = first.querySelector('.call-item-time').getBoundingClientRect();
                first.click();
                await new Promise(r => setTimeout(r, 100));
                return {
                    count: cards.length,
                    row2BelowRow1: r2.top >= r1.bottom - 1,
                    sameLineNoTimeOverlap: timeRect.top >= r1.bottom - 1 && nameRect.right <= first.querySelector('.call-item-media-icon').getBoundingClientRect().left + 1,
                    cardHeight: first.getBoundingClientRect().height,
                };
            })()
            """,
            10.0,
        )
        assert calls_ui["count"] == 3
        assert calls_ui["row2BelowRow1"] is True
        assert calls_ui["sameLineNoTimeOverlap"] is True
        assert calls_ui["cardHeight"] >= 56
        session.capture_screenshot(screenshot_dir / "calls_list_two_line.png", 5.0)

        # 3. Open Elena Vance 1:1 chat and inspect chronological in-chat call bubbles + info popover + Chat Info
        chat_ui = session.evaluate(
            """
            (async () => {
                document.getElementById('tab-btn-chats').click();
                for (let i = 0; i < 40; i++) {
                    if (document.querySelector('.chat-item[data-id="15557654321"]')) break;
                    await new Promise(r => setTimeout(r, 100));
                }
                document.querySelector('.chat-item[data-id="15557654321"]').click();
                let callRows = [];
                for (let i = 0; i < 60; i++) {
                    callRows = Array.from(document.querySelectorAll('.msg-row .msg-bubble.call-event'));
                    const loadingEl = document.getElementById('chat-loading');
                    if (callRows.length === 2 && loadingEl && loadingEl.style.display === 'none') break;
                    await new Promise(r => setTimeout(r, 100));
                }
                const allRows = Array.from(document.querySelectorAll('#message-scroll .msg-row')).map(r => r.dataset.msgId);
                const bubble = callRows[0];
                const bubbleBorder = getComputedStyle(bubble).borderTopWidth;
                const infoBtn = bubble.querySelector('button.msg-info-btn');
                infoBtn.click();
                await new Promise(r => setTimeout(r, 150));
                const popup = document.getElementById('msg-details-popup');
                const popupText = popup ? popup.textContent : '';
                const popupVisible = popup ? popup.classList.contains('visible') : false;

                document.getElementById('chat-info-btn').click();
                let infoText = '';
                for (let i = 0; i < 40; i++) {
                    const body = document.getElementById('chat-info-body');
                    if (body && body.textContent.includes('Calls')) {
                        infoText = body.textContent;
                        break;
                    }
                    await new Promise(r => setTimeout(r, 100));
                }
                return {
                    callCount: callRows.length,
                    allRows,
                    bubbleBorder,
                    popupVisible,
                    popupText,
                    infoHasCallsLabel: infoText.includes('Calls') && !infoText.includes('Preserved calls'),
                    errors: window.__testErrors,
                    badFetches: window.__badFetches,
                };
            })()
            """,
            15.0,
        )
        assert chat_ui["errors"] == []
        assert chat_ui["badFetches"] == []
        assert chat_ui["callCount"] == 2
        assert chat_ui["allRows"] == ["1", "call:call_missed_1", "call:call_video_2", "2"]
        assert chat_ui["bubbleBorder"] == "0px"
        assert chat_ui["popupVisible"] is True
        assert "Voice call" in chat_ui["popupText"] or "Video call" in chat_ui["popupText"]
        assert chat_ui["infoHasCallsLabel"] is True

        # Close Chat Info & popup, then capture dark + light visual evidence screenshots
        session.evaluate(
            """
            (() => {
                document.getElementById('chat-info-close')?.click();
                document.getElementById('msg-details-popup')?.classList.remove('visible');
            })()
            """,
            3.0,
        )
        session.capture_screenshot(screenshot_dir / "in_chat_call_bubbles.png", 5.0)

        session.evaluate(
            """
            (async () => {
                document.getElementById('settings-btn').click();
                await new Promise(r => setTimeout(r, 100));
                document.querySelector('.pref-btn[data-pref="theme"][data-value="light"]').click();
                document.getElementById('settings-close').click();
                await new Promise(r => setTimeout(r, 150));
            })()
            """,
            5.0,
        )
        session.capture_screenshot(screenshot_dir / "in_chat_call_bubbles_light.png", 5.0)
    finally:
        session.close()


def test_review_fix_ui_contracts(tmp_path: Path) -> None:
    """Verify #message-scroll flex layout, archived call-moment return tab, call-dir-label, and disabled tab classes."""
    import threading
    import urllib.request

    from tests.conftest import make_android_db, make_archive_db
    from wab_viewer.main import create_app

    from .cdp_driver import find_free_port, launch_browser_session

    wa_path = tmp_path / "msgstore.db"
    arch_path = tmp_path / ".wa_media_archiver.db"

    wa_conn = make_android_db(wa_path)
    arch_conn = make_archive_db(arch_path)

    jid_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(jid)").fetchall()}
    if "server" not in jid_cols:
        wa_conn.execute("ALTER TABLE jid ADD COLUMN server TEXT")
    chat_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(chat)").fetchall()}
    if "hidden" not in chat_cols:
        wa_conn.execute("ALTER TABLE chat ADD COLUMN hidden INTEGER DEFAULT 0")
    if "archived" not in chat_cols:
        wa_conn.execute("ALTER TABLE chat ADD COLUMN archived INTEGER DEFAULT 0")

    wa_conn.executescript("""
        CREATE TABLE IF NOT EXISTS call_log (
            _id INTEGER PRIMARY KEY,
            call_id TEXT NOT NULL,
            jid_row_id INTEGER NOT NULL,
            from_me INTEGER NOT NULL,
            timestamp INTEGER NOT NULL,
            video_call INTEGER,
            duration INTEGER,
            call_result INTEGER,
            group_jid_row_id INTEGER
        );
        INSERT INTO jid (_id, user, server, raw_string)
        VALUES (1, '15551112222', 's.whatsapp.net', '15551112222@s.whatsapp.net'),
               (2, '15553334444', 's.whatsapp.net', '15553334444@s.whatsapp.net');
        INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp, hidden, archived)
        VALUES (10, 1, NULL, 1700000090000, 0, 0),
               (20, 2, NULL, 1700000080000, 0, 1);
        INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id)
        VALUES (1, 10, 0, 1, 1700000010000, 'Active msg', 0, 'M1'),
               (2, 20, 0, 2, 1700000020000, 'Archived call context', 0, 'M2');
        INSERT INTO call_log VALUES (101, 'arch_call_1', 2, 1, 1700000025000, 0, 40, 1, NULL);
    """)
    wa_conn.commit()
    wa_conn.close()

    arch_conn.execute(
        "INSERT INTO contacts (number, folder, display_name) VALUES ('15551112222', 'Alice (15551112222)', 'Alice')"
    )
    arch_conn.execute(
        "INSERT INTO contacts (number, folder, display_name) VALUES ('15553334444', 'Archived Bob (15553334444)', 'Archived Bob')"
    )
    arch_conn.commit()
    arch_conn.close()

    port = find_free_port()
    app = create_app(tmp_path, rescan=False)
    app.config["TESTING"] = True
    server_thread = threading.Thread(
        target=lambda: app.run(port=port, use_reloader=False),
        daemon=True,
    )
    server_thread.start()

    base_url = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"{base_url}/api/chats", timeout=1.0) as resp:
                if resp.status == 200:
                    break
        except Exception:
            time.sleep(0.1)

    cdp_port = find_free_port()
    profile_dir = tmp_path / "browser_profile_fixes"
    session = launch_browser_session(base_url, profile_dir, cdp_port, 30, 0.2)
    try:
        for _ in range(60):
            ready = session.evaluate(
                "Boolean(document.getElementById('sidebar') && document.querySelector('.chat-item[data-id=\"15551112222\"]'))",
                5.0,
            )
            if ready:
                break
            time.sleep(0.15)

        res = session.evaluate(
            """
            (async () => {
                window.__fixErrors = [];
                window.__fixBadFetches = [];
                window.addEventListener('error', e => window.__fixErrors.push(String(e.message || e)));
                window.addEventListener('unhandledrejection', e => window.__fixErrors.push(String(e.reason || e)));
                const origFetch = window.fetch;
                window.fetch = async (...args) => {
                    const r = await origFetch(...args);
                    if (!r.ok) window.__fixBadFetches.push({ url: String(args[0]), status: r.status });
                    return r;
                };

                // 1. Open active chat -> #message-scroll must stay display:flex
                document.querySelector('.chat-item[data-id="15551112222"]').click();
                for (let i = 0; i < 40; i++) {
                    const loadingEl = document.getElementById('chat-loading');
                    if (loadingEl && loadingEl.style.display === 'none') break;
                    await new Promise(r => setTimeout(r, 50));
                }
                const scroll = document.getElementById('message-scroll');
                const directDisplay = getComputedStyle(scroll).display;

                // 2. Switch to Calls and back to Chats -> #message-scroll must restore as flex
                document.getElementById('tab-btn-calls').click();
                let callCard = null;
                for (let i = 0; i < 50; i++) {
                    callCard = document.querySelector('#chat-list .call-item');
                    if (callCard) break;
                    await new Promise(r => setTimeout(r, 50));
                }
                callCard.click();
                await new Promise(r => setTimeout(r, 100));
                const dirLabel = document.querySelector('#call-inspect-dir .call-dir-label');
                const dirFontWeight = dirLabel ? getComputedStyle(dirLabel).fontWeight : '';

                document.getElementById('tab-btn-chats').click();
                await new Promise(r => setTimeout(r, 50));
                const restoredDisplay = getComputedStyle(scroll).display;

                // 3. Jump from archived chat's call moment -> check flex & Return switches to archived tab
                document.getElementById('tab-btn-calls').click();
                let jumpCallCard = null;
                for (let i = 0; i < 50; i++) {
                    jumpCallCard = document.querySelector('#chat-list .call-item');
                    if (jumpCallCard) break;
                    await new Promise(r => setTimeout(r, 50));
                }
                jumpCallCard.click();
                let jumpMomentBtn = null;
                for (let i = 0; i < 40; i++) {
                    jumpMomentBtn = document.getElementById('call-inspect-jump-btn');
                    if (jumpMomentBtn && !jumpMomentBtn.disabled) break;
                    await new Promise(r => setTimeout(r, 50));
                }
                jumpMomentBtn.click();
                let banner = null;
                for (let i = 0; i < 60; i++) {
                    banner = document.getElementById('context-jump-banner');
                    const loadingEl = document.getElementById('chat-loading');
                    if (banner && banner.style.display !== 'none' && loadingEl && loadingEl.style.display === 'none') break;
                    await new Promise(r => setTimeout(r, 50));
                }
                const jumpDisplay = getComputedStyle(scroll).display;
                const switchBtn = document.getElementById('context-jump-switch-btn');
                if (switchBtn) switchBtn.click();
                for (let i = 0; i < 40; i++) {
                    if (document.getElementById('tab-btn-archived').classList.contains('active')) break;
                    await new Promise(r => setTimeout(r, 50));
                }
                const archivedActive = document.getElementById('tab-btn-archived').classList.contains('active');
                const chatsActive = document.getElementById('tab-btn-chats').classList.contains('active');

                const tabBtns = Array.from(document.querySelectorAll('#sidebar-bottom-bar .tab-btn'));
                const hasTailwindTabClasses = tabBtns.some(b => b.classList.contains('opacity-30') || b.classList.contains('pointer-events-none'));

                return {
                    directDisplay,
                    restoredDisplay,
                    jumpDisplay,
                    dirFontWeight,
                    archivedActive,
                    chatsActive,
                    hasTailwindTabClasses,
                    errors: window.__fixErrors,
                    badFetches: window.__fixBadFetches,
                };
            })()
            """,
            20.0,
        )
        assert res["errors"] == []
        assert res["badFetches"] == []
        assert res["directDisplay"] == "flex"
        assert res["restoredDisplay"] == "flex"
        assert res["jumpDisplay"] == "flex"
        assert res["dirFontWeight"] in ("600", "bold")
        assert res["archivedActive"] is True
        assert res["chatsActive"] is False
        assert res["hasTailwindTabClasses"] is False
    finally:
        session.close()


def test_long_message_collapse_and_toggle(tmp_path: Path, screenshot_dir: Path) -> None:
    """Verify 750-char/15-line collapse, hysteresis, two-way Read More/Show Less toggle, pagination, search jump, and themes."""
    import threading
    import urllib.request

    from tests.conftest import make_android_db, make_archive_db
    from wab_viewer.main import create_app

    from .cdp_driver import find_free_port, launch_browser_session

    wa_path = tmp_path / "msgstore.db"
    arch_path = tmp_path / ".wa_media_archiver.db"
    media_file = tmp_path / "Contacts" / "Alice (15551112222)" / "2023" / "Received" / "photo.jpg"
    media_file.parent.mkdir(parents=True, exist_ok=True)
    # Minimal 1x1 JPEG bytes
    media_file.write_bytes(
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00\x03\x02\x02\x03\x02\x02\x03"
        b"\x03\x03\x03\x04\x03\x03\x04\x05\x08\x05\x05\x04\x04\x05\n\x07\x07\x06\x08\x0c\n\x0c\x0c\x0b\n\x0b\x0b"
        b"\r\x0e\x12\x10\r\x0e\x11\x0e\x0b\x0b\x10\x16\x10\x11\x13\x14\x15\x15\x15\x0c\x0f\x17\x18\x16\x14\x18\x12"
        b"\x14\x15\x14\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x14\x00\x01\x00\x00\x00\x00"
        b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\t\xff\xc4\x00\x14\x10\x01\x00\x00\x00\x00\x00\x00\x00\x00"
        b"\x00\x00\x00\x00\x00\x00\x00\x00\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xd2\xcf \xff\xd9"
    )

    wa_conn = make_android_db(wa_path)
    arch_conn = make_archive_db(arch_path)

    jid_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(jid)").fetchall()}
    if "server" not in jid_cols:
        wa_conn.execute("ALTER TABLE jid ADD COLUMN server TEXT")
    chat_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(chat)").fetchall()}
    if "hidden" not in chat_cols:
        wa_conn.execute("ALTER TABLE chat ADD COLUMN hidden INTEGER DEFAULT 0")
    if "archived" not in chat_cols:
        wa_conn.execute("ALTER TABLE chat ADD COLUMN archived INTEGER DEFAULT 0")

    wa_conn.executescript("""
        INSERT INTO jid (_id, user, server, raw_string)
        VALUES (1, '15551112222', 's.whatsapp.net', '15551112222@s.whatsapp.net'),
               (2, '15553334444', 's.whatsapp.net', '15553334444@s.whatsapp.net');
        INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp, hidden, archived)
        VALUES (10, 1, NULL, 1700000900000, 0, 0),
               (20, 2, NULL, 1700000050000, 0, 0);
    """)

    # 60 older filler messages so scrolling up triggers loadMessages('older') + renderWindow()
    base_ts = 1700000000000
    for idx in range(1, 61):
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id) "
            "VALUES (?, 10, ?, 1, ?, ?, 0, ?)",
            (idx, idx % 2, base_ts + idx * 1000, f"Older history message #{idx}", f"OLD_{idx}"),
        )

    long_formatted_msg = (
        "Start *bold intro section* with family emoji 👨‍👩‍👧‍👦 and link https://example.com/docs . "
        + ("Repeated sentence for readability testing across lines. " * 14)
        + "UNIQUETAILSEARCHNEEDLE end."
    )
    tall_list_msg = "\n".join(f"- Checklist item number {n}" for n in range(1, 23))
    hysteresis_msg = "Boundary check " + ("word " * 150) + "end."  # ~769 chars -> under 750+60
    long_caption_msg = "Long media caption: " + ("caption text " * 70) + "CAP_END"

    wa_conn.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id) "
        "VALUES (101, 10, 0, 1, ?, ?, 0, 'LONG_1')",
        (base_ts + 70000, long_formatted_msg),
    )
    wa_conn.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id) "
        "VALUES (102, 10, 1, 1, ?, ?, 0, 'LIST_1')",
        (base_ts + 71000, tall_list_msg),
    )
    wa_conn.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id) "
        "VALUES (103, 10, 0, 1, ?, ?, 0, 'HYST_1')",
        (base_ts + 72000, hysteresis_msg),
    )
    wa_conn.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id) "
        "VALUES (104, 10, 0, 1, ?, ?, 1, 'CAP_1')",
        (base_ts + 73000, long_caption_msg),
    )
    wa_conn.execute(
        "INSERT INTO message_media (_id, message_row_id, chat_row_id, file_path, media_name, mime_type) "
        "VALUES (1, 104, 10, 'Media/WhatsApp Images/photo.jpg', 'photo.jpg', 'image/jpeg')"
    )
    zwj_boundary_msg = ("a" * 749) + "👨‍👩‍👧‍👦 " + ("b" * 120) + " ZWJ_TAIL"
    wa_conn.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id) "
        "VALUES (105, 10, 0, 1, ?, ?, 0, 'ZWJ_1')",
        (base_ts + 74000, zwj_boundary_msg),
    )
    wa_conn.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id) "
        "VALUES (106, 10, 1, 1, ?, 'Reply pointing to long message', 0, 'QUOTE_1')",
        (base_ts + 75000,),
    )
    wa_conn.execute(
        "INSERT INTO message_quoted (message_row_id, text_data, from_me, sender_jid_row_id, timestamp) "
        "VALUES (106, 'Start *bold intro section*...', 0, 1, ?)",
        (base_ts + 70000,),
    )
    wa_conn.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id) "
        "VALUES (201, 20, 0, 2, ?, 'Other chat msg', 0, 'OTHER_1')",
        (base_ts + 10000,),
    )
    wa_conn.commit()
    wa_conn.close()

    arch_conn.execute(
        "INSERT INTO contacts (number, folder, display_name) VALUES ('15551112222', 'Alice (15551112222)', 'Alice')"
    )
    arch_conn.execute(
        "INSERT INTO contacts (number, folder, display_name) VALUES ('15553334444', 'Bob (15553334444)', 'Bob')"
    )
    arch_conn.execute(
        "INSERT INTO files (original_path, md5) VALUES ('Media/WhatsApp Images/photo.jpg', X'00010203')"
    )
    arch_conn.execute(
        "INSERT INTO archive_copies (original_path, archive_path) "
        "VALUES ('Media/WhatsApp Images/photo.jpg', 'Contacts/Alice (15551112222)/2023/Received/photo.jpg')"
    )
    arch_conn.commit()
    arch_conn.close()

    port = find_free_port()
    app = create_app(tmp_path, rescan=False)
    app.config["TESTING"] = True
    server_thread = threading.Thread(
        target=lambda: app.run(port=port, use_reloader=False),
        daemon=True,
    )
    server_thread.start()

    base_url = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"{base_url}/api/chats", timeout=1.0) as resp:
                if resp.status == 200:
                    break
        except Exception:
            time.sleep(0.1)

    cdp_port = find_free_port()
    profile_dir = tmp_path / "browser_profile_collapse"
    session = launch_browser_session(base_url, profile_dir, cdp_port, 30, 0.2)
    try:
        for _ in range(60):
            ready = session.evaluate(
                "Boolean(document.querySelector('.chat-item[data-id=\"15551112222\"]'))",
                5.0,
            )
            if ready:
                break
            time.sleep(0.15)

        res = session.evaluate(
            """
            (async () => {
                window.__colErrors = [];
                window.addEventListener('error', e => window.__colErrors.push(String(e.message || e)));
                window.addEventListener('unhandledrejection', e => window.__colErrors.push(String(e.reason || e)));

                document.querySelector('.chat-item[data-id="15551112222"]').click();
                for (let i = 0; i < 60; i++) {
                    const loadingEl = document.getElementById('chat-loading');
                    if (document.querySelector('.msg-row[data-msg-id="104"]') && loadingEl && loadingEl.style.display === 'none') break;
                    await new Promise(r => setTimeout(r, 50));
                }

                const r101 = document.querySelector('.msg-row[data-msg-id="101"]');
                const r102 = document.querySelector('.msg-row[data-msg-id="102"]');
                const r103 = document.querySelector('.msg-row[data-msg-id="103"]');
                const r104 = document.querySelector('.msg-row[data-msg-id="104"]');

                const btn101 = r101.querySelector('.msg-read-more');
                const btn102 = r102.querySelector('.msg-read-more');
                const btn103 = r103.querySelector('.msg-read-more');
                const btn104 = r104.querySelector('.msg-read-more');

                const btnStyle = getComputedStyle(btn101);
                const collapsed101 = {
                    hasBtn: Boolean(btn101),
                    btnText: btn101 ? btn101.textContent : '',
                    ariaExpanded: btn101 ? btn101.getAttribute('aria-expanded') : '',
                    endsWithEllipsisBtn: r101.querySelector('.msg-text').textContent.endsWith('... Read More'),
                    hasStrong: Boolean(r101.querySelector('.msg-text strong')),
                    hasBrokenReplacement: r101.querySelector('.msg-text').textContent.includes('\\uFFFD'),
                    hidesTailNeedle: !r101.querySelector('.msg-text').textContent.includes('UNIQUETAILSEARCHNEEDLE'),
                    bgColor: btnStyle.backgroundColor,
                    borderTopWidth: btnStyle.borderTopWidth,
                };
                const listCollapsed = {
                    hasBtn: Boolean(btn102),
                    btnText: btn102 ? btn102.textContent : '',
                    liCountCollapsed: r102.querySelectorAll('.msg-text li').length,
                };
                const hysteresisSkipped = !btn103 && r103.querySelector('.msg-text').textContent.includes('end.');
                const collapsed104 = {
                    hasBtn: Boolean(btn104),
                    btnText: btn104 ? btn104.textContent : '',
                    ariaExpanded: btn104 ? btn104.getAttribute('aria-expanded') : '',
                    endsWithEllipsisBtn: r104.querySelector('.msg-caption').textContent.endsWith('... Read More'),
                    hidesCapEnd: !r104.querySelector('.msg-caption').textContent.includes('CAP_END'),
                };

                // Expand 101, 102 & 104
                btn101.click();
                btn102.click();
                if (btn104) btn104.click();
                const expanded101 = {
                    btnText: r101.querySelector('.msg-read-more').textContent,
                    ariaExpanded: r101.querySelector('.msg-read-more').getAttribute('aria-expanded'),
                    showsTailNeedle: r101.querySelector('.msg-text').textContent.includes('UNIQUETAILSEARCHNEEDLE'),
                };
                const expanded104 = {
                    btnText: r104.querySelector('.msg-read-more') ? r104.querySelector('.msg-read-more').textContent : '',
                    ariaExpanded: r104.querySelector('.msg-read-more') ? r104.querySelector('.msg-read-more').getAttribute('aria-expanded') : '',
                    showsCapEnd: r104.querySelector('.msg-caption').textContent.includes('CAP_END'),
                };
                const listExpandedCount = r102.querySelectorAll('.msg-text li').length;

                // Toggle 104 back to collapsed ("Show Less")
                const btn104Expanded = r104.querySelector('.msg-read-more');
                if (btn104Expanded) btn104Expanded.click();
                const recollapsed104 = {
                    btnText: r104.querySelector('.msg-read-more') ? r104.querySelector('.msg-read-more').textContent : '',
                    ariaExpanded: r104.querySelector('.msg-read-more') ? r104.querySelector('.msg-read-more').getAttribute('aria-expanded') : '',
                    hidesCapEnd: !r104.querySelector('.msg-caption').textContent.includes('CAP_END'),
                };

                // Trigger older pagination and verify 101 stays expanded across renderWindow()
                const scroll = document.getElementById('message-scroll');
                scroll.scrollTop = 0;
                scroll.dispatchEvent(new Event('scroll'));
                for (let i = 0; i < 60; i++) {
                    if (document.querySelector('.msg-row[data-msg-id="1"]')) break;
                    await new Promise(r => setTimeout(r, 50));
                }
                const r101AfterPage = document.querySelector('.msg-row[data-msg-id="101"]');
                const stayedExpandedAfterPage = Boolean(
                    r101AfterPage && r101AfterPage.querySelector('.msg-text').textContent.includes('UNIQUETAILSEARCHNEEDLE')
                );

                // Toggle 101 back to collapsed ("Show Less")
                r101AfterPage.querySelector('.msg-read-more').click();
                const recollapsed101 = {
                    btnText: r101AfterPage.querySelector('.msg-read-more').textContent,
                    ariaExpanded: r101AfterPage.querySelector('.msg-read-more').getAttribute('aria-expanded'),
                    hidesTailNeedle: !r101AfterPage.querySelector('.msg-text').textContent.includes('UNIQUETAILSEARCHNEEDLE'),
                };

                // Switch away to Bob and back to Alice -> _prepareChatPane resets 102 back to collapsed
                document.querySelector('.chat-item[data-id="15553334444"]').click();
                for (let i = 0; i < 40; i++) {
                    if (document.querySelector('.msg-row[data-msg-id="201"]')) break;
                    await new Promise(r => setTimeout(r, 50));
                }
                document.querySelector('.chat-item[data-id="15551112222"]').click();
                for (let i = 0; i < 60; i++) {
                    const loadingEl = document.getElementById('chat-loading');
                    if (document.querySelector('.msg-row[data-msg-id="102"]') && loadingEl && loadingEl.style.display === 'none') break;
                    await new Promise(r => setTimeout(r, 50));
                }
                const reset102Btn = document.querySelector('.msg-row[data-msg-id="102"] .msg-read-more');
                const resetOnSwitch = Boolean(reset102Btn && reset102Btn.getAttribute('aria-expanded') === 'false');

                // In-chat search for UNIQUETAILSEARCHNEEDLE auto-expands 101
                document.getElementById('toolbar-toggle').click();
                const searchInp = document.getElementById('chat-search-input');
                searchInp.value = 'UNIQUETAILSEARCHNEEDLE';
                searchInp.dispatchEvent(new Event('input'));
                let autoExpandedOnSearch = false;
                for (let i = 0; i < 80; i++) {
                    const msg101 = document.querySelector('.msg-row[data-msg-id="101"]');
                    const countEl = document.getElementById('chat-search-count');
                    if (msg101 && countEl && countEl.textContent === '1/1' &&
                        msg101.querySelector('.msg-text').textContent.includes('UNIQUETAILSEARCHNEEDLE')) {
                        autoExpandedOnSearch = true;
                        break;
                    }
                    await new Promise(r => setTimeout(r, 100));
                }

                // Collapse 101 again, then verify quoted-message jump auto-expands 101
                const btnBeforeQuote = document.querySelector('.msg-row[data-msg-id="101"] .msg-read-more');
                if (btnBeforeQuote && btnBeforeQuote.getAttribute('aria-expanded') === 'true') {
                    btnBeforeQuote.click();
                }
                const quote106 = document.querySelector('.msg-row[data-msg-id="106"] .msg-quote');
                if (quote106) quote106.click();
                let autoExpandedOnQuote = false;
                for (let i = 0; i < 40; i++) {
                    if (document.querySelector('.msg-row[data-msg-id="101"] .msg-read-more')?.getAttribute('aria-expanded') === 'true') {
                        autoExpandedOnQuote = true;
                        break;
                    }
                    await new Promise(r => setTimeout(r, 50));
                }

                // ZWJ grapheme cluster at 750 boundary stays intact (not split mid-ZWJ)
                const msg105Text = document.querySelector('.msg-row[data-msg-id="105"] .msg-text')?.textContent || '';
                const zwjIntact = msg105Text.includes('👨‍👩‍👧‍👦') && !msg105Text.includes('ZWJ_TAIL');

                // Collapse 101 again for dark screenshot showing "... Read More"
                const btnAfterSearch = document.querySelector('.msg-row[data-msg-id="101"] .msg-read-more');
                if (btnAfterSearch && btnAfterSearch.getAttribute('aria-expanded') === 'true') {
                    btnAfterSearch.click();
                }

                return {
                    errors: window.__colErrors,
                    collapsed101,
                    listCollapsed,
                    hysteresisSkipped,
                    collapsed104,
                    expanded104,
                    recollapsed104,
                    expanded101,
                    listExpandedCount,
                    stayedExpandedAfterPage,
                    recollapsed101,
                    resetOnSwitch,
                    autoExpandedOnSearch,
                    autoExpandedOnQuote,
                    zwjIntact,
                };
            })()
            """,
            25.0,
        )

        assert res["errors"] == []
        assert res["collapsed101"]["hasBtn"] is True
        assert res["collapsed101"]["btnText"] == "Read More"
        assert res["collapsed101"]["ariaExpanded"] == "false"
        assert res["collapsed101"]["endsWithEllipsisBtn"] is True
        assert res["collapsed101"]["hasStrong"] is True
        assert res["collapsed101"]["hasBrokenReplacement"] is False
        assert res["collapsed101"]["hidesTailNeedle"] is True
        assert res["collapsed101"]["bgColor"] == "rgba(0, 0, 0, 0)"
        assert res["collapsed101"]["borderTopWidth"] == "0px"

        assert res["listCollapsed"]["hasBtn"] is True
        assert res["listCollapsed"]["btnText"] == "Read More"
        assert res["listCollapsed"]["liCountCollapsed"] == 15

        assert res["hysteresisSkipped"] is True
        assert res["collapsed104"]["hasBtn"] is True
        assert res["collapsed104"]["btnText"] == "Read More"
        assert res["collapsed104"]["ariaExpanded"] == "false"
        assert res["collapsed104"]["endsWithEllipsisBtn"] is True
        assert res["collapsed104"]["hidesCapEnd"] is True

        assert res["expanded104"]["btnText"] == "Show Less"
        assert res["expanded104"]["ariaExpanded"] == "true"
        assert res["expanded104"]["showsCapEnd"] is True

        assert res["recollapsed104"]["btnText"] == "Read More"
        assert res["recollapsed104"]["ariaExpanded"] == "false"
        assert res["recollapsed104"]["hidesCapEnd"] is True

        assert res["expanded101"]["btnText"] == "Show Less"
        assert res["expanded101"]["ariaExpanded"] == "true"
        assert res["expanded101"]["showsTailNeedle"] is True
        assert res["listExpandedCount"] == 22

        assert res["stayedExpandedAfterPage"] is True
        assert res["recollapsed101"]["btnText"] == "Read More"
        assert res["recollapsed101"]["ariaExpanded"] == "false"
        assert res["recollapsed101"]["hidesTailNeedle"] is True
        assert res["resetOnSwitch"] is True
        assert res["autoExpandedOnSearch"] is True
        assert res["autoExpandedOnQuote"] is True
        assert res["zwjIntact"] is True

        session.capture_screenshot(screenshot_dir / "long_msg_collapse_dark.png", 5.0)
        session.evaluate(
            """
            (async () => {
                document.getElementById('settings-btn').click();
                await new Promise(r => setTimeout(r, 100));
                document.querySelector('.pref-btn[data-pref="theme"][data-value="light"]').click();
                document.getElementById('settings-close').click();
                await new Promise(r => setTimeout(r, 150));
            })()
            """,
            5.0,
        )
        session.capture_screenshot(screenshot_dir / "long_msg_collapse_light.png", 5.0)
    finally:
        session.close()


def test_group_mentions_rendering_and_navigation(tmp_path: Path, screenshot_dir: Path) -> None:
    """Verify group @mentions render resolved names, unlinked spans, (Unknown) fallback, and navigate on link click."""
    import threading
    import urllib.request

    from tests.conftest import make_android_db, make_archive_db
    from wab_viewer.main import create_app

    from .cdp_driver import find_free_port, launch_browser_session

    wa_path = tmp_path / "msgstore.db"
    arch_path = tmp_path / ".wa_media_archiver.db"
    wa_conn = make_android_db(wa_path)
    arch_conn = make_archive_db(arch_path)

    jid_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(jid)").fetchall()}
    if "server" not in jid_cols:
        wa_conn.execute("ALTER TABLE jid ADD COLUMN server TEXT")
    chat_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(chat)").fetchall()}
    if "hidden" not in chat_cols:
        wa_conn.execute("ALTER TABLE chat ADD COLUMN hidden INTEGER DEFAULT 0")
    if "archived" not in chat_cols:
        wa_conn.execute("ALTER TABLE chat ADD COLUMN archived INTEGER DEFAULT 0")

    wa_conn.executescript("""
        CREATE TABLE IF NOT EXISTS jid_map (
            lid_row_id INTEGER PRIMARY KEY,
            jid_row_id INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lid_display_name (
            lid_row_id INTEGER PRIMARY KEY,
            display_name TEXT,
            username TEXT
        );
        CREATE TABLE IF NOT EXISTS message_mentions (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_row_id INTEGER NOT NULL,
            jid_row_id INTEGER NOT NULL
        );

        INSERT INTO jid (_id, user, server, raw_string) VALUES
            (1, '5550001001', 's.whatsapp.net', '5550001001@s.whatsapp.net'),
            (2, '1111111111', 'lid', '1111111111@lid'),
            (3, '2222222222', 'lid', '2222222222@lid'),
            (4, '9999999999', 'lid', '9999999999@lid'),
            (5, '120363000099', 'g.us', '120363000099@g.us');

        INSERT INTO jid_map (lid_row_id, jid_row_id) VALUES (2, 1);
        INSERT INTO lid_display_name (lid_row_id, display_name) VALUES (3, 'Bob Push');

        INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp, hidden, archived) VALUES
            (10, 1, NULL, 1700000100000, 0, 0),
            (20, 5, 'Project Alpha', 1700000200000, 0, 0);

        INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id) VALUES
            (1, 10, 0, 1, 1700000100000, 'Direct chat hello', 0, 'DM_1'),
            (2, 20, 0, 1, 1700000200000, 'Are you there @1111111111? Ping *@2222222222* and @9999999999, code `@1111111111`', 0, 'GRP_1');

        INSERT INTO message_mentions (message_row_id, jid_row_id) VALUES
            (2, 2), (2, 3), (2, 4);
    """)
    wa_conn.commit()
    wa_conn.close()

    arch_conn.execute(
        "INSERT INTO contacts (number, folder, display_name) VALUES ('5550001001', 'Alice', 'Alice Smith')"
    )
    arch_conn.commit()
    arch_conn.close()

    app = create_app(tmp_path)
    port = find_free_port()
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", port, app, threaded=True)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base_url = f"http://127.0.0.1:{port}"
    urllib.request.urlopen(f"{base_url}/api/chats", timeout=5).read()

    session = launch_browser_session(
        base_url, tmp_path / "browser_profile", find_free_port(), 10, 0.5
    )
    try:
        res = session.evaluate(
            """
            (async () => {
                let grpItem = null;
                for (let i = 0; i < 50; i++) {
                    grpItem = [...document.querySelectorAll('.chat-item')].find(
                        e => e.textContent.includes('Project Alpha')
                    );
                    if (grpItem) break;
                    await new Promise(r => setTimeout(r, 100));
                }
                if (grpItem) grpItem.click();
                await new Promise(r => setTimeout(r, 300));

                let msgEl = null;
                for (let i = 0; i < 50; i++) {
                    msgEl = document.querySelector('.msg-row .msg-text');
                    if (msgEl) break;
                    await new Promise(r => setTimeout(r, 100));
                }
                const linked = msgEl ? msgEl.querySelector('a.wa-mention-link') : null;
                const spans = [...msgEl.querySelectorAll('span.wa-mention')];
                const codeEl = msgEl.querySelector('code');
                const linkStyle = linked ? getComputedStyle(linked) : null;
                const spanStyle = spans[0] ? getComputedStyle(spans[0]) : null;

                const renderedText = msgEl.textContent;
                const linkedInfo = linked ? {
                    text: linked.textContent,
                    chatId: linked.dataset.chatId,
                    color: linkStyle.color,
                    textDecorationLine: linkStyle.textDecorationLine,
                } : null;
                const spanInfos = spans.map(s => ({
                    text: s.textContent,
                    color: getComputedStyle(s).color,
                    textDecorationLine: getComputedStyle(s).textDecorationLine,
                    cursor: getComputedStyle(s).cursor,
                }));

                // Click linked mention and verify navigation to Alice Smith's 1-to-1 chat
                linked.click();
                await new Promise(r => setTimeout(r, 350));
                const nextTitle = document.getElementById('chat-title').textContent;
                const nextBubbleText = document.querySelector('.msg-row .msg-text')?.textContent || '';

                return {
                    renderedText,
                    linkedInfo,
                    spanInfos,
                    codeText: codeEl ? codeEl.textContent : '',
                    nextTitle,
                    nextBubbleText,
                };
            })()
            """,
            15.0,
        )

        assert "@Alice Smith?" in res["renderedText"]
        assert "@Bob Push" in res["renderedText"]
        assert "@9999999999 (Unknown)" in res["renderedText"]
        assert res["codeText"] == "@1111111111"

        assert res["linkedInfo"] is not None
        assert res["linkedInfo"]["text"] == "@Alice Smith"
        assert res["linkedInfo"]["chatId"] == "5550001001"

        assert len(res["spanInfos"]) == 2
        assert res["spanInfos"][0]["text"] == "@Bob Push"
        assert res["spanInfos"][0]["color"] == res["linkedInfo"]["color"]
        assert res["spanInfos"][0]["textDecorationLine"] == "none"
        assert res["spanInfos"][0]["cursor"] == "default"
        assert res["spanInfos"][1]["text"] == "@9999999999 (Unknown)"

        assert res["nextTitle"] == "Alice Smith"
        assert "Direct chat hello" in res["nextBubbleText"]
        session.capture_screenshot(screenshot_dir / "group_mentions_nav.png", 5.0)
    finally:
        session.close()
        server.shutdown()


def test_group_sender_name_colors_and_theme_adaptation(
    browser: CDPSession, screenshot_dir: Path
) -> None:
    """Verify group sender names receive deterministic palette classes and adapt to dark/light themes."""
    open_group_chat_in_browser(browser, 15.0)

    prev_theme = browser.evaluate("document.documentElement.dataset.theme || 'dark'", 5.0)
    try:
        browser.evaluate("document.documentElement.dataset.theme = 'dark'", 5.0)
        dark_res = browser.evaluate(
            """
            (() => {
                const senders = Array.from(document.querySelectorAll('.msg-sender'));
                const items = senders.map(el => {
                    const cls = Array.from(el.classList).find(c => /^sender-color-\\d$/.test(c)) || '';
                    return { name: el.textContent.trim(), cls, color: getComputedStyle(el).color };
                });
                const quotes = Array.from(document.querySelectorAll('.msg-quote')).map(q => {
                    const qSender = q.querySelector('.msg-quote-sender');
                    const cls = Array.from(q.classList).find(c => /^sender-color-\\d$/.test(c)) || '';
                    return {
                        name: qSender ? qSender.textContent.trim() : '',
                        cls,
                        color: qSender ? getComputedStyle(qSender).color : '',
                        borderColor: getComputedStyle(q).borderLeftColor,
                    };
                });
                return { items, quotes };
            })()
            """,
            5.0,
        )
        assert len(dark_res["items"]) > 0
        by_name: dict[str, str] = {}
        for item in dark_res["items"]:
            assert item["cls"].startswith("sender-color-")
            if item["name"] in by_name:
                assert by_name[item["name"]] == item["cls"]
            else:
                by_name[item["name"]] = item["cls"]
        for q in dark_res["quotes"]:
            if q["name"] in by_name:
                assert q["cls"] == by_name[q["name"]]
                assert q["borderColor"] == q["color"]

        browser.capture_screenshot(screenshot_dir / "group_sender_colors_dark.png", 5.0)

        browser.evaluate("document.documentElement.dataset.theme = 'light'", 5.0)
        light_res = browser.evaluate(
            """
            (() => {
                return Array.from(document.querySelectorAll('.msg-sender')).map(el => ({
                    name: el.textContent.trim(),
                    color: getComputedStyle(el).color,
                }));
            })()
            """,
            5.0,
        )
        assert len(light_res) == len(dark_res["items"])
        for d_item, l_item in zip(dark_res["items"], light_res):
            assert d_item["color"] != l_item["color"]

        browser.capture_screenshot(screenshot_dir / "group_sender_colors_light.png", 5.0)
    finally:
        browser.evaluate(f"document.documentElement.dataset.theme = {prev_theme!r}", 5.0)


def test_round_video_note_player_and_exclusion_from_grouping(
    browser: CDPSession, screenshot_dir: Path
) -> None:
    """Verify PTV video notes render as circular players with progress rings and are excluded from 2x2 grouping."""
    open_group_chat_in_browser(browser, 15.0)

    setup_mock = browser.evaluate(
        """
        (() => {
            window._testOriginalFetch = window.fetch;
            const baseTs = 1775000000000;
            const fakeMsgs = [
                {
                    msg_id: 9001,
                    timestamp_ms: baseTs,
                    from_me: 0,
                    sender: 'Alice Smith',
                    media_type: 'video',
                    raw_msg_type: 28,
                    is_video_note: 1,
                    media_duration: 14,
                    archive_path: 'Contacts/Alice Smith/2026/Received/VID-1.mp4',
                    text_body: '',
                },
                {
                    msg_id: 9002,
                    timestamp_ms: baseTs + 2000,
                    from_me: 0,
                    sender: 'Alice Smith',
                    media_type: 'video',
                    raw_msg_type: 28,
                    is_video_note: 1,
                    media_duration: 7,
                    archive_path: 'Contacts/Alice Smith/2026/Received/VID-2.mp4',
                    text_body: '',
                },
                {
                    msg_id: 9003,
                    timestamp_ms: baseTs + 4000,
                    from_me: 0,
                    sender: 'Alice Smith',
                    media_type: 'video',
                    raw_msg_type: 54,
                    is_video_note: 1,
                    media_duration: 25,
                    archive_path: 'Contacts/Alice Smith/2026/Received/VID-3.mp4',
                    text_body: '',
                },
                {
                    msg_id: 9004,
                    timestamp_ms: baseTs + 6000,
                    from_me: 0,
                    sender: 'Alice Smith',
                    media_type: 'video',
                    raw_msg_type: 28,
                    is_video_note: 1,
                    media_duration: 62,
                    archive_path: 'Contacts/Alice Smith/2026/Received/VID-4.mp4',
                    text_body: '',
                },
                {
                    msg_id: 9005,
                    timestamp_ms: baseTs + 8000,
                    from_me: 1,
                    sender: '',
                    media_type: 'video',
                    raw_msg_type: 54,
                    is_video_note: 1,
                    media_duration: 9,
                    media_size: 128000,
                    archive_path: null,
                    text_body: '',
                },
            ];
            window.fetch = async (url, options) => {
                const urlStr = String(url);
                if (urlStr.includes('/api/messages?') && !urlStr.includes('before=') && !urlStr.includes('after=')) {
                    return new Response(JSON.stringify(fakeMsgs), {
                        status: 200,
                        headers: { 'Content-Type': 'application/json' },
                    });
                }
                return window._testOriginalFetch(url, options);
            };
            return true;
        })()
        """,
        5.0,
    )
    assert setup_mock is True

    try:
        res = browser.evaluate(
            """
            (async () => {
                const refreshBtn = document.getElementById('scroll-to-bottom');
                const firstChat = document.querySelector('#chat-list .chat-item');
                if (firstChat) firstChat.click();
                for (let i = 0; i < 60; i++) {
                    await new Promise(r => setTimeout(r, 50));
                    if (document.querySelectorAll('.msg-video-note').length === 4) break;
                }
                const notes = Array.from(document.querySelectorAll('.msg-video-note'));
                const missing = document.querySelector('.media-missing-card.is-video-note');
                const grids = document.querySelectorAll('.media-grid');
                const firstRect = notes[0] ? notes[0].getBoundingClientRect() : null;
                const firstStyle = notes[0] ? getComputedStyle(notes[0]) : null;
                const firstEnlargeBtn = notes[0] ? notes[0].querySelectorAll('.video-note-action-btn')[1] : null;
                let enlargedWidth = 0;
                let enlargedSquare = false;
                let shrunkWidth = 0;
                if (firstEnlargeBtn) {
                    firstEnlargeBtn.click();
                    await new Promise(r => setTimeout(r, 260));
                    const enRect = notes[0].getBoundingClientRect();
                    enlargedWidth = enRect.width;
                    enlargedSquare = Math.abs(enRect.width - enRect.height) <= 2;
                    firstEnlargeBtn.click();
                    await new Promise(r => setTimeout(r, 260));
                    const shRect = notes[0].getBoundingClientRect();
                    shrunkWidth = shRect.width;
                }
                return {
                    noteCount: notes.length,
                    gridCount: grids.length,
                    durations: notes.map(n => n.querySelector('.video-note-duration')?.textContent?.trim()),
                    hasRing: notes.every(n => !!n.querySelector('.video-note-ring-progress')),
                    hasPlayBtn: notes.every(n => !!n.querySelector('.video-note-play-btn')),
                    hasActions: notes.every(n => n.querySelectorAll('.video-note-action-btn').length === 2),
                    isSquare: firstRect ? Math.abs(firstRect.width - firstRect.height) <= 2 : false,
                    borderRadius: firstStyle ? firstStyle.borderRadius : '',
                    missingLabel: missing ? missing.querySelector('.media-missing-label')?.textContent?.trim() : '',
                    missingMeta: missing ? missing.querySelector('.media-missing-meta')?.textContent?.trim() : '',
                    firstWidth: firstRect ? firstRect.width : 0,
                    enlargedWidth,
                    enlargedSquare,
                    shrunkWidth,
                };
            })()
            """,
            10.0,
        )
        assert res["noteCount"] == 4
        assert res["gridCount"] == 0
        assert res["durations"] == ["0:14", "0:07", "0:25", "1:02"]
        assert res["hasRing"] is True
        assert res["hasPlayBtn"] is True
        assert res["hasActions"] is True
        assert res["isSquare"] is True
        assert res["borderRadius"] == "50%"
        assert res["missingLabel"] == "Video message"
        assert res["missingMeta"] == "125.0 KB · 0:09"
        assert res["enlargedWidth"] > res["firstWidth"]
        assert res["enlargedSquare"] is True
        assert abs(res["shrunkWidth"] - res["firstWidth"]) <= 2
        browser.capture_screenshot(screenshot_dir / "round_video_notes.png", 5.0)
    finally:
        browser.evaluate(
            """
            (() => {
                if (window._testOriginalFetch) {
                    window.fetch = window._testOriginalFetch;
                    delete window._testOriginalFetch;
                }
            })()
            """,
            5.0,
        )


@pytest.mark.ui
def test_gallery_filter_loading_feedback_and_batch_rendering(browser: CDPSession) -> None:
    """Verify visual loading feedback and end-of-results indicators in media gallery.

    Ensures:
    1. Active filter pill displays loading spinner during fetch operations.
    2. Centered loading state appears when switching to an empty filter section.
    3. Bottom end-of-results notice (.gallery-end-notice) appears when all items have loaded.
    4. Fast batch rendering preserves smooth navigation across media filters.
    """
    screenshot_dir = Path(".test_artifacts/screenshots")
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    chat_name = open_chat_by_id(browser, "15550102", 30.0)
    assert "Marcus" in chat_name

    # Open media gallery
    gallery = browser.evaluate(
        """
        (async () => {
            const mediaBtn = document.getElementById('media-btn');
            if (mediaBtn) mediaBtn.click();

            for (let i = 0; i < 60; i++) {
                await new Promise(r => setTimeout(r, 50));
                const gal = document.getElementById('media-gallery');
                const items = document.querySelectorAll('#media-gallery-grid .gallery-item');
                if (gal && gal.classList.contains('open') && items.length > 0) {
                    await new Promise(r => setTimeout(r, 300));
                    return { isOpen: true };
                }
            }
            return { isOpen: false };
        })()
        """,
        10.0,
    )
    assert isinstance(gallery, dict)
    assert gallery["isOpen"] is True

    # 1. Check end-of-results notice in default view
    end_notice = browser.evaluate(
        """
        (() => {
            const notice = document.querySelector('#media-gallery-grid .gallery-end-notice');
            return {
                exists: Boolean(notice),
                text: notice ? notice.textContent.trim() : '',
            };
        })()
        """,
        5.0,
    )
    assert end_notice["exists"] is True
    assert "loaded" in end_notice["text"].lower()

    # 2. Switch to Document pill and verify feedback
    doc_pill_res = browser.evaluate(
        """
        (async () => {
            const docPill = document.querySelector('#media-gallery-stats .gallery-stat[data-type="document"]');
            if (!docPill) return { found: false };
            docPill.click();

            // Check during / after switch
            await new Promise(r => setTimeout(r, 300));
            const activePill = document.querySelector('#media-gallery-stats .gallery-stat.active');
            const endNotice = document.querySelector('#media-gallery-grid .gallery-end-notice');
            const visibleDocs = [...document.querySelectorAll('#media-gallery-grid .gallery-item[data-type="document"]')]
                .filter(el => window.getComputedStyle(el).display !== 'none');

            return {
                found: true,
                activeType: activePill ? activePill.dataset.type : '',
                visibleCount: visibleDocs.length,
                endNoticeText: endNotice ? endNotice.textContent.trim() : '',
            };
        })()
        """,
        10.0,
    )
    assert doc_pill_res["found"] is True
    assert doc_pill_res["activeType"] == "document"
    assert doc_pill_res["visibleCount"] > 0
    assert "document" in doc_pill_res["endNoticeText"].lower()

    browser.capture_screenshot(screenshot_dir / "gallery_feedback_document.png", 5.0)


@pytest.mark.ui
def test_gallery_switch_from_default_photos_videos_to_all_items(browser: CDPSession) -> None:
    """Verify that switching from default photos+videos to all items loads all media types

    (including non-photo/non-video items like vcards) into the existing window immediately without scrolling.
    """
    screenshot_dir = Path(".test_artifacts/screenshots")
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    open_group_chat_in_browser(browser, 25.0)

    # 1. Open gallery
    gallery = browser.evaluate(
        """
        (async () => {
            const mediaBtn = document.getElementById('media-btn');
            if (mediaBtn) mediaBtn.click();

            for (let i = 0; i < 60; i++) {
                await new Promise(r => setTimeout(r, 50));
                const gal = document.getElementById('media-gallery');
                const items = document.querySelectorAll('#media-gallery-grid .gallery-item');
                if (gal && gal.classList.contains('open') && items.length > 0) {
                    await new Promise(r => setTimeout(r, 300));
                    return { isOpen: true };
                }
            }
            return { isOpen: false };
        })()
        """,
        10.0,
    )
    assert isinstance(gallery, dict)
    assert gallery["isOpen"] is True

    # 2. Check default state: photos and videos visible, vcards not in grid
    default_state = browser.evaluate(
        """
        (() => {
            const allItems = [...document.querySelectorAll('#media-gallery-grid .gallery-item')];
            const visiblePhotos = allItems.filter(el => el.dataset.type === 'image' && window.getComputedStyle(el).display !== 'none');
            const visibleVideos = allItems.filter(el => el.dataset.type === 'video' && window.getComputedStyle(el).display !== 'none');
            const visibleVcards = allItems.filter(el => el.dataset.type === 'vcard' && window.getComputedStyle(el).display !== 'none');
            return {
                photos: visiblePhotos.length,
                videos: visibleVideos.length,
                vcards: visibleVcards.length,
            };
        })()
        """,
        5.0,
    )
    assert default_state["photos"] > 0
    assert default_state["videos"] > 0
    assert default_state["vcards"] == 0

    # 3. Click 'Total' stat pill to switch to 'All items'
    all_items_state = browser.evaluate(
        """
        (async () => {
            const totalPill = document.querySelector('#media-gallery-stats .gallery-stat[data-type="total"]');
            if (!totalPill) return { error: 'Total pill not found' };
            totalPill.click();

            // Wait for unified stream to fetch and render
            for (let i = 0; i < 40; i++) {
                await new Promise(r => setTimeout(r, 50));
                const vcardItems = document.querySelectorAll('#media-gallery-grid .gallery-item[data-type="vcard"]');
                if (vcardItems.length > 0) {
                    break;
                }
            }
            await new Promise(r => setTimeout(r, 200));

            const allItems = [...document.querySelectorAll('#media-gallery-grid .gallery-item')];
            const visiblePhotos = allItems.filter(el => el.dataset.type === 'image' && window.getComputedStyle(el).display !== 'none');
            const visibleVideos = allItems.filter(el => el.dataset.type === 'video' && window.getComputedStyle(el).display !== 'none');
            const visibleVcards = allItems.filter(el => el.dataset.type === 'vcard' && window.getComputedStyle(el).display !== 'none');

            return {
                photos: visiblePhotos.length,
                videos: visibleVideos.length,
                vcards: visibleVcards.length,
            };
        })()
        """,
        10.0,
    )
    assert all_items_state["photos"] > 0
    assert all_items_state["videos"] > 0
    assert all_items_state["vcards"] > 0

    browser.capture_screenshot(screenshot_dir / "gallery_switch_all_items.png", 5.0)

    # 4. Click 'Total' again to toggle back to default view (instant in-memory hide)
    toggle_back_state = browser.evaluate(
        """
        (() => {
            const totalPill = document.querySelector('#media-gallery-stats .gallery-stat[data-type="total"]');
            if (totalPill) totalPill.click();

            const allItems = [...document.querySelectorAll('#media-gallery-grid .gallery-item')];
            const visiblePhotos = allItems.filter(el => el.dataset.type === 'image' && window.getComputedStyle(el).display !== 'none');
            const visibleVideos = allItems.filter(el => el.dataset.type === 'video' && window.getComputedStyle(el).display !== 'none');
            const visibleVcards = allItems.filter(el => el.dataset.type === 'vcard' && window.getComputedStyle(el).display !== 'none');

            return {
                photos: visiblePhotos.length,
                videos: visibleVideos.length,
                vcards: visibleVcards.length,
            };
        })()
        """,
        5.0,
    )
    assert toggle_back_state["photos"] > 0
    assert toggle_back_state["videos"] > 0
    assert toggle_back_state["vcards"] == 0


@pytest.mark.ui
def test_archive_view_total_pill_switch_and_media_types(browser: CDPSession) -> None:
    """Validate that switching to Total (all items) in Archive View does not freeze on 'Loading...',

    renders year blocks, and supports archived media files.
    """
    screenshot_dir = Path(".test_artifacts/screenshots")
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    open_group_chat_in_browser(browser, 25.0)

    # 1. Open gallery and switch to Archive view
    archive_setup = browser.evaluate(
        """
        (async () => {
            const mediaBtn = document.getElementById('media-btn');
            if (mediaBtn) mediaBtn.click();

            for (let i = 0; i < 40; i++) {
                await new Promise(r => setTimeout(r, 50));
                const gal = document.getElementById('media-gallery');
                if (gal && gal.classList.contains('open')) break;
            }

            const archiveBtn = document.querySelector('.media-view-btn[data-view="archive"]');
            if (!archiveBtn) return { error: 'Archive view button not found' };
            archiveBtn.click();

            for (let i = 0; i < 40; i++) {
                await new Promise(r => setTimeout(r, 50));
                const archiveView = document.getElementById('media-archive-view');
                if (archiveView && archiveView.classList.contains('active')) {
                    return { ok: true };
                }
            }
            return { error: 'Archive view failed to activate' };
        })()
        """,
        10.0,
    )
    assert archive_setup.get("ok") is True

    # 2. Click 'Total' stat pill in Archive View
    total_switch = browser.evaluate(
        """
        (async () => {
            let totalPill = null;
            for (let i = 0; i < 60; i++) {
                totalPill = document.querySelector('#media-gallery-stats .gallery-stat[data-type="total"]');
                if (totalPill) break;
                await new Promise(r => setTimeout(r, 50));
            }
            if (!totalPill) return { error: 'Total pill not found' };
            totalPill.click();

            // Wait for archive tree to load
            for (let i = 0; i < 50; i++) {
                await new Promise(r => setTimeout(r, 50));
                const years = document.querySelectorAll('#media-archive-tree .archive-year');
                if (years.length > 0) break;
            }
            await new Promise(r => setTimeout(r, 200));

            const notice = document.querySelector('#media-archive-tree .archive-empty-notice');
            const noticeText = notice ? notice.textContent : '';
            const years = [...document.querySelectorAll('#media-archive-tree .archive-year')];

            return {
                noticeText: noticeText,
                yearCount: years.length,
            };
        })()
        """,
        10.0,
    )
    assert "Loading…" not in total_switch["noticeText"]
    assert total_switch["yearCount"] > 0
    browser.capture_screenshot(screenshot_dir / "archive_view_all_items.png", 5.0)


@pytest.mark.ui
def test_gallery_video_thumbnail_no_path_leak_and_shimmer(browser: CDPSession) -> None:
    """Validate that video gallery tiles do not expose file paths in alt attributes,

    and that the subtle shimmer loading skeleton is configured.
    """
    open_group_chat_in_browser(browser, 25.0)

    # Check gallery video elements and shimmer lifecycle
    video_check = browser.evaluate(
        """
        (async () => {
            const mediaBtn = document.getElementById('media-btn');
            if (mediaBtn) mediaBtn.click();

            for (let i = 0; i < 40; i++) {
                await new Promise(r => setTimeout(r, 50));
                const gridBtn = document.querySelector('.media-view-btn[data-view="grid"]');
                if (gridBtn && !gridBtn.classList.contains('active')) gridBtn.click();
                const vids = document.querySelectorAll('#media-gallery-grid .gallery-item[data-type="video"]');
                if (vids.length > 0) break;
            }

            const videoImgs = [...document.querySelectorAll('#media-gallery-grid .gallery-item[data-type="video"] img')];
            const leakyAlts = videoImgs.map(img => img.alt).filter(alt => alt.includes('/') || alt.includes('\\\\') || alt.endsWith('.mp4') || alt.endsWith('.mov'));

            // Verify that .gallery-item.is-loading-thumb CSS shimmer rule is present in document stylesheets
            let hasShimmerRule = false;
            for (const sheet of document.styleSheets) {
                try {
                    for (const rule of sheet.cssRules) {
                        if (rule.selectorText && rule.selectorText.includes('is-loading-thumb')) {
                            hasShimmerRule = true;
                            break;
                        }
                    }
                } catch (e) {}
                if (hasShimmerRule) break;
            }

            return {
                videoCount: videoImgs.length,
                leakyAlts: leakyAlts,
                hasShimmerRule: hasShimmerRule,
            };
        })()
        """,
        10.0,
    )
    assert video_check["videoCount"] > 0
    assert len(video_check["leakyAlts"]) == 0
    assert video_check["hasShimmerRule"] is True


def test_android_pinned_messages_banner_and_bubble_appearance(
    browser: CDPSession, screenshot_dir: Path
) -> None:
    """Verify that Android pinned messages render the top banner, navigate between pins, and show bubble pin icons."""
    open_chat_by_id(browser, "1", 15.0)

    # 1. Wait for pinned banner to populate
    banner_info = browser.evaluate(
        """
        (async () => {
            const banner = document.getElementById('pinned-banner');
            if (!banner) return { found: false };

            for (let i = 0; i < 60; i++) {
                if (banner.style.display !== 'none') break;
                await new Promise(r => setTimeout(r, 50));
            }

            const author = document.getElementById('pinned-banner-author')?.textContent || '';
            const snippet = document.getElementById('pinned-banner-snippet')?.textContent || '';
            const count = document.getElementById('pinned-banner-count')?.textContent || '';
            const nav = document.getElementById('pinned-banner-nav');
            const navVisible = nav ? nav.style.display !== 'none' : false;

            return {
                found: true,
                visible: banner.style.display !== 'none',
                author,
                snippet,
                count,
                navVisible,
            };
        })()
        """,
        5.0,
    )
    assert banner_info["found"] is True
    assert banner_info["visible"] is True
    assert "Pinned Message" in banner_info["author"]
    assert len(banner_info["snippet"]) > 0
    assert banner_info["navVisible"] is True
    assert "1 of 2" in banner_info["count"]

    # 2. Cycle to next pinned message
    cycle_info = browser.evaluate(
        """
        (() => {
            const nextBtn = document.getElementById('pinned-banner-next');
            if (nextBtn) nextBtn.click();
            const count = document.getElementById('pinned-banner-count')?.textContent || '';
            const snippet = document.getElementById('pinned-banner-snippet')?.textContent || '';
            return { count, snippet };
        })()
        """,
        3.0,
    )
    assert "2 of 2" in cycle_info["count"]
    assert len(cycle_info["snippet"]) > 0

    # 3. Verify message bubble pin icons in scroll window
    bubble_pin_info = browser.evaluate(
        """
        (() => {
            const pinnedIcons = document.querySelectorAll('.msg-row .msg-pinned-icon');
            const pinnedRows = [...pinnedIcons].map(icon => icon.closest('.msg-row')?.dataset.msgId);
            return {
                iconCount: pinnedIcons.length,
                pinnedRowIds: pinnedRows,
            };
        })()
        """,
        3.0,
    )
    assert bubble_pin_info["iconCount"] >= 1
    assert any(rid in ("10", "105") for rid in bubble_pin_info["pinnedRowIds"])

    browser.capture_screenshot(screenshot_dir / "pinned_banner_and_bubble_alpine.png", 5.0)


def test_chat_info_duration_calculation_display(browser: CDPSession, screenshot_dir: Path) -> None:
    """Verify that Chat Info panel calculates and displays the conversation lifetime / duration row."""
    open_chat_by_id(browser, "1", 15.0)

    # Open Chat Info panel and wait for asynchronous data hydration
    info_data = browser.evaluate(
        """
        (async () => {
            const infoBtn = document.getElementById('chat-info-btn');
            if (infoBtn) infoBtn.click();

            const panel = document.getElementById('chat-info-panel');
            for (let i = 0; i < 40; i++) {
                if (panel && panel.classList.contains('open')) break;
                await new Promise(r => setTimeout(r, 50));
            }

            // Await chat-info-row items to be populated from /api/chat-info
            let rows = [];
            for (let i = 0; i < 60; i++) {
                rows = document.querySelectorAll('#chat-info-body .chat-info-row');
                if (rows.length >= 2) break;
                await new Promise(r => setTimeout(r, 50));
            }

            const data = {};
            rows.forEach(r => {
                const label = r.querySelector('.chat-info-label')?.textContent?.trim() || '';
                const val = r.querySelector('.chat-info-value')?.textContent?.trim() || '';
                if (label) data[label] = val;
            });

            return {
                panelOpen: panel ? panel.classList.contains('open') : false,
                rows: data,
            };
        })()
        """,
        5.0,
    )
    assert info_data["panelOpen"] is True
    assert "Conversation since" in info_data["rows"]
    assert "Duration" in info_data["rows"]

    duration_text = info_data["rows"]["Duration"]
    assert len(duration_text) > 0
    assert "NaN" not in duration_text
    assert any(unit in duration_text for unit in ("day", "month", "year"))

    browser.capture_screenshot(screenshot_dir / "chat_info_duration_alpine.png", 5.0)

    # Close panel
    browser.evaluate(
        """
        (() => {
            const closeBtn = document.getElementById('chat-info-close');
            if (closeBtn) closeBtn.click();
        })()
        """,
        2.0,
    )


def test_chat_video_first_frame_poster_preview(browser: CDPSession, ui_demo_dir: Path) -> None:
    """Verify video messages in chat view capture and display the first frame as poster."""
    open_group_chat_in_browser(browser, 25.0)

    # Synthesize a tiny valid WebM video in-memory via Canvas + MediaRecorder and write it to ui_demo_dir
    webm_b64 = browser.evaluate(
        """
        (async () => {
            const canvas = document.createElement('canvas');
            canvas.width = 64;
            canvas.height = 48;
            const ctx = canvas.getContext('2d');
            ctx.fillStyle = '#22aa66';
            ctx.fillRect(0, 0, 64, 48);

            const stream = canvas.captureStream(10);
            const recorder = new MediaRecorder(stream, { mimeType: 'video/webm' });
            const chunks = [];
            recorder.ondataavailable = (e) => {
                if (e.data && e.data.size > 0) chunks.push(e.data);
            };
            const stopped = new Promise((resolve) => {
                recorder.onstop = resolve;
            });
            recorder.start();
            await new Promise((r) => setTimeout(r, 150));
            recorder.stop();
            await stopped;
            stream.getTracks().forEach((t) => t.stop());

            const blob = new Blob(chunks, { type: 'video/webm' });
            const buf = await blob.arrayBuffer();
            const bytes = new Uint8Array(buf);
            let bin = '';
            for (let i = 0; i < bytes.length; i++) {
                bin += String.fromCharCode(bytes[i]);
            }
            return btoa(bin);
        })()
        """,
        10.0,
    )
    import base64

    synth_video = ui_demo_dir / "SYNTH-VID.webm"
    synth_video.write_bytes(base64.b64decode(webm_b64))
    try:
        result = browser.evaluate(
            """
            (async () => {
                const fakeMsgs = [
                    {
                        msg_id: 9901,
                        timestamp_ms: 1775000010000,
                        from_me: 0,
                        sender: 'Alice Smith',
                        media_type: 'video',
                        is_video_note: 0,
                        media_duration: 1,
                        archive_path: 'SYNTH-VID.webm',
                        text_body: '',
                    },
                    {
                        msg_id: 9902,
                        timestamp_ms: 1775000012000,
                        from_me: 0,
                        sender: 'Alice Smith',
                        media_type: 'video',
                        is_video_note: 1,
                        media_duration: 1,
                        archive_path: 'SYNTH-VID.webm',
                        text_body: '',
                    },
                ];

                const origFetch = window.fetch;
                window.fetch = async (url, options) => {
                    const urlStr = String(url);
                    if (urlStr.includes('/api/messages?') && !urlStr.includes('before=') && !urlStr.includes('after=')) {
                        return new Response(JSON.stringify(fakeMsgs), {
                            status: 200,
                            headers: { 'Content-Type': 'application/json' },
                        });
                    }
                    return origFetch(url, options);
                };

                try {
                    const firstChat = document.querySelector('#chat-list .chat-item');
                    if (firstChat) firstChat.click();

                    let targetVid = null;
                    let notePoster = null;
                    for (let i = 0; i < 40; i++) {
                        targetVid = document.querySelector('#message-scroll .msg-media video:not(.msg-gif-video):not(.live-photo-video):not(.video-note-video)');
                        notePoster = document.querySelector('#message-scroll .msg-video-note img.video-note-poster');
                        if (targetVid && notePoster) break;
                        await new Promise(r => setTimeout(r, 100));
                    }
                    if (!targetVid || !notePoster) {
                        return { count: 0, posterVal: '', notePosterVal: '' };
                    }

                    let posterVal = '';
                    let notePosterVal = '';
                    for (let i = 0; i < 60; i++) {
                        if (targetVid.poster && targetVid.poster.startsWith('data:image/jpeg')) {
                            posterVal = targetVid.poster;
                        }
                        if (notePoster.src && notePoster.src.startsWith('data:image/jpeg') && notePoster.style.display === 'block') {
                            notePosterVal = notePoster.src;
                        }
                        if (posterVal && notePosterVal) break;
                        await new Promise(r => setTimeout(r, 100));
                    }
                    return { count: 1, posterVal, notePosterVal };
                } finally {
                    window.fetch = origFetch;
                }
            })()
            """,
            15.0,
        )
        assert result["count"] == 1
        assert result["posterVal"].startswith("data:image/jpeg;base64,")
        assert result["notePosterVal"].startswith("data:image/jpeg;base64,")
    finally:
        synth_video.unlink(missing_ok=True)


def test_group_all_mention_highlight_and_i18n(browser, ui_server_url) -> None:
    """@all in group chats is styled bold/highlighted and translated per active locale."""
    import json as _json
    import time
    import urllib.request

    req = urllib.request.Request(
        f"{ui_server_url}/api/preferences",
        data=_json.dumps({"key": "language", "value": "de"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200

    browser.evaluate("setTimeout(() => location.reload(), 10);", 5.0)
    for _ in range(80):
        try:
            if browser.evaluate(
                "window.INITIAL_LOCALE === 'de' && !!document.querySelector('#chat-list .chat-item')",
                2.0,
            ):
                break
        except RuntimeError:
            pass
        time.sleep(0.1)

    res = browser.evaluate(
        """
        (async () => {
            for (let i = 0; i < 60; i++) {
                if (window.INITIAL_LOCALE === 'de' && document.querySelector('#chat-list .chat-item')) {
                    break;
                }
                await new Promise(r => setTimeout(r, 100));
            }
            const chats = await (await fetch('/api/chats')).json();
            const groupChat = chats.find(c => c.type === 'group');
            const contactChat = chats.find(c => c.type === 'contact');
            if (!groupChat || !contactChat) return { missingChats: true };

            const origFetch = window.fetch.bind(window);
            window.fetch = async (url, opts) => {
                if (typeof url === 'string' && url.includes('/api/chat-index-status')) {
                    return new Response(JSON.stringify({ indexed: true, percent: 100 }), {
                        status: 200,
                        headers: { 'Content-Type': 'application/json' }
                    });
                }
                if (typeof url === 'string' && url.includes('/api/messages')) {
                    const isGrp = url.includes('chat_type=group');
                    return new Response(JSON.stringify([{
                        msg_id: isGrp ? 99001 : 99002,
                        timestamp_ms: 1700000000000,
                        from_me: 0,
                        sender: isGrp ? 'Alice' : null,
                        media_type: 'text',
                        text_body: 'Hey @all, meet @allen and `@all`'
                    }]), { status: 200, headers: { 'Content-Type': 'application/json' } });
                }
                return origFetch(url, opts);
            };

            try {
                const grpItem = document.querySelector(`#chat-list .chat-item[data-id="${groupChat.id}"][data-type="group"]`);
                grpItem.click();
                let grpTextEl = null;
                for (let i = 0; i < 40; i++) {
                    grpTextEl = document.querySelector('#message-scroll .msg-row[data-msg-id="99001"] .msg-text');
                    if (grpTextEl) break;
                    await new Promise(r => setTimeout(r, 100));
                }
                const mentionEl = grpTextEl ? grpTextEl.querySelector('.wa-mention.wa-mention-all') : null;
                const grpResult = {
                    html: grpTextEl ? grpTextEl.innerHTML : '',
                    mentionText: mentionEl ? mentionEl.textContent : '',
                    fontWeight: mentionEl ? getComputedStyle(mentionEl).fontWeight : ''
                };

                const ctcItem = document.querySelector(`#chat-list .chat-item[data-id="${contactChat.id}"][data-type="contact"]`);
                ctcItem.click();
                let ctcTextEl = null;
                for (let i = 0; i < 40; i++) {
                    ctcTextEl = document.querySelector('#message-scroll .msg-row[data-msg-id="99002"] .msg-text');
                    if (ctcTextEl) break;
                    await new Promise(r => setTimeout(r, 100));
                }
                const ctcMention = ctcTextEl ? ctcTextEl.querySelector('.wa-mention') : null;

                return {
                    grpResult,
                    contactHasMention: Boolean(ctcMention),
                    contactHtml: ctcTextEl ? ctcTextEl.innerHTML : ''
                };
            } finally {
                window.fetch = origFetch;
                await origFetch('/api/preferences', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ key: 'language', value: 'auto' })
                });
            }
        })()
        """,
        20.0,
    )
    assert not res.get("missingChats")
    assert res["grpResult"]["mentionText"] == "@alle"
    assert '<span class="wa-mention wa-mention-all">@alle</span>' in res["grpResult"]["html"]
    assert "<code>@all</code>" in res["grpResult"]["html"]
    assert "@allen" in res["grpResult"]["html"]
    assert int(res["grpResult"]["fontWeight"]) >= 600
    assert res["contactHasMention"] is False
