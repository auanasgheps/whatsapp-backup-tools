# HTML template for the WhatsApp chat viewer web UI.

from pathlib import Path

_PICO_CSS = (Path(__file__).parent / "pico.min.css").read_text(encoding="utf-8")
# Strip @charset — invalid inside @layer
_PICO_CSS = _PICO_CSS.replace('@charset "UTF-8";', '', 1)

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta id="meta-color-scheme" name="color-scheme" content="dark">
  <title>WA Chat Viewer</title>
  <style>""" + _PICO_CSS + r"""</style>
  <style>
    /* ---- theme tokens ---- */
    :root {
      --bg: #111b21;
      --surface: #1f2c33;
      --surface2: #2a3942;
      --bubble-in: #d9fdd3;
      --bubble-out: #dcf8c6;
      --bubble-in-text: #111b21;
      --bubble-out-text: #111b21;
      --text: #e9edef;
      --text-muted: #8696a0;
      --accent: #00a884;
      --border: #222d34;
      --sidebar-w: 300px;
      --font-size: 15px;
    }

    [data-theme="light"] {
      --bg: #f0f2f5;
      --surface: #ffffff;
      --surface2: #e9edef;
      --bubble-in: #ffffff;
      --bubble-out: #d9fdd3;
      --bubble-in-text: #111b21;
      --bubble-out-text: #111b21;
      --text: #111b21;
      --text-muted: #54656f;
      --accent: #00a884;
      --border: #d1d7db;
    }

    /* ---- Pico overrides: reset opinionated defaults that break our layout ---- */
    *, *::before, *::after { box-sizing: border-box; }
    html, body {
      height: 100%;
      background: var(--bg) !important;
      color: var(--text) !important;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      padding: 0 !important;
      margin: 0;
    }
    h1, h2, h3, h4, h5, h6, p { margin: 0; padding: 0; }
    button { font-family: inherit; }
    /* Pico adds padding to body > header/footer/main; we don't use those */

    /* ---- app shell ---- */
    #app { display: flex; flex-direction: column; height: 100vh; }

    #header {
      background: var(--surface);
      padding: 12px 16px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      gap: 12px;
      flex-shrink: 0;
    }
    #header h1 { font-size: 16px; font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    #header .subtitle { font-size: 11px; color: var(--text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

    #body { display: flex; flex: 1; overflow: hidden; }

    /* ---- sidebar ---- */
    #sidebar {
      width: var(--sidebar-w);
      background: var(--surface);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      flex-shrink: 0;
      overflow: hidden;
    }

    #sidebar-filters {
      display: flex;
      gap: 4px;
      padding: 8px 10px 4px;
      flex-shrink: 0;
      border-bottom: 1px solid var(--border);
    }
    .filter-btn {
      flex: 1;
      background: var(--surface2);
      border: none;
      border-radius: 12px;
      color: var(--text-muted);
      cursor: pointer;
      font-size: 12px;
      padding: 4px 0;
      margin: 0;
    }
    .filter-btn.active { background: var(--accent); color: #fff; }
    .filter-btn:hover:not(.active) { background: var(--border); color: var(--text); }

    #search-box { padding: 10px; display: flex; gap: 6px; align-items: center; }
    #search-input {
      flex: 1;
      height: 36px;
      background: var(--surface2);
      border: none;
      border-radius: 8px;
      padding: 8px 12px;
      color: var(--text);
      font-size: 14px;
      outline: none;
      margin: 0;
    }
    #search-input:focus { box-shadow: 0 0 0 2px var(--accent); }
    #settings-btn {
      background: none; border: none; cursor: pointer;
      color: var(--text-muted); padding: 4px; border-radius: 6px;
      display: flex; align-items: center; justify-content: center; flex-shrink: 0;
      margin: 0;
    }
    #settings-btn:hover { color: var(--text); background: var(--surface2); }

    /* ---- settings modal ---- */
    #settings-modal {
      position: fixed; inset: 0; background: rgba(0,0,0,0.5);
      z-index: 950; display: none;
      align-items: center; justify-content: center;
    }
    #settings-modal.open { display: flex; }
    #settings-panel {
      background: var(--surface); border-radius: 12px;
      width: 360px; max-width: 90vw; padding: 0;
      box-shadow: 0 8px 32px rgba(0,0,0,0.4);
    }
    #settings-header {
      display: flex; align-items: center; justify-content: space-between;
      padding: 16px 20px; border-bottom: 1px solid var(--border);
      font-weight: 600; font-size: 16px;
    }
    #settings-close {
      background: none; border: none; cursor: pointer;
      color: var(--text-muted); font-size: 18px; padding: 2px 6px; border-radius: 4px;
      margin: 0; line-height: 1;
    }
    #settings-close:hover { color: var(--text); background: var(--surface2); }
    .settings-section { padding: 16px 20px; border-bottom: 1px solid var(--border); }
    .settings-section:last-child { border-bottom: none; }
    .settings-label { font-size: 13px; color: var(--text-muted); margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.05em; }
    .settings-row { display: flex; gap: 8px; flex-wrap: wrap; }
    .pref-btn {
      background: var(--surface2); border: 1px solid var(--border);
      color: var(--text); border-radius: 8px; padding: 7px 14px;
      font-size: 13px; cursor: pointer; margin: 0;
    }
    .pref-btn:hover { border-color: var(--accent); }
    .pref-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }

    /* ---- chat list ---- */
    #chat-list { flex: 1; overflow-y: auto; }
    #chat-list-notice {
      padding: 24px 16px; color: var(--text-muted);
      font-size: 13px; text-align: center;
      display: flex; flex-direction: column; align-items: center; gap: 10px;
    }
    #chat-list-notice .loading-spinner {
      width: 22px; height: 22px;
      border: 2px solid var(--border);
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }

    .section-label {
      padding: 8px 16px 4px;
      font-size: 11px;
      font-weight: 600;
      color: var(--accent);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }

    .chat-item {
      padding: 12px 16px;
      cursor: pointer;
      border-bottom: 1px solid var(--border);
      transition: background 0.15s;
    }
    .chat-item:hover { background: var(--surface2); }
    .chat-item.active { background: var(--surface2); border-left: 3px solid var(--accent); }
    .chat-item .chat-name { font-size: var(--font-size); font-weight: 500; margin-bottom: 2px; }
    .chat-item .chat-meta { font-size: 12px; color: var(--text-muted); }
    .chat-item.group .chat-name::before { content: '👥 '; }

    #search-results { display: none; }
    #search-results.has-results { display: block; }
    #search-results-header { padding: 8px 16px 4px; font-size: 11px; color: var(--text-muted); }

    /* ---- chat pane ---- */
    #chat-pane {
      flex: 1;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      position: relative;
    }

    #chat-header {
      background: var(--surface);
      padding: 8px 16px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      gap: 10px;
      flex-shrink: 0;
      flex-wrap: wrap;
    }
    #chat-header h2 { font-size: 15px; font-weight: 600; flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

    #chat-toolbar {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-shrink: 0;
    }

    #toolbar-toggle {
      background: none; border: none;
      color: var(--text-muted); cursor: pointer;
      font-size: 18px; padding: 4px 6px;
      border-radius: 4px; line-height: 1; margin: 0;
    }
    #toolbar-toggle:hover { background: var(--surface2); color: var(--text); }

    #toolbar-expanded { display: none; align-items: center; gap: 8px; }
    #toolbar-expanded.open { display: flex; }

    #chat-search-wrap { position: relative; display: flex; align-items: center; }
    #chat-search-input {
      background: var(--surface2); border: none;
      border-radius: 6px; padding: 5px 28px 5px 10px;
      color: var(--text); font-size: 13px;
      outline: none; width: 180px; height: 28px; margin: 0;
    }
    #chat-search-input:focus { box-shadow: 0 0 0 2px var(--accent); }
    #chat-search-clear {
      position: absolute; right: 6px;
      background: none; border: none; cursor: pointer;
      color: var(--text-muted); font-size: 14px; line-height: 1;
      padding: 0; display: none; margin: 0;
    }
    #chat-search-clear:hover { color: var(--text); }
    #date-go-btn {
      background: var(--accent); border: none; border-radius: 6px;
      color: #fff; font-size: 12px; padding: 5px 10px; cursor: pointer;
      display: none; margin: 0;
    }
    #date-go-btn:hover { opacity: 0.85; }
    #date-clear-btn {
      background: var(--surface2); border: none; border-radius: 6px;
      color: var(--text-muted); font-size: 12px; padding: 5px 10px;
      cursor: pointer; display: none; margin: 0;
    }
    #date-clear-btn:hover { background: var(--surface); color: var(--text); }

    #chat-search-nav { display: flex; gap: 2px; align-items: center; }
    #chat-search-count { font-size: 12px; color: var(--text-muted); min-width: 50px; text-align: center; }
    .nav-btn {
      background: var(--surface2); border: none;
      border-radius: 4px; color: var(--text);
      cursor: pointer; padding: 4px 8px;
      font-size: 14px; line-height: 1; margin: 0;
    }
    .nav-btn:hover { background: var(--accent); color: #fff; }
    .nav-btn:disabled { opacity: 0.3; cursor: default; }

    #date-picker-input {
      background: var(--surface2); border: none;
      border-radius: 6px; padding: 5px 8px;
      color: var(--text); font-size: 13px;
      outline: none; color-scheme: dark; margin: 0; height: 28px;
    }
    #date-picker-input:focus { box-shadow: 0 0 0 2px var(--accent); }
    #date-picker-input::-webkit-calendar-picker-indicator { cursor: pointer; opacity: 0.6; }
    #date-picker-input::-webkit-clear-button { display: none; }

    /* ---- messages ---- */
    #message-scroll {
      flex: 1; overflow-y: auto;
      padding: 16px;
      display: flex; flex-direction: column; gap: 4px;
    }

    .msg-row { display: flex; flex-direction: column; margin-bottom: 2px; }
    .msg-row.sent { align-items: flex-end; }
    .msg-row.recv { align-items: flex-start; }

    .msg-bubble {
      max-width: 70%;
      padding: 6px 10px 8px;
      border-radius: 7px;
      position: relative;
    }
    .msg-row.sent .msg-bubble {
      background: var(--bubble-out);
      color: var(--bubble-out-text);
      border-bottom-right-radius: 2px;
    }
    .msg-row.recv .msg-bubble {
      background: var(--bubble-in);
      color: var(--bubble-in-text);
      border-bottom-left-radius: 2px;
    }

    .msg-sender { font-size: 11px; font-weight: 600; color: var(--accent); margin-bottom: 2px; }
    .msg-text { font-size: var(--font-size); line-height: 1.4; white-space: pre-wrap; word-break: break-word; }
    .msg-unavailable { color: var(--text-muted); font-style: italic; }
    .msg-caption { font-size: var(--font-size); line-height: 1.4; margin-top: 4px; }
    .msg-meta { font-size: 10px; color: var(--text-muted); text-align: right; margin-top: 2px; }

    .msg-row.sent .msg-meta { color: rgba(17,27,33,0.5); }
    .msg-row.recv .msg-meta { color: rgba(17,27,33,0.5); }

    .msg-media { border-radius: 4px; overflow: hidden; max-width: 100%; }
    .msg-media img, .msg-media video { display: block; max-width: 320px; max-height: 300px; width: 100%; height: auto; cursor: pointer; }
    .msg-media audio { display: block; max-width: 300px; width: 100%; margin: 4px 0; }
    .msg-media.sticker img { max-width: 180px; max-height: 180px; }

    .doc-link { display: flex; align-items: center; gap: 8px; padding: 4px; font-size: 13px; text-decoration: none; }
    .doc-link .doc-icon { font-size: 20px; }

    /* ---- empty / loading states ---- */
    #empty-pane {
      flex: 1; display: flex;
      align-items: center; justify-content: center;
      color: var(--text-muted); font-size: 15px;
    }

    #chat-loading {
      display: none; flex: 1; flex-direction: column;
      align-items: center; justify-content: center;
      gap: 12px; color: var(--text-muted); font-size: 14px;
    }
    #chat-loading .loading-spinner {
      width: 28px; height: 28px;
      border: 3px solid var(--border);
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    #chat-loading-count { font-size: 12px; color: var(--text-muted); }
    .index-spinner {
      display: inline-block;
      width: 10px; height: 10px;
      border: 2px solid var(--text-muted);
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
      margin-left: 6px;
      vertical-align: middle;
      flex-shrink: 0;
    }

    .spinner { display: flex; justify-content: center; padding: 16px; color: var(--text-muted); font-size: 13px; }

    /* ---- lightbox ---- */
    .img-lightbox {
      position: fixed; inset: 0; background: rgba(0,0,0,0.9);
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      z-index: 1000;
    }
    .img-lightbox img { max-width: 90vw; max-height: 85vh; object-fit: contain; }
    .img-lightbox video { max-width: 90vw; max-height: 85vh; }
    .lb-timestamp { color: rgba(255,255,255,0.7); font-size: 12px; margin-bottom: 10px; letter-spacing: 0.02em; }
    .lb-close {
      position: absolute; top: 16px; right: 20px;
      background: none; border: none; color: #fff; font-size: 28px;
      cursor: pointer; line-height: 1; opacity: 0.8; z-index: 1;
      padding: 0; margin: 0;
    }
    .lb-close:hover { opacity: 1; }
    .lb-arrow {
      position: absolute; top: 50%; transform: translateY(-50%);
      background: rgba(255,255,255,0.12); border: none; color: #fff;
      font-size: 32px; cursor: pointer; padding: 12px 16px;
      border-radius: 6px; line-height: 1; opacity: 0.7; z-index: 1;
      transition: opacity 0.15s, background 0.15s; margin: 0;
    }
    .lb-arrow:hover { opacity: 1; background: rgba(255,255,255,0.22); }
    .lb-arrow:disabled { opacity: 0.15; cursor: default; }
    .lb-arrow.prev { left: 16px; }
    .lb-arrow.next { right: 16px; }

    /* ---- scroll-to-bottom ---- */
    #scroll-to-bottom {
      position: absolute; bottom: 18px; right: 18px;
      width: 34px; height: 34px;
      background: linear-gradient(135deg, #00e676, #00a884);
      color: #fff; border: none; border-radius: 6px;
      font-size: 18px; line-height: 34px; text-align: center;
      cursor: pointer; box-shadow: 0 2px 6px rgba(0,0,0,0.3);
      display: none; z-index: 10;
      opacity: 0.55; transition: opacity 0.15s; margin: 0; padding: 0;
    }
    #scroll-to-bottom:hover { opacity: 1; }

    /* ---- media gallery ---- */
    #media-btn {
      background: none; border: none;
      color: var(--text-muted); cursor: pointer;
      font-size: 16px; padding: 4px 6px;
      border-radius: 4px; line-height: 1; margin: 0;
    }
    #media-btn:hover { background: var(--surface2); color: var(--text); }

    #media-gallery {
      position: fixed; inset: 0; background: var(--bg); z-index: 900;
      display: none; flex-direction: column;
    }
    #media-gallery.open { display: flex; }
    #media-gallery-header {
      display: flex; align-items: center; padding: 10px 16px;
      background: var(--surface); border-bottom: 1px solid var(--border);
      flex-shrink: 0;
    }
    #media-gallery-title { flex: 1; font-weight: 600; font-size: 15px; }
    #media-gallery-close {
      background: none; border: none; color: var(--text-muted);
      font-size: 18px; cursor: pointer; padding: 4px 6px; border-radius: 4px; margin: 0;
    }
    #media-gallery-close:hover { background: var(--surface2); }
    #media-view-switcher {
      display: flex; gap: 4px; margin-right: 4px;
      background: var(--surface2); border-radius: 8px; padding: 3px;
    }
    .media-view-btn {
      background: none; border: none; cursor: pointer;
      color: var(--text-muted); border-radius: 6px;
      padding: 5px 8px; font-size: 18px; line-height: 1;
      transition: background 0.15s, color 0.15s; margin: 0;
    }
    .media-view-btn:hover { background: var(--surface); color: var(--text); }
    .media-view-btn.active { background: var(--accent); color: #fff; }
    #media-archive-view { display: none; flex-direction: column; flex: 1; overflow: hidden; }
    #media-archive-view.active { display: flex; }
    #media-archive-toolbar {
      display: flex; align-items: center; justify-content: space-between;
      padding: 6px 12px; background: var(--surface);
      border-bottom: 1px solid var(--border); flex-shrink: 0;
    }
    #media-archive-tabs { display: flex; gap: 4px; }
    .archive-tab {
      background: none; border: none; padding: 4px 12px; cursor: pointer;
      color: var(--text-muted); border-radius: 4px; font-size: 13px; margin: 0;
    }
    .archive-tab.active { background: var(--surface2); color: var(--text); font-weight: 600; }
    #media-archive-actions { display: flex; gap: 6px; }
    #media-archive-actions button {
      font-size: 12px; background: none; border: 1px solid var(--border);
      color: var(--text-muted); border-radius: 4px; padding: 2px 8px; cursor: pointer; margin: 0;
    }
    #media-archive-tree { overflow-y: auto; padding: 8px; flex: 1; }
    .archive-year { margin-bottom: 8px; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
    .archive-year-header {
      display: flex; align-items: center; padding: 8px 12px;
      background: var(--surface); cursor: pointer; user-select: none;
      font-weight: 600; font-size: 13px; gap: 6px;
    }
    .archive-year-header:hover { background: var(--surface2); }
    .archive-year-chevron { font-size: 10px; transition: transform 0.15s; }
    .archive-year.open .archive-year-chevron { transform: rotate(90deg); }
    .archive-year-grid {
      display: none;
      grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
      gap: 4px; padding: 8px; background: var(--bg); align-items: start;
    }
    .archive-year.open .archive-year-grid { display: grid; }
    #media-gallery-stats {
      padding: 8px 16px; background: var(--surface);
      border-bottom: 1px solid var(--border);
      font-size: 12px; color: var(--text-muted);
      display: flex; flex-wrap: wrap; gap: 6px 16px; flex-shrink: 0;
    }
    .gallery-stat { white-space: nowrap; }
    .gallery-stat-missing { color: var(--text-muted); font-style: italic; }
    #media-gallery-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
      gap: 4px; overflow-y: auto; padding: 8px;
    }
    .gallery-month-header {
      grid-column: 1 / -1;
      padding: 8px 4px 4px;
      font-size: 13px; font-weight: 600; color: var(--text-muted);
      border-bottom: 1px solid var(--border); margin-bottom: 2px;
    }
    .gallery-item {
      position: relative; height: 120px; cursor: pointer;
      background: var(--surface); overflow: hidden; border-radius: 4px;
    }
    .gallery-item img, .gallery-item video { width: 100%; height: 100%; object-fit: cover; display: block; }
    .gallery-item .gallery-doc {
      display: flex; flex-direction: column; align-items: center;
      justify-content: center; height: 100%; font-size: 12px;
      color: var(--text-muted); padding: 4px; text-align: center; word-break: break-all;
    }
    .gallery-goto {
      position: absolute; bottom: 4px; right: 4px;
      background: rgba(0,0,0,0.6); color: #fff; border: none;
      border-radius: 4px; font-size: 11px; padding: 2px 5px; cursor: pointer;
      opacity: 0; transition: opacity 0.15s; margin: 0;
    }
    .gallery-item:hover .gallery-goto { opacity: 1; }

    /* ---- search results ---- */
    .search-result-item {
      padding: 10px 16px; cursor: pointer;
      border-bottom: 1px solid var(--border); font-size: 13px;
    }
    .search-result-item:hover { background: var(--surface2); }
    .search-result-item .sr-chat { font-weight: 600; font-size: 12px; margin-bottom: 2px; }
    .search-result-item .sr-text { color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .search-result-item .sr-time { font-size: 11px; color: var(--text-muted); margin-top: 2px; }
    mark { background: #ffe082; color: #111; border-radius: 2px; padding: 0 1px; }

    .sr-section-label {
      padding: 6px 16px 4px; font-size: 10px; font-weight: 700;
      color: var(--accent); text-transform: uppercase; letter-spacing: 0.6px;
      border-top: 1px solid var(--border);
    }
    .sr-section-label:first-child { border-top: none; }

    .search-index-notice {
      padding: 8px 16px; font-size: 11px;
      color: var(--text-muted); border-top: 1px solid var(--border); font-style: italic;
    }

    /* ---- misc ---- */
    .direction-badge {
      display: inline-block; font-size: 10px;
      padding: 1px 4px; border-radius: 3px;
      margin-right: 4px; vertical-align: middle;
    }
    .direction-badge.sent { background: var(--accent); color: #fff; }

    .date-separator { display: flex; align-items: center; justify-content: center; margin: 8px 0; }
    .date-separator span {
      background: var(--surface2); color: var(--text-muted);
      font-size: 11px; padding: 3px 10px; border-radius: 8px;
    }

    .load-spinner { display: flex; justify-content: center; padding: 10px; color: var(--text-muted); font-size: 20px; }

    .msg-quote {
      background: rgba(0,0,0,0.08);
      border-left: 3px solid var(--accent);
      border-radius: 4px; padding: 4px 8px; margin-bottom: 4px;
      max-width: 100%; overflow: hidden;
    }
    .msg-row.sent .msg-quote { background: rgba(0,0,0,0.1); }
    .msg-quote-sender { font-size: 11px; font-weight: 600; color: var(--accent); margin-bottom: 1px; }
    .msg-quote-text { font-size: 12px; color: rgba(17,27,33,0.75); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .msg-quote[style*="cursor: pointer"]:hover { background: rgba(0,0,0,0.15); }

    .msg-bubble.search-highlight { outline: 2px solid var(--accent); }
  </style>
</head>
<body>
<div id="app">
  <div id="header">
    <h1>WA Chat Viewer</h1>
    <span class="subtitle">{{ output_root }}</span>
  </div>

  <div id="body">
    <div id="sidebar">
      <div id="search-box">
        <input id="search-input" type="search" placeholder="Search messages…" autocomplete="off">
        <button id="settings-btn" title="Settings"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></button>
      </div>
      <div id="sidebar-filters">
        <button class="filter-btn active" data-filter="all">All</button>
        <button class="filter-btn" data-filter="contact">Chats</button>
        <button class="filter-btn" data-filter="group">Groups</button>
      </div>
      <div id="search-results"></div>
      <div id="chat-list"></div>
    </div>

    <div id="chat-pane">
      <div id="chat-header" style="display:none;">
        <h2 id="chat-title"></h2>
        <div id="chat-toolbar">
          <button id="media-btn" title="Media">&#128247;</button>
          <button id="toolbar-toggle" title="Search &amp; date">&#128269;</button>
          <div id="toolbar-expanded">
            <div id="chat-search-wrap">
              <input id="chat-search-input" type="search" placeholder="Find in chat…" autocomplete="off">
              <button id="chat-search-clear" title="Clear search">&#10005;</button>
            </div>
            <div id="chat-search-nav" style="display:none;">
              <button class="nav-btn" id="search-prev" title="Previous">&#8679;</button>
              <span id="chat-search-count"></span>
              <button class="nav-btn" id="search-next" title="Next">&#8681;</button>
            </div>
            <input id="date-picker-input" type="date" title="Jump to date">
            <button id="date-go-btn">Go</button>
            <button id="date-clear-btn">Clear</button>
          </div>
        </div>
      </div>
      <div id="message-scroll"></div>
      <button id="scroll-to-bottom" title="Jump to latest">&#8627;</button>
      <div id="empty-pane">Select a chat to browse messages</div>
      <div id="chat-loading">
        <div class="loading-spinner"></div>
        <span>Loading chat…</span>
        <span id="chat-loading-count"></span>
      </div>
    </div>
  </div>
</div>

<div id="media-gallery">
  <div id="media-gallery-header">
    <span id="media-gallery-title">Media</span>
    <div id="media-view-switcher" style="display:none">
      <button class="media-view-btn active" data-view="grid" title="Grid view">&#8862;</button>
      <button class="media-view-btn" data-view="archive" title="Archive view">&#128193;</button>
    </div>
    <button id="media-gallery-close" title="Close">&#10005;</button>
  </div>
  <div id="media-gallery-stats"></div>
  <div id="media-gallery-grid"></div>
  <div id="media-archive-view">
    <div id="media-archive-toolbar">
      <div id="media-archive-tabs">
        <button class="archive-tab active" data-dir="Received">Received</button>
        <button class="archive-tab" data-dir="Sent">Sent</button>
      </div>
      <div id="media-archive-actions">
        <button id="archive-expand-all">Expand all</button>
        <button id="archive-collapse-all">Collapse all</button>
      </div>
    </div>
    <div id="media-archive-tree"></div>
  </div>
</div>

<div id="settings-modal">
  <div id="settings-panel">
    <div id="settings-header">
      <span>Settings</span>
      <button id="settings-close">&#10005;</button>
    </div>
    <div class="settings-section">
      <div class="settings-label">Theme</div>
      <div class="settings-row">
        <button class="pref-btn" data-pref="theme" data-value="dark">Dark</button>
        <button class="pref-btn" data-pref="theme" data-value="light">Light</button>
      </div>
    </div>
    <div class="settings-section">
      <div class="settings-label">Date format</div>
      <div class="settings-row">
        <button class="pref-btn" data-pref="date_format" data-value="DD/MM/YYYY">DD/MM/YYYY &nbsp;<em>25/07/2026</em></button>
        <button class="pref-btn" data-pref="date_format" data-value="MM/DD/YYYY">MM/DD/YYYY &nbsp;<em>07/25/2026</em></button>
        <button class="pref-btn" data-pref="date_format" data-value="YYYY/MM/DD">YYYY/MM/DD &nbsp;<em>2026/07/25</em></button>
      </div>
    </div>
    <div class="settings-section">
      <div class="settings-label">Font size</div>
      <div class="settings-row">
        <button class="pref-btn" data-pref="font_size" data-value="small">Small</button>
        <button class="pref-btn" data-pref="font_size" data-value="medium">Medium</button>
        <button class="pref-btn" data-pref="font_size" data-value="large">Large</button>
      </div>
    </div>
  </div>
</div>

<script>
(function () {
  'use strict';

  const outputRoot = {{ output_root | tojson }};
  let allChats = [];
  let currentChat = null;
  let msgList = [];
  let domNodes = 0;
  let prefs = { theme: 'dark', date_format: 'DD/MM/YYYY', font_size: 'medium' };

  async function loadPrefs() {
    const res = await fetch('/api/preferences');
    prefs = await res.json();
    applyPrefs();
  }

  function applyPrefs() {
    document.documentElement.dataset.theme = prefs.theme;
    const scheme = prefs.theme === 'light' ? 'light' : 'dark';
    document.documentElement.style.colorScheme = scheme;
    document.getElementById('meta-color-scheme').content = scheme;
    document.getElementById('date-picker-input').style.colorScheme = scheme;
    const sizeMap = { small: '13px', medium: '15px', large: '17px' };
    document.documentElement.style.setProperty('--font-size', sizeMap[prefs.font_size] || '15px');
    document.querySelectorAll('.pref-btn').forEach(btn => {
      btn.classList.toggle('active', prefs[btn.dataset.pref] === btn.dataset.value);
    });
  }
  const MAX_DOM = 100;

  // ---- util ----------------------------------------------------------------

  function fmtTime(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  function fmtDateSep(ts) {
    const d = new Date(ts);
    const dd = String(d.getDate()).padStart(2, '0');
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const yyyy = String(d.getFullYear());
    if (prefs.date_format === 'MM/DD/YYYY') return `${mm}/${dd}/${yyyy}`;
    if (prefs.date_format === 'YYYY/MM/DD') return `${yyyy}/${mm}/${dd}`;
    return `${dd}/${mm}/${yyyy}`;
  }

  function dayKey(ts) {
    const d = new Date(ts);
    return d.getFullYear() * 10000 + (d.getMonth() + 1) * 100 + d.getDate();
  }

  function monthKey(ts) {
    const d = new Date(ts);
    return d.getFullYear() * 100 + (d.getMonth() + 1);
  }

  function fmtMonthHeader(ts) {
    const d = new Date(ts);
    if (d.getFullYear() === new Date().getFullYear()) {
      return d.toLocaleString('default', { month: 'long' });
    }
    return d.toLocaleString('default', { month: 'long', year: 'numeric' });
  }

  function archiveSegments(archivePath) {
    const parts = archivePath.split('/');
    return { year: parts[2], direction: parts[3] };
  }

  function buildArchiveView(items, direction) {
    const tree = document.getElementById('media-archive-tree');
    tree.innerHTML = '';

    const filtered = items.filter(m => m.archive_path &&
      archiveSegments(m.archive_path).direction === direction);

    if (!filtered.length) {
      tree.innerHTML = '<div style="color:var(--text-muted);padding:16px">No media.</div>';
      return;
    }

    const byYear = {};
    for (const msg of filtered) {
      const { year } = archiveSegments(msg.archive_path);
      (byYear[year] = byYear[year] || []).push(msg);
    }

    for (const year of Object.keys(byYear).sort((a, b) => b - a)) {
      const block = document.createElement('div');
      block.className = 'archive-year open';

      const hdr = document.createElement('div');
      hdr.className = 'archive-year-header';
      hdr.innerHTML =
        `<span class="archive-year-chevron">&#9658;</span><span>${year}</span>` +
        `<span style="margin-left:auto;font-weight:400;color:var(--text-muted);font-size:12px">` +
        `${byYear[year].length}</span>`;
      hdr.addEventListener('click', () => block.classList.toggle('open'));

      const grid = document.createElement('div');
      grid.className = 'archive-year-grid';

      let lastMonth = null;
      for (const msg of byYear[year]) {
        const mk = monthKey(msg.timestamp_ms);
        if (mk !== lastMonth) {
          const mhdr = document.createElement('div');
          mhdr.className = 'gallery-month-header';
          mhdr.textContent = new Date(msg.timestamp_ms).toLocaleString('default', { month: 'long' });
          grid.appendChild(mhdr);
          lastMonth = mk;
        }
        grid.appendChild(renderGalleryItem(msg));
      }

      block.appendChild(hdr);
      block.appendChild(grid);
      tree.appendChild(block);
    }
  }

  function esc(s) {
    if (s == null) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function highlight(text, q) {
    if (!q || !text) return esc(text || '');
    const words = q.trim().split(/\s+/);
    let result = esc(text);
    for (const w of words) {
      const re = new RegExp(w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
      result = result.replace(re, m => '<mark>' + m + '</mark>');
    }
    return result;
  }

  function makeDateSeparator(ts) {
    const el = document.createElement('div');
    el.className = 'date-separator';
    el.dataset.dayKey = dayKey(ts);
    el.innerHTML = `<span>${fmtDateSep(ts)}</span>`;
    return el;
  }

  // ---- load chats ----------------------------------------------------------

  async function loadChats() {
    document.getElementById('chat-list').innerHTML = '<div id="chat-list-notice"><div class="loading-spinner"></div>Loading Chats…</div>';
    const res = await fetch('/api/chats');
    allChats = await res.json();
    renderChatList();
  }

  let currentFilter = 'all';

  document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      currentFilter = btn.dataset.filter;
      document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      renderChatList();
    });
  });

  function _startIndexPoll(chat, sidebarEl) {
    const params = new URLSearchParams({chat_id: chat.id, chat_type: chat.type});
    async function poll() {
      try {
        const r = await fetch('/api/chat-index-status?' + params);
        const d = await r.json();
        if (d.status === 'done') {
          sidebarEl.querySelector('.index-spinner')?.remove();
          return;
        }
        if (!sidebarEl.querySelector('.index-spinner')) {
          const sp = document.createElement('span');
          sp.className = 'index-spinner';
          sidebarEl.querySelector('.chat-name').appendChild(sp);
        }
        setTimeout(poll, 500);
      } catch (_) {
        setTimeout(poll, 1000);
      }
    }
    poll();
  }

  const MEDIA_LABELS = {image:'📷 Photo', video:'🎥 Video', audio:'🎵 Voice message',
                        gif:'🎞 GIF', sticker:'🎭 Sticker', document:'📄 Document'};
  function chatPreview(chat) {
    const prefix = chat.last_msg_from_me ? 'You: ' : '';
    if (chat.last_msg_type !== 'text') {
      return prefix + (MEDIA_LABELS[chat.last_msg_type] || '📎 Media');
    }
    const text = chat.last_msg_preview || '';
    return (prefix + esc(text.slice(0, 60))) || '…';
  }

  function renderChatList() {
    const list = document.getElementById('chat-list');
    list.innerHTML = '';
    const visible = currentFilter === 'all' ? allChats : allChats.filter(c => c.type === currentFilter);
    visible.forEach(chat => {
      const el = document.createElement('div');
      el.className = 'chat-item' + (chat.type === 'group' ? ' group' : '');
      el.dataset.id = chat.id;
      el.dataset.type = chat.type;
      el.innerHTML = `<div class="chat-name">${esc(chat.display_name)}</div><div class="chat-meta">${chatPreview(chat)} · ${fmtTime(chat.newest_ts)}</div>`;
      el.addEventListener('click', () => selectChat(chat, el));
      list.appendChild(el);
    });
  }

  // ---- select chat ---------------------------------------------------------

  async function selectChat(chat, el) {
    document.querySelectorAll('.chat-item').forEach(e => e.classList.remove('active'));
    el.classList.add('active');
    currentChat = chat;
    msgList = [];

    const scroll = document.getElementById('message-scroll');
    scroll.innerHTML = '';
    domNodes = 0;
    document.getElementById('scroll-to-bottom').style.display = 'none';

    document.getElementById('chat-header').style.display = '';
    document.getElementById('chat-title').textContent = chat.display_name;
    document.getElementById('empty-pane').style.display = 'none';
    document.getElementById('search-results').classList.remove('has-results');
    document.getElementById('search-results').innerHTML = '';

    // reset in-chat search and date picker when switching chats
    document.getElementById('chat-search-input').value = '';
    document.getElementById('chat-search-clear').style.display = 'none';
    clearChatSearch();
    document.getElementById('date-picker-input').value = '';
    document.getElementById('date-go-btn').style.display = 'none';
    document.getElementById('date-clear-btn').style.display = 'none';

    const loadingEl = document.getElementById('chat-loading');
    loadingEl.style.display = 'flex';

    await loadMessages('older');

    loadingEl.style.display = 'none';
    scroll.scrollTop = scroll.scrollHeight;

    _startIndexPoll(chat, el);
  }

  // ---- load messages -------------------------------------------------------

  let loading = false;

  function showSpinner(position) {
    const el = document.createElement('div');
    el.className = 'load-spinner';
    el.id = 'load-spinner-' + position;
    el.textContent = '⟳';
    const scroll = document.getElementById('message-scroll');
    if (position === 'top') scroll.insertBefore(el, scroll.firstChild);
    else scroll.appendChild(el);
    return el;
  }

  function removeSpinner(position) {
    const el = document.getElementById('load-spinner-' + position);
    if (el) el.remove();
  }

  async function loadMessages(direction) {
    if (loading) return;
    loading = true;

    const params = new URLSearchParams({
      chat_id: currentChat.id,
      chat_type: currentChat.type,
      limit: 50
    });

    if (direction === 'older' && msgList.length > 0) {
      params.set('before', msgList[0].timestamp_ms);
    } else if (direction === 'newer' && msgList.length > 0) {
      params.set('after', msgList[msgList.length - 1].timestamp_ms);
    }

    const spinner = showSpinner(direction === 'older' ? 'top' : 'bottom');

    try {
      const res = await fetch('/api/messages?' + params);
      const msgs = await res.json();
      removeSpinner(direction === 'older' ? 'top' : 'bottom');

      if (!msgs.length) { loading = false; return; }

      const scroll = document.getElementById('message-scroll');

      if (direction === 'older') {
        // API returns DESC; reverse to get chronological order for prepending
        const ordered = msgs.slice().reverse();
        // The message currently at the top of our list is the boundary for date seps
        const firstExistingTs = msgList.length > 0 ? msgList[0].timestamp_ms : null;

        // Prepend into msgList
        for (let i = ordered.length - 1; i >= 0; i--) {
          msgList.unshift(ordered[i]);
        }

        const prevHeight = scroll.scrollHeight;
        const prevTop = scroll.scrollTop;

        // Insert into DOM oldest-first (each goes before the current firstChild)
        // so final order is oldest-at-top. For date sep: compare each msg with
        // the one that comes after it in the DOM (i.e. ordered[i+1] or firstExistingTs).
        for (let i = ordered.length - 1; i >= 0; i--) {
          const m = ordered[i];
          const nextTs = i < ordered.length - 1 ? ordered[i + 1].timestamp_ms : firstExistingTs;
          const nodes = renderBubbleWithSep(m, nextTs, 'before');
          nodes.forEach(node => scroll.insertBefore(node, scroll.firstChild));
        }

        domNodes += ordered.length;
        scroll.scrollTop = prevTop + (scroll.scrollHeight - prevHeight);
        pruneDom('top');
      } else {
        const prevLastTs = msgList.length > 0 ? msgList[msgList.length - 1].timestamp_ms : null;
        msgs.forEach(m => msgList.push(m));
        msgs.forEach((m, i) => {
          const prevTs = i === 0 ? prevLastTs : msgs[i - 1].timestamp_ms;
          renderBubbleWithSep(m, prevTs, 'after').forEach(node => scroll.appendChild(node));
        });
        domNodes += msgs.length;
        pruneDom('bottom');
      }

    } catch (e) {
      removeSpinner(direction === 'older' ? 'top' : 'bottom');
    }
    loading = false;
  }

  function renderBubbleWithSep(msg, prevTs, direction) {
    const nodes = [];
    const needsSep = prevTs === null || dayKey(msg.timestamp_ms) !== dayKey(prevTs);
    if (needsSep && direction === 'after') nodes.push(makeDateSeparator(msg.timestamp_ms));
    nodes.push(renderBubble(msg));
    // 'before': separator goes after the bubble so insertBefore puts it above the next row;
    // label uses prevTs (= the chronologically newer neighbour, start of that day)
    if (needsSep && direction === 'before' && prevTs !== null) nodes.push(makeDateSeparator(prevTs));
    return nodes;
  }

  // ---- append / prepend with pruning ---------------------------------------

  function pruneDom(keepEnd) {
    const scroll = document.getElementById('message-scroll');
    while (domNodes > MAX_DOM && scroll.children.length > 0) {
      const child = keepEnd === 'top' ? scroll.lastChild : scroll.firstChild;
      if (!child) break;
      scroll.removeChild(child);
      if (child.classList && child.classList.contains('msg-row')) domNodes--;
    }
    // Re-sync msgList boundaries from surviving DOM rows
    const rows = scroll.querySelectorAll('.msg-row[data-ts]');
    if (rows.length === 0) { msgList = []; return; }
    const minTs = parseInt(rows[0].dataset.ts);
    const maxTs = parseInt(rows[rows.length - 1].dataset.ts);
    msgList = msgList.filter(m => m.timestamp_ms >= minTs && m.timestamp_ms <= maxTs);
  }

  // ---- scroll trigger -------------------------------------------------------

  function setupScrollTrigger() {
    const scroll = document.getElementById('message-scroll');
    const btn = document.getElementById('scroll-to-bottom');
    scroll.addEventListener('scroll', function () {
      const atBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 100;
      btn.style.display = atBottom ? 'none' : 'block';
      if (loading) return;
      if (scroll.scrollTop < 100) {
        loadMessages('older');
      } else if (atBottom) {
        loadMessages('newer');
      }
    });
    btn.addEventListener('click', () => {
      scroll.scrollTop = scroll.scrollHeight;
    });
  }
  setupScrollTrigger();

  function renderBubble(msg) {
    const row = document.createElement('div');
    row.className = 'msg-row ' + (msg.from_me ? 'sent' : 'recv');
    row.dataset.ts = msg.timestamp_ms;

    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble';

    const senderEl = document.createElement('div');
    senderEl.className = 'msg-sender';
    senderEl.textContent = msg.sender || '';
    if (msg.sender && !msg.from_me && currentChat && currentChat.type === 'group') bubble.appendChild(senderEl);

    if (msg.quoted_text) {
      const quote = document.createElement('div');
      quote.className = 'msg-quote';
      const qSender = document.createElement('div');
      qSender.className = 'msg-quote-sender';
      const qs = msg.quoted_sender;
      qSender.textContent = qs || (currentChat && currentChat.type === 'contact' ? currentChat.display_name : '');
      const qText = document.createElement('div');
      qText.className = 'msg-quote-text';
      qText.textContent = msg.quoted_text;
      quote.appendChild(qSender);
      quote.appendChild(qText);
      if (msg.quoted_ts) {
        quote.style.cursor = 'pointer';
        quote.title = 'Jump to original message';
        quote.addEventListener('click', () => jumpToTimestamp(msg.quoted_ts));
      }
      bubble.appendChild(quote);
    }

    const meta = document.createElement('div');
    meta.className = 'msg-meta';
    if (msg.from_me) {
      const badge = document.createElement('span');
      badge.className = 'direction-badge sent';
      badge.textContent = 'You';
      meta.appendChild(badge);
    }
    meta.appendChild(document.createTextNode(fmtTime(msg.timestamp_ms)));

    if (msg.media_type === 'text' || !msg.archive_path) {
      if (!msg.archive_path && msg.media_type !== 'text') {
        const txt = document.createElement('div');
        txt.className = 'msg-text msg-unavailable';
        const icon = msg.media_type === 'image' ? '🖼️' : msg.media_type === 'video' ? '🎥' :
                     msg.media_type === 'audio' ? '🎵' : msg.media_type === 'sticker' ? '🩹' :
                     msg.media_type === 'gif' ? '🎞️' : '📄';
        txt.textContent = icon + ' ' + (msg.text_body || 'Media not available');
        bubble.appendChild(txt);
      } else {
        const txt = document.createElement('div');
        txt.className = 'msg-text';
        txt.innerHTML = highlight(msg.text_body, '');
        bubble.appendChild(txt);
      }
    } else {
      const mediaEl = renderMedia(msg);
      bubble.appendChild(mediaEl);
      if (msg.text_body) {
        const cap = document.createElement('div');
        cap.className = 'msg-caption';
        cap.innerHTML = highlight(msg.text_body, '');
        bubble.appendChild(cap);
      }
    }

    bubble.appendChild(meta);
    row.appendChild(bubble);
    return row;
  }

  function renderMedia(msg) {
    const wrap = document.createElement('div');
    wrap.className = 'msg-media';
    const path = msg.archive_path;
    const src = '/media/' + path;
    const mt = msg.media_type;

    if (mt === 'image' || mt === 'gif' || mt === 'sticker') {
      const img = document.createElement('img');
      img.src = src;
      img.loading = 'lazy';
      img.alt = msg.media_name || 'image';
      img.addEventListener('click', () => showLightbox(src, mt));
      wrap.appendChild(img);
    } else if (mt === 'video') {
      const vid = document.createElement('video');
      vid.controls = true;
      vid.preload = 'none';
      vid.src = src;
      wrap.appendChild(vid);
    } else if (mt === 'audio') {
      const aud = document.createElement('audio');
      aud.controls = true;
      aud.src = src;
      wrap.appendChild(aud);
    } else {
      const a = document.createElement('a');
      a.href = src;
      a.className = 'doc-link';
      a.download = msg.media_name || 'file';
      a.innerHTML = `<span class="doc-icon">📄</span><span>${esc(msg.media_name || 'Download')}</span>`;
      wrap.appendChild(a);
    }
    return wrap;
  }

  let lightboxItems = [];
  let lightboxIndex = 0;

  function openLightboxAt(index) {
    document.getElementById('img-lightbox')?.remove();

    const msg = lightboxItems[index];
    const src = '/media/' + msg.archive_path;
    const mt = msg.media_type;

    const lb = document.createElement('div');
    lb.id = 'img-lightbox';
    lb.className = 'img-lightbox';

    const closeBtn = document.createElement('button');
    closeBtn.className = 'lb-close';
    closeBtn.innerHTML = '&#10005;';
    closeBtn.title = 'Close';
    closeBtn.addEventListener('click', () => lb.remove());
    lb.appendChild(closeBtn);

    const prevBtn = document.createElement('button');
    prevBtn.className = 'lb-arrow prev';
    prevBtn.innerHTML = '&#10094;';
    prevBtn.title = 'Previous';
    prevBtn.disabled = index === 0;
    prevBtn.addEventListener('click', e => { e.stopPropagation(); lightboxIndex--; openLightboxAt(lightboxIndex); });
    lb.appendChild(prevBtn);

    const nextBtn = document.createElement('button');
    nextBtn.className = 'lb-arrow next';
    nextBtn.innerHTML = '&#10095;';
    nextBtn.title = 'Next';
    nextBtn.disabled = index === lightboxItems.length - 1;
    nextBtn.addEventListener('click', e => { e.stopPropagation(); lightboxIndex++; openLightboxAt(lightboxIndex); });
    lb.appendChild(nextBtn);

    const tsEl = document.createElement('div');
    tsEl.className = 'lb-timestamp';
    tsEl.textContent = fmtTime(msg.timestamp_ms);
    lb.appendChild(tsEl);

    let media;
    if (mt === 'video') {
      media = document.createElement('video');
      media.controls = true;
      media.autoplay = true;
      media.src = src;
    } else {
      media = document.createElement('img');
      media.src = src;
    }
    lb.appendChild(media);

    lb.addEventListener('click', e => { if (e.target === lb) lb.remove(); });
    document.body.appendChild(lb);
  }

  function showLightbox(src, mt) {
    document.getElementById('img-lightbox')?.remove();
    const lb = document.createElement('div');
    lb.id = 'img-lightbox';
    lb.className = 'img-lightbox';

    const closeBtn = document.createElement('button');
    closeBtn.className = 'lb-close';
    closeBtn.innerHTML = '&#10005;';
    closeBtn.title = 'Close';
    closeBtn.addEventListener('click', () => lb.remove());
    lb.appendChild(closeBtn);

    let media;
    if (mt === 'video') {
      media = document.createElement('video');
      media.controls = true;
      media.autoplay = true;
      media.src = src;
    } else {
      media = document.createElement('img');
      media.src = src;
    }
    lb.appendChild(media);
    lb.addEventListener('click', e => { if (e.target === lb) lb.remove(); });
    document.body.appendChild(lb);
  }

  // ---- media gallery -------------------------------------------------------

  function closeMediaGallery() {
    document.getElementById('media-gallery').classList.remove('open');
  }

  function renderGalleryItem(msg) {
    const cell = document.createElement('div');
    cell.className = 'gallery-item';
    const src = '/media/' + msg.archive_path;
    const mt = msg.media_type;

    if (mt === 'image' || mt === 'gif' || mt === 'sticker') {
      const img = document.createElement('img');
      img.src = src;
      img.loading = 'lazy';
      img.alt = msg.media_name || '';
      const idx = lightboxItems.length;
      lightboxItems.push(msg);
      img.addEventListener('click', () => { lightboxIndex = idx; openLightboxAt(idx); });
      cell.appendChild(img);
    } else if (mt === 'video') {
      const vid = document.createElement('video');
      vid.src = src;
      vid.preload = 'none';
      const idx = lightboxItems.length;
      lightboxItems.push(msg);
      vid.addEventListener('click', () => { lightboxIndex = idx; openLightboxAt(idx); });
      cell.appendChild(vid);
    } else if (mt === 'audio') {
      const d = document.createElement('div');
      d.className = 'gallery-doc';
      d.innerHTML = '<span style="font-size:28px">🎵</span><span>' + esc(msg.media_name || 'audio') + '</span>';
      d.addEventListener('click', () => window.open(src, '_blank'));
      cell.appendChild(d);
    } else {
      const d = document.createElement('div');
      d.className = 'gallery-doc';
      d.innerHTML = '<span style="font-size:28px">📄</span><span>' + esc(msg.media_name || 'file') + '</span>';
      d.addEventListener('click', () => window.open(src, '_blank'));
      cell.appendChild(d);
    }

    const btn = document.createElement('button');
    btn.className = 'gallery-goto';
    btn.title = 'Go to message';
    btn.textContent = '→ in chat';
    btn.addEventListener('click', e => {
      e.stopPropagation();
      closeMediaGallery();
      jumpToTimestamp(msg.timestamp_ms);
    });
    cell.appendChild(btn);
    return cell;
  }

  async function openMediaGallery() {
    if (!currentChat) return;
    const grid = document.getElementById('media-gallery-grid');
    const stats = document.getElementById('media-gallery-stats');
    const switcher = document.getElementById('media-view-switcher');

    // reset to classic view each time
    archiveViewActive = false;
    switcher.style.display = currentChat.type === 'contact' ? '' : 'none';
    document.querySelectorAll('.media-view-btn').forEach((b, i) => b.classList.toggle('active', i === 0));
    grid.style.display = '';
    document.getElementById('media-archive-view').classList.remove('active');

    document.getElementById('media-gallery-title').textContent =
      currentChat.display_name ? `Media — ${currentChat.display_name}` : 'Media';

    grid.innerHTML = '<div style="color:var(--text-muted);padding:16px">Loading…</div>';
    stats.innerHTML = '';
    document.getElementById('media-gallery').classList.add('open');

    const r = await fetch(
      '/api/media?chat_id=' + encodeURIComponent(currentChat.id) +
      '&chat_type=' + encodeURIComponent(currentChat.type)
    );
    const items = await r.json();
    currentGalleryItems = items;
    grid.innerHTML = '';

    if (!items.length) {
      grid.innerHTML = '<div style="color:var(--text-muted);padding:16px">No media in this chat.</div>';
      return;
    }

    // compute stats
    const typeOrder = ['image', 'video', 'audio', 'gif', 'sticker', 'document'];
    const total = items.length;
    const totalMissing = items.filter(m => !m.archive_path).length;
    const byType = {};
    for (const m of items) {
      byType[m.media_type] = byType[m.media_type] || {count: 0, missing: 0};
      byType[m.media_type].count++;
      if (!m.archive_path) byType[m.media_type].missing++;
    }

    const totalMissingStr = totalMissing ? ` <span class="gallery-stat-missing">(${totalMissing} missing)</span>` : '';
    let html = `<span class="gallery-stat"><strong>${total}</strong> total${totalMissingStr}</span>`;
    for (const t of typeOrder) {
      if (!byType[t]) continue;
      const {count, missing} = byType[t];
      const missingStr = missing ? ` <span class="gallery-stat-missing">(${missing} missing)</span>` : '';
      html += `<span class="gallery-stat"><strong>${count}</strong> ${t}${missingStr}</span>`;
    }
    stats.innerHTML = html;

    lightboxItems = [];
    let lastMonthKey = null;
    for (const msg of items.filter(m => m.archive_path)) {
      const mk = monthKey(msg.timestamp_ms);
      if (mk !== lastMonthKey) {
        const hdr = document.createElement('div');
        hdr.className = 'gallery-month-header';
        hdr.textContent = fmtMonthHeader(msg.timestamp_ms);
        grid.appendChild(hdr);
        lastMonthKey = mk;
      }
      grid.appendChild(renderGalleryItem(msg));
    }
  }

  document.getElementById('media-btn').addEventListener('click', openMediaGallery);
  document.getElementById('media-gallery-close').addEventListener('click', closeMediaGallery);
  document.addEventListener('keydown', e => {
    if (document.getElementById('img-lightbox')) {
      if (e.key === 'Escape') { document.getElementById('img-lightbox').remove(); return; }
      if (e.key === 'ArrowLeft'  && lightboxIndex > 0) { lightboxIndex--; openLightboxAt(lightboxIndex); return; }
      if (e.key === 'ArrowRight' && lightboxIndex < lightboxItems.length - 1) { lightboxIndex++; openLightboxAt(lightboxIndex); return; }
    }
    if (e.key === 'Escape') closeMediaGallery();
  });

  let archiveViewActive = false;
  let currentGalleryItems = [];

  document.querySelectorAll('.media-view-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.media-view-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      archiveViewActive = btn.dataset.view === 'archive';
      document.getElementById('media-gallery-grid').style.display = archiveViewActive ? 'none' : '';
      document.getElementById('media-archive-view').classList.toggle('active', archiveViewActive);
      if (archiveViewActive) {
        const activeDir = document.querySelector('.archive-tab.active').dataset.dir;
        buildArchiveView(currentGalleryItems, activeDir);
      }
    });
  });

  document.querySelectorAll('.archive-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.archive-tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      buildArchiveView(currentGalleryItems, btn.dataset.dir);
    });
  });

  document.getElementById('archive-expand-all').addEventListener('click', () => {
    document.querySelectorAll('.archive-year').forEach(el => el.classList.add('open'));
  });
  document.getElementById('archive-collapse-all').addEventListener('click', () => {
    document.querySelectorAll('.archive-year').forEach(el => el.classList.remove('open'));
  });

  // ---- sidebar search -------------------------------------------------------

  let searchTimer = null;

  document.getElementById('search-input').addEventListener('input', function () {
    clearTimeout(searchTimer);
    const q = this.value.trim();
    if (!q) {
      clearSearchResults();
      return;
    }
    searchTimer = setTimeout(() => doSearch(q), 300);
  });

  async function doSearch(q) {
    // Contact name matches — client-side, instant
    const ql = q.toLowerCase();
    const contactMatches = allChats.filter(c =>
      c.display_name.toLowerCase().includes(ql) || c.id.toLowerCase().includes(ql)
    );

    // Show loading state while FTS fetch is in flight
    const container = document.getElementById('search-results');
    container.classList.add('has-results');
    container.innerHTML = '<div class="sr-section-label">Searching…</div>';

    // Full-text search across indexed chats
    const res = await fetch('/api/search?' + new URLSearchParams({ q }));
    const data = await res.json();

    renderSearchResults(q, contactMatches, data.results, data.indexed_count);
  }

  function clearSearchResults() {
    document.getElementById('search-results').classList.remove('has-results');
    document.getElementById('search-results').innerHTML = '';
  }

  function makeSectionLabel(text) {
    const el = document.createElement('div');
    el.className = 'sr-section-label';
    el.textContent = text;
    return el;
  }

  function makeContactResultItem(chat, q) {
    const el = document.createElement('div');
    el.className = 'search-result-item';
    el.innerHTML = `<div class="sr-chat">${highlight(chat.display_name, q)}</div><div class="sr-text">${esc(chat.id)}</div>`;
    el.addEventListener('click', () => {
      document.getElementById('search-input').value = '';
      clearSearchResults();
      const el2 = document.querySelector(`.chat-item[data-id="${chat.id}"][data-type="${chat.type}"]`);
      if (el2) selectChat(chat, el2);
    });
    return el;
  }

  function makeTextResultItem(r, q) {
    const el = document.createElement('div');
    el.className = 'search-result-item';
    const chatName = allChats.find(c => c.id === r.chat_id && c.type === r.chat_type)?.display_name || r.chat_id;
    const snippet = r.text_body || (r.media_name ? '[📎 ' + r.media_name + ']' : '');
    el.innerHTML = `
      <div class="sr-chat">${esc(chatName)}</div>
      <div class="sr-text">${highlight(snippet, q)}</div>
      <div class="sr-time">${fmtTime(r.timestamp_ms)}</div>
    `;
    el.addEventListener('click', () => {
      document.getElementById('search-input').value = '';
      clearSearchResults();
      const chat = allChats.find(c => c.id === r.chat_id && c.type === r.chat_type);
      if (chat) {
        const el2 = document.querySelector(`.chat-item[data-id="${r.chat_id}"][data-type="${r.chat_type}"]`);
        if (el2) selectChat(chat, el2);
      }
    });
    return el;
  }

  function renderSearchResults(q, contactMatches, textResults, indexedCount) {
    const container = document.getElementById('search-results');
    if (!contactMatches.length && !textResults.length) {
      clearSearchResults();
      return;
    }
    container.classList.add('has-results');
    container.innerHTML = '';

    if (contactMatches.length) {
      container.appendChild(makeSectionLabel('Contacts'));
      contactMatches.forEach(chat => container.appendChild(makeContactResultItem(chat, q)));
    }

    if (textResults.length) {
      container.appendChild(makeSectionLabel('Messages'));
      textResults.slice(0, 50).forEach(r => container.appendChild(makeTextResultItem(r, q)));
    }

    if (indexedCount != null && indexedCount < allChats.length) {
      const notice = document.createElement('div');
      notice.className = 'search-index-notice';
      notice.textContent = `Searched ${indexedCount} of ${allChats.length} chats. Open more chats to index them.`;
      container.appendChild(notice);
    }
  }

  // ---- in-chat search -------------------------------------------------------

  let chatSearchResults = [];
  let chatSearchIdx = -1;
  let chatSearchTimer = null;

  document.getElementById('chat-search-input').addEventListener('input', function () {
    document.getElementById('chat-search-clear').style.display = this.value ? 'block' : 'none';
    clearTimeout(chatSearchTimer);
    const q = this.value.trim();
    if (!q) {
      clearChatSearch();
      return;
    }
    chatSearchTimer = setTimeout(() => doChatSearch(q), 300);
  });

  document.getElementById('chat-search-clear').addEventListener('click', () => {
    const inp = document.getElementById('chat-search-input');
    inp.value = '';
    document.getElementById('chat-search-clear').style.display = 'none';
    clearChatSearch();
    inp.focus();
  });

  document.getElementById('chat-search-input').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      if (e.shiftKey) navigateChatSearch(-1);
      else navigateChatSearch(1);
    } else if (e.key === 'Escape') {
      clearChatSearch();
      this.value = '';
    }
  });

  document.getElementById('search-prev').addEventListener('click', () => navigateChatSearch(-1));
  document.getElementById('search-next').addEventListener('click', () => navigateChatSearch(1));

  async function doChatSearch(q) {
    if (!currentChat) return;
    const nav = document.getElementById('chat-search-nav');
    const count = document.getElementById('chat-search-count');
    const statusRes = await fetch('/api/chat-index-status?chat_id=' +
      encodeURIComponent(currentChat.id) + '&chat_type=' + encodeURIComponent(currentChat.type));
    const statusData = await statusRes.json();
    if (statusData.status !== 'done') {
      nav.style.display = 'flex';
      count.textContent = 'Search not ready yet…';
      document.getElementById('search-prev').disabled = true;
      document.getElementById('search-next').disabled = true;
      return;
    }
    nav.style.display = 'flex';
    count.textContent = 'Searching…';
    document.getElementById('search-prev').disabled = true;
    document.getElementById('search-next').disabled = true;
    const params = new URLSearchParams({ q, chat_id: currentChat.id, chat_type: currentChat.type });
    const res = await fetch('/api/search?' + params);
    const data = await res.json();
    chatSearchResults = data.results;
    chatSearchIdx = chatSearchResults.length > 0 ? 0 : -1;
    updateChatSearchNav();
    if (chatSearchIdx >= 0) jumpToChatSearchResult(chatSearchIdx);
  }

  function clearChatSearch() {
    chatSearchResults = [];
    chatSearchIdx = -1;
    document.getElementById('chat-search-nav').style.display = 'none';
    document.getElementById('chat-search-count').textContent = '';
    document.querySelectorAll('.search-highlight').forEach(el => el.classList.remove('search-highlight'));
  }

  function navigateChatSearch(delta) {
    if (!chatSearchResults.length) return;
    chatSearchIdx = (chatSearchIdx + delta + chatSearchResults.length) % chatSearchResults.length;
    updateChatSearchNav();
    jumpToChatSearchResult(chatSearchIdx);
  }

  function updateChatSearchNav() {
    const nav = document.getElementById('chat-search-nav');
    const count = document.getElementById('chat-search-count');
    nav.style.display = 'flex';
    if (!chatSearchResults.length) {
      count.textContent = 'No results';
      document.getElementById('search-prev').disabled = true;
      document.getElementById('search-next').disabled = true;
      return;
    }
    count.textContent = `${chatSearchIdx + 1}/${chatSearchResults.length}`;
    document.getElementById('search-prev').disabled = false;
    document.getElementById('search-next').disabled = false;
  }

  async function jumpToChatSearchResult(idx) {
    const result = chatSearchResults[idx];
    if (!result) return;
    document.querySelectorAll('.search-highlight').forEach(el => el.classList.remove('search-highlight'));
    const existing = document.querySelector(`.msg-row[data-ts="${result.timestamp_ms}"]`);
    if (existing) {
      highlightMessageRow(existing);
      return;
    }
    await jumpToTimestamp(result.timestamp_ms);
    const loaded = document.querySelector(`.msg-row[data-ts="${result.timestamp_ms}"]`);
    if (loaded) highlightMessageRow(loaded);
  }

  function highlightMessageRow(row) {
    const bubble = row.querySelector('.msg-bubble');
    if (bubble) bubble.classList.add('search-highlight');
    row.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  // ---- date picker ----------------------------------------------------------

  document.getElementById('date-picker-input').max = new Date().toISOString().slice(0, 10);

  document.getElementById('date-picker-input').addEventListener('change', function () {
    const hasVal = !!this.value;
    document.getElementById('date-go-btn').style.display = hasVal ? 'inline-block' : 'none';
    document.getElementById('date-clear-btn').style.display = hasVal ? 'inline-block' : 'none';
  });

  document.getElementById('date-go-btn').addEventListener('click', () => {
    const val = document.getElementById('date-picker-input').value;
    if (!val || !currentChat) return;
    jumpToTimestamp(new Date(val).getTime());
  });

  document.getElementById('date-clear-btn').addEventListener('click', () => {
    document.getElementById('date-picker-input').value = '';
    document.getElementById('date-go-btn').style.display = 'none';
    document.getElementById('date-clear-btn').style.display = 'none';
  });

  async function jumpToTimestamp(ts) {
    const params = new URLSearchParams({
      chat_id: currentChat.id,
      chat_type: currentChat.type,
      ts,
      limit: 50
    });
    const res = await fetch('/api/messages/at?' + params);
    const msgs = await res.json();
    if (!msgs.length) return;

    const scroll = document.getElementById('message-scroll');
    scroll.innerHTML = '';
    msgList = [];
    domNodes = 0;

    msgs.forEach((m, i) => {
      msgList.push(m);
      const prevTs = i === 0 ? null : msgs[i - 1].timestamp_ms;
      renderBubbleWithSep(m, prevTs, 'after').forEach(node => scroll.appendChild(node));
      domNodes++;
    });

    const target = msgs.reduce((best, m) =>
      Math.abs(m.timestamp_ms - ts) < Math.abs(best.timestamp_ms - ts) ? m : best
    );
    const targetEl = document.querySelector(`.msg-row[data-ts="${target.timestamp_ms}"]`);
    if (targetEl) targetEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  // ---- toolbar toggle -------------------------------------------------------

  document.getElementById('toolbar-toggle').addEventListener('click', function () {
    const exp = document.getElementById('toolbar-expanded');
    const open = exp.classList.toggle('open');
    if (open) document.getElementById('chat-search-input').focus();
    else {
      clearChatSearch();
      document.getElementById('chat-search-input').value = '';
    }
  });

  // ---- init ----------------------------------------------------------------

  document.getElementById('settings-btn').addEventListener('click', () => {
    document.getElementById('settings-modal').classList.add('open');
  });
  document.getElementById('settings-close').addEventListener('click', () => {
    document.getElementById('settings-modal').classList.remove('open');
  });
  document.getElementById('settings-modal').addEventListener('click', e => {
    if (e.target === e.currentTarget) document.getElementById('settings-modal').classList.remove('open');
  });
  document.querySelectorAll('.pref-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      prefs[btn.dataset.pref] = btn.dataset.value;
      applyPrefs();
      fetch('/api/preferences', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: btn.dataset.pref, value: btn.dataset.value })
      });
    });
  });

  loadPrefs().then(() => loadChats());
})();
</script>

</body>
</html>
"""
