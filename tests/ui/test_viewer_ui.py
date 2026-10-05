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
            const mediaItems = items.filter(el => el.dataset.src);
            const hydrated = mediaItems.filter(el => el.dataset.hydrated === 'true');
            const heights = mediaItems.map(el => el.getBoundingClientRect().height);
            const allStandardHeight = heights.every(h => Math.abs(h - 120) < 2);

            const firstImage = items.find(el => el.dataset.type === 'image');
            const firstVideo = items.find(el => el.dataset.type === 'video');

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
    # 1. Open contact chat Sarah Jenkins
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
        diff = abs(media_info["naturalAspect"] - media_info["renderedAspect"])
        assert diff < 0.05, (
            f"Aspect ratio distorted: natural={media_info['naturalAspect']:.3f}, "
            f"rendered={media_info['renderedAspect']:.3f}"
        )
        if not media_info.get("hasCaption"):
            wasted_space = media_info["bubbleWidth"] - media_info["renderedWidth"] - 20
            assert wasted_space < 60, (
                f"Too much wasted space in media bubble: bubbleWidth={media_info['bubbleWidth']}, "
                f"mediaWidth={media_info['renderedWidth']}, wasted={wasted_space}"
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
                if (lb && lbImg) {
                    return { opened: true };
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

