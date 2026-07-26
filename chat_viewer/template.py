# HTML template for the WhatsApp chat viewer web UI.

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>WA Chat Viewer</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

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

    body[data-theme="light"] {
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

    html, body { height: 100%; background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }

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
    }
    .filter-btn.active { background: var(--accent); color: #fff; }
    .filter-btn:hover:not(.active) { background: var(--border); color: var(--text); }

    #search-box { padding: 10px; display: flex; gap: 6px; align-items: center; }
    #search-input {
      flex: 1;
      background: var(--surface2);
      border: none;
      border-radius: 8px;
      padding: 8px 12px;
      color: var(--text);
      font-size: 14px;
      outline: none;
    }
    #search-input:focus { box-shadow: 0 0 0 2px var(--accent); }
    #settings-btn {
      background: none; border: none; cursor: pointer;
      color: var(--text-muted); padding: 4px; border-radius: 6px;
      display: flex; align-items: center; justify-content: center; flex-shrink: 0;
    }
    #settings-btn:hover { color: var(--text); background: var(--surface2); }

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
    }
    #settings-close:hover { color: var(--text); background: var(--surface2); }
    .settings-section { padding: 16px 20px; border-bottom: 1px solid var(--border); }
    .settings-section:last-child { border-bottom: none; }
    .settings-label { font-size: 13px; color: var(--text-muted); margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.05em; }
    .settings-row { display: flex; gap: 8px; flex-wrap: wrap; }
    .pref-btn {
      background: var(--surface2); border: 1px solid var(--border);
      color: var(--text); border-radius: 8px; padding: 7px 14px;
      font-size: 13px; cursor: pointer;
    }
    .pref-btn:hover { border-color: var(--accent); }
    .pref-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }

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

    #chat-pane {
      flex: 1;
      display: flex;
      flex-direction: column;
      overflow: hidden;
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
      background: none;
      border: none;
      color: var(--text-muted);
      cursor: pointer;
      font-size: 18px;
      padding: 4px 6px;
      border-radius: 4px;
      line-height: 1;
    }
    #toolbar-toggle:hover { background: var(--surface2); color: var(--text); }

    #toolbar-expanded {
      display: none;
      align-items: center;
      gap: 8px;
    }
    #toolbar-expanded.open { display: flex; }

    #chat-search-input {
      background: var(--surface2);
      border: none;
      border-radius: 6px;
      padding: 5px 10px;
      color: var(--text);
      font-size: 13px;
      outline: none;
      width: 180px;
    }
    #chat-search-input:focus { box-shadow: 0 0 0 2px var(--accent); }

    #chat-search-nav { display: flex; gap: 2px; align-items: center; }
    #chat-search-count { font-size: 12px; color: var(--text-muted); min-width: 50px; text-align: center; }
    .nav-btn {
      background: var(--surface2);
      border: none;
      border-radius: 4px;
      color: var(--text);
      cursor: pointer;
      padding: 4px 8px;
      font-size: 14px;
      line-height: 1;
    }
    .nav-btn:hover { background: var(--accent); color: #fff; }
    .nav-btn:disabled { opacity: 0.3; cursor: default; }

    #date-picker-input {
      background: var(--surface2);
      border: none;
      border-radius: 6px;
      padding: 5px 8px;
      color: var(--text);
      font-size: 13px;
      outline: none;
      color-scheme: dark;
    }
    #date-picker-input:focus { box-shadow: 0 0 0 2px var(--accent); }

    #message-scroll {
      flex: 1;
      overflow-y: auto;
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }

    .msg-row {
      display: flex;
      flex-direction: column;
      margin-bottom: 2px;
    }
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

    #empty-pane {
      flex: 1;
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--text-muted);
      font-size: 15px;
    }

    #chat-loading {
      display: none;
      flex: 1;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 12px;
      color: var(--text-muted);
      font-size: 14px;
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

    .spinner {
      display: flex; justify-content: center; padding: 16px;
      color: var(--text-muted); font-size: 13px;
    }

    .img-lightbox {
      position: fixed; inset: 0; background: rgba(0,0,0,0.9);
      display: flex; align-items: center; justify-content: center;
      z-index: 1000; cursor: zoom-out;
    }
    .img-lightbox img { max-width: 90vw; max-height: 90vh; object-fit: contain; }
    .img-lightbox video { max-width: 90vw; max-height: 90vh; }

    #media-btn {
      background: none;
      border: none;
      color: var(--text-muted);
      cursor: pointer;
      font-size: 16px;
      padding: 4px 6px;
      border-radius: 4px;
      line-height: 1;
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
      background: none; border: none; color: var(--text-secondary);
      font-size: 18px; cursor: pointer; padding: 4px 6px; border-radius: 4px;
    }
    #media-gallery-close:hover { background: var(--surface2); }
    #media-gallery-stats {
      padding: 8px 16px;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      font-size: 12px;
      color: var(--text-secondary);
      display: flex;
      flex-wrap: wrap;
      gap: 6px 16px;
      flex-shrink: 0;
    }
    .gallery-stat { white-space: nowrap; }
    .gallery-stat-missing { color: var(--text-muted); font-style: italic; }
    #media-gallery-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
      gap: 4px; overflow-y: auto; padding: 8px;
    }
    .gallery-item {
      position: relative; aspect-ratio: 1/1; cursor: pointer;
      background: var(--surface); overflow: hidden; border-radius: 4px;
    }
    .gallery-item img, .gallery-item video {
      width: 100%; height: 100%; object-fit: cover; display: block;
    }
    .gallery-item .gallery-doc {
      display: flex; flex-direction: column; align-items: center;
      justify-content: center; height: 100%; font-size: 12px;
      color: var(--text-secondary); padding: 4px; text-align: center;
      word-break: break-all;
    }
    .gallery-goto {
      position: absolute; bottom: 4px; right: 4px;
      background: rgba(0,0,0,0.6); color: #fff; border: none;
      border-radius: 4px; font-size: 11px; padding: 2px 5px; cursor: pointer;
      opacity: 0; transition: opacity 0.15s;
    }
    .gallery-item:hover .gallery-goto { opacity: 1; }

    .search-result-item {
      padding: 10px 16px;
      cursor: pointer;
      border-bottom: 1px solid var(--border);
      font-size: 13px;
    }
    .search-result-item:hover { background: var(--surface2); }
    .search-result-item .sr-chat { font-weight: 600; font-size: 12px; margin-bottom: 2px; }
    .search-result-item .sr-text { color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .search-result-item .sr-time { font-size: 11px; color: var(--text-muted); margin-top: 2px; }
    mark { background: #ffe082; color: #111; border-radius: 2px; padding: 0 1px; }

    .sr-section-label {
      padding: 6px 16px 4px;
      font-size: 10px;
      font-weight: 700;
      color: var(--accent);
      text-transform: uppercase;
      letter-spacing: 0.6px;
      border-top: 1px solid var(--border);
    }
    .sr-section-label:first-child { border-top: none; }

    .search-index-notice {
      padding: 8px 16px;
      font-size: 11px;
      color: var(--text-muted);
      border-top: 1px solid var(--border);
      font-style: italic;
    }

    .direction-badge {
      display: inline-block;
      font-size: 10px;
      padding: 1px 4px;
      border-radius: 3px;
      margin-right: 4px;
      vertical-align: middle;
    }
    .direction-badge.sent { background: var(--accent); color: #fff; }

    .date-separator {
      display: flex;
      align-items: center;
      justify-content: center;
      margin: 8px 0;
    }
    .date-separator span {
      background: var(--surface2);
      color: var(--text-muted);
      font-size: 11px;
      padding: 3px 10px;
      border-radius: 8px;
    }

    .load-spinner {
      display: flex;
      justify-content: center;
      padding: 10px;
      color: var(--text-muted);
      font-size: 20px;
    }

    .msg-quote {
      background: rgba(0,0,0,0.08);
      border-left: 3px solid var(--accent);
      border-radius: 4px;
      padding: 4px 8px;
      margin-bottom: 4px;
      max-width: 100%;
      overflow: hidden;
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
            <input id="chat-search-input" type="search" placeholder="Find in chat…" autocomplete="off">
            <div id="chat-search-nav" style="display:none;">
              <button class="nav-btn" id="search-prev" title="Previous">&#8679;</button>
              <span id="chat-search-count"></span>
              <button class="nav-btn" id="search-next" title="Next">&#8681;</button>
            </div>
            <input id="date-picker-input" type="date" title="Jump to date">
          </div>
        </div>
      </div>
      <div id="message-scroll"></div>
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
    <button id="media-gallery-close" title="Close">&#10005;</button>
  </div>
  <div id="media-gallery-stats"></div>
  <div id="media-gallery-grid"></div>
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
    document.body.dataset.theme = prefs.theme;
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

  function renderChatList() {
    const list = document.getElementById('chat-list');
    list.innerHTML = '';
    const visible = currentFilter === 'all' ? allChats : allChats.filter(c => c.type === currentFilter);
    visible.forEach(chat => {
      const el = document.createElement('div');
      el.className = 'chat-item' + (chat.type === 'group' ? ' group' : '');
      el.dataset.id = chat.id;
      el.dataset.type = chat.type;
      el.innerHTML = `<div class="chat-name">${esc(chat.display_name)}</div><div class="chat-meta">${chat.msg_count} messages · ${fmtTime(chat.newest_ts)}</div>`;
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

    document.getElementById('chat-header').style.display = '';
    document.getElementById('chat-title').textContent = chat.display_name;
    document.getElementById('empty-pane').style.display = 'none';
    document.getElementById('search-results').classList.remove('has-results');
    document.getElementById('search-results').innerHTML = '';

    // reset in-chat search when switching chats
    document.getElementById('chat-search-input').value = '';
    clearChatSearch();

    const loadingEl = document.getElementById('chat-loading');
    const countEl = document.getElementById('chat-loading-count');
    loadingEl.style.display = 'flex';
    countEl.textContent = '';
    let pollTimer = setInterval(async () => {
      try {
        const r = await fetch('/api/index-status');
        const d = await r.json();
        if (d.indexed > 0) countEl.textContent = d.indexed.toLocaleString() + ' messages indexed';
      } catch (_) {}
    }, 500);

    await loadMessages('older');

    clearInterval(pollTimer);
    loadingEl.style.display = 'none';

    scroll.scrollTop = scroll.scrollHeight;
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
    scroll.addEventListener('scroll', function () {
      if (loading) return;
      if (scroll.scrollTop < 100) {
        loadMessages('older');
      } else if (scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 100) {
        loadMessages('newer');
      }
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

  function showLightbox(src, mt) {
    const lb = document.createElement('div');
    lb.className = 'img-lightbox';
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
      img.addEventListener('click', () => showLightbox(src, mt));
      cell.appendChild(img);
    } else if (mt === 'video') {
      const vid = document.createElement('video');
      vid.src = src;
      vid.preload = 'none';
      vid.addEventListener('click', () => showLightbox(src, mt));
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
    grid.innerHTML = '<div style="color:var(--text-secondary);padding:16px">Loading…</div>';
    stats.innerHTML = '';
    document.getElementById('media-gallery').classList.add('open');

    const r = await fetch(
      '/api/media?chat_id=' + encodeURIComponent(currentChat.id) +
      '&chat_type=' + encodeURIComponent(currentChat.type)
    );
    const items = await r.json();
    grid.innerHTML = '';

    if (!items.length) {
      grid.innerHTML = '<div style="color:var(--text-secondary);padding:16px">No media in this chat.</div>';
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

    items.filter(m => m.archive_path).forEach(msg => grid.appendChild(renderGalleryItem(msg)));
  }

  document.getElementById('media-btn').addEventListener('click', openMediaGallery);
  document.getElementById('media-gallery-close').addEventListener('click', closeMediaGallery);
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeMediaGallery();
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
    clearTimeout(chatSearchTimer);
    const q = this.value.trim();
    if (!q) {
      clearChatSearch();
      return;
    }
    chatSearchTimer = setTimeout(() => doChatSearch(q), 300);
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

  document.getElementById('date-picker-input').addEventListener('change', function () {
    const val = this.value;
    if (!val || !currentChat) return;
    const ts = new Date(val).getTime();
    jumpToTimestamp(ts);
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
