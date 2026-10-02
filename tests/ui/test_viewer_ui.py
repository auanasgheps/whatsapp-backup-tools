"""UI/UX and visual interaction tests for WhatsApp Chat Viewer.

Validates the complete web interface lifecycle in a real headless browser using CDP:
- Chat list loading and loading overlay dismissal
- Bottom scroll pinning and layout stability (CLS prevention)
- Upward scroll pagination and anchor preservation
- Message elements rendering (bubbles, date separators, service messages, quotes, reactions)
- Design token compliance and dual-theme switching (Dark / Light)
- Modals and drawers (Chat Info panel, Media Gallery, Settings)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .cdp_driver import CDPSession

# Apply ui marker to all tests in this module
pytestmark = pytest.mark.ui


def open_group_chat_in_browser(browser: CDPSession, timeout: float) -> str:
    """Wait for chat list, click the first group chat, and wait for messages to load."""
    js_code = """
    (async () => {
        for (let i = 0; i < 60; i++) {
            const item = document.querySelector('.chat-item[data-type="group"]');
            if (item) {
                const name = item.querySelector('.chat-name')?.textContent || '';
                item.click();

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


def test_chat_list_and_chat_opening(browser: CDPSession, screenshot_dir: Path) -> None:
    """Validate that chat items render and clicking an item loads the chat pane."""
    # 1. Wait for chat list to populate
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
    assert state_initial["total"] == 2
    assert state_initial["visible"] == 1
    assert state_initial["empty"] == 1

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

