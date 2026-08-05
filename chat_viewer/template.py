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
  <link rel="stylesheet" href="/static/app.css">
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
          <button id="chat-info-btn" title="Chat info">&#8505;</button>
          <button id="media-btn" title="Media">&#128247;</button>
          <button id="toolbar-toggle" title="Search &amp; date">&#128269;</button>
        </div>
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
          <div id="date-group">
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

<div id="chat-info-panel">
  <div id="chat-info-inner">
    <div id="chat-info-header">
      <span id="chat-info-title"></span>
      <button id="chat-info-close">&#10005;</button>
    </div>
    <div id="chat-info-body"></div>
  </div>
</div>

<div id="msg-details-popup"></div>

<script>window.OUTPUT_ROOT = {{ output_root | tojson }};</script>
<script src="/static/app.js"></script>

</body>
</html>
"""
