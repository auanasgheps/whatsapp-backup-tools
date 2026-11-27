# HTML template for the WhatsApp chat viewer web UI.

from pathlib import Path

_PICO_CSS = (Path(__file__).parent / "pico.min.css").read_text(encoding="utf-8")
# Strip @charset — invalid inside @layer
_PICO_CSS = _PICO_CSS.replace('@charset "UTF-8";', "", 1)

HTML_TEMPLATE = (
    r"""
<!DOCTYPE html>
<html lang="{{ lang | default('en') }}" data-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta id="meta-color-scheme" name="color-scheme" content="dark">
  <title>{{ t('app.page_title') if t is defined else 'WhatsApp Backup Tools' }}</title>
  <link rel="icon" type="image/svg+xml" href="{{ base_path }}/static/favicon.svg">
  <style>"""
    + _PICO_CSS
    + r"""</style>
  <link rel="stylesheet" href="{{ base_path }}/static/app.css?v={{ version }}">
</head>
<body>
<div id="app">
  {% if schema_alert %}
  <div id="schema-alert-banner" class="schema-alert-banner">
    <div class="schema-alert-content">
      <span class="schema-alert-icon">⚠️</span>
      <div class="schema-alert-text">
        {% if schema_alert.is_legacy %}
        <strong>{{ t('schema_alert.legacy_title') if t is defined else 'Legacy WhatsApp Database Detected:' }}</strong>
        {{ schema_alert.summary }}. {{ t('schema_alert.legacy_body') if t is defined else 'Only modern WhatsApp database backups (Android 2019+ and iOS CoreData) are supported.' }}
        {% else %}
        <strong>{{ t('schema_alert.incompat_title') if t is defined else 'WhatsApp Database Schema Incompatibility Detected:' }}</strong>
        {{ schema_alert.summary }}. {{ t('schema_alert.incompat_body') if t is defined else 'Chats could not be read. Please check for updates:' }} <code>pip install --upgrade whatsapp-backup-tools</code>
        {% endif %}
      </div>
      {% if schema_alert.issue_url %}
      <a href="{{ schema_alert.issue_url }}" target="_blank" rel="noopener noreferrer" class="schema-alert-btn">
        {% if schema_alert.is_legacy %}{{ t('schema_alert.request_support') if t is defined else 'Request Support on GitHub' }}{% else %}{{ t('schema_alert.report_issue') if t is defined else 'Report Issue on GitHub' }}{% endif %}
      </a>
      {% endif %}
    </div>
  </div>
  {% endif %}
  <div id="header">
    <div id="header-brand">
      <h1 data-i18n="app.title">WhatsApp Backup Viewer</h1>
      <span class="subtitle">{{ output_root }}{% if platform_name %} • <span class="platform-badge">{{ platform_name }}</span>{% endif %}</span>
    </div>
    <button id="settings-btn" title="Settings" aria-label="Settings" data-i18n-title="nav.settings" data-i18n-aria="nav.settings">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
      <span class="settings-btn-label" data-i18n="nav.settings">Settings</span>
    </button>
  </div>

  <div id="body">
    <div id="sidebar">
      <div id="search-box">
        <input id="search-input" type="search" placeholder="Search messages &amp; chats…" data-i18n-placeholder="sidebar.search_chats_placeholder" autocomplete="off">
        <button id="search-clear" title="Clear search" aria-label="Clear search" data-i18n-title="sidebar.clear_search" data-i18n-aria="sidebar.clear_search" style="display:none;">&#10005;</button>
      </div>
      <div id="sidebar-filters">
        <button class="filter-btn active" data-filter="all" data-i18n="sidebar.filter_all">All</button>
        <button class="filter-btn" data-filter="contact" data-i18n="sidebar.filter_chats">Chats</button>
        <button class="filter-btn" data-filter="group" data-i18n="sidebar.filter_groups">Groups</button>
        <button class="filter-btn" data-filter="unread" data-i18n="sidebar.filter_unread">Unread</button>
      </div>
      <div id="search-results"></div>
      <div id="chat-list"></div>
      <nav id="sidebar-bottom-bar" role="tablist" aria-label="Main Navigation" data-i18n-aria="nav.main_aria">
        <button id="tab-btn-chats" class="tab-btn active" role="tab" aria-selected="true" tabindex="0" data-tab="chats" title="Chats" data-i18n-title="nav.chats">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
          <span data-i18n="nav.chats">Chats</span>
        </button>
        <button id="tab-btn-calls" class="tab-btn" role="tab" aria-selected="false" tabindex="-1" data-tab="calls" title="Calls" data-i18n-title="nav.calls">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"/></svg>
          <span data-i18n="nav.calls">Calls</span>
        </button>
        <button id="tab-btn-starred" class="tab-btn" role="tab" aria-selected="false" tabindex="-1" data-tab="starred" title="Starred" data-i18n-title="nav.starred">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
          <span data-i18n="nav.starred">Starred</span>
        </button>
        <button id="tab-btn-archived" class="tab-btn" role="tab" aria-selected="false" tabindex="-1" data-tab="archived" title="Archived" data-i18n-title="nav.archived">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="21 8 21 21 3 21 3 8"/><rect x="1" y="3" width="22" height="5"/><line x1="10" y1="12" x2="14" y2="12"/></svg>
          <span data-i18n="nav.archived">Archived</span>
        </button>
      </nav>
    </div>

    <div id="chat-pane">
      <div id="chat-header" style="display:none;">
        <button id="chat-back-btn" class="pane-back-btn" type="button" title="Back" aria-label="Back" data-i18n-title="common.back" data-i18n-aria="common.back"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="15 18 9 12 15 6"/></svg></button>
        <div id="chat-header-info" tabindex="0" title="Click to view chat info" aria-label="Chat details" data-i18n-title="chat.header_info_title" data-i18n-aria="chat.header_info_aria">
          <div id="chat-header-avatar" class="chat-header-avatar" aria-hidden="true"></div>
          <div class="chat-header-meta">
            <div class="chat-header-title-wrap">
              <h2 id="chat-title"></h2>
              <span id="chat-header-business-badge" class="chat-business-badge" style="display:none;" data-i18n-title="chat.business_badge_title">
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 7V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2"/></svg>
                <span data-i18n="chat.business_badge">Business</span>
              </span>
              <span class="chat-header-arrow" aria-hidden="true">›</span>
            </div>
            <div id="chat-subtitle" class="chat-header-subtitle"></div>
          </div>
        </div>
        <div id="chat-toolbar">
          <button id="toolbar-toggle" class="chat-toolbar-btn" title="Search &amp; date" aria-label="Search in chat" data-i18n-title="chat.search_and_date" data-i18n-aria="chat.search_in_chat_aria">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <circle cx="11" cy="11" r="8"/>
              <line x1="21" y1="21" x2="16.65" y2="16.65"/>
            </svg>
          </button>
          <button id="media-btn" class="chat-toolbar-btn" title="Media" aria-label="Media gallery" data-i18n-title="chat.media" data-i18n-aria="chat.media_gallery_aria">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/>
              <circle cx="12" cy="13" r="4"/>
            </svg>
          </button>
          <div id="chat-menu-container" class="chat-menu-container">
            <button id="chat-menu-btn" class="chat-toolbar-btn" title="Menu" aria-label="Chat options" data-i18n-title="chat.menu" data-i18n-aria="chat.chat_options_aria" aria-haspopup="true" aria-expanded="false">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                <circle cx="12" cy="5" r="1.75"/>
                <circle cx="12" cy="12" r="1.75"/>
                <circle cx="12" cy="19" r="1.75"/>
              </svg>
            </button>
            <div id="chat-menu-dropdown" class="chat-menu-dropdown" role="menu" aria-label="Chat options" data-i18n-aria="chat.chat_options_aria" style="display:none;">
              <button id="chat-starred-btn" class="chat-menu-item" role="menuitem" tabindex="-1">
                <svg class="chat-menu-item-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>
                </svg>
                <span class="chat-menu-item-text" data-i18n="chat.starred_messages">Starred messages</span>
                <span id="chat-starred-badge" class="chat-menu-badge" style="display:none;"></span>
              </button>
              <button id="chat-info-btn" class="chat-menu-item" role="menuitem" tabindex="-1">
                <svg class="chat-menu-item-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <circle cx="12" cy="12" r="10"/>
                  <line x1="12" y1="16" x2="12" y2="12"/>
                  <line x1="12" y1="8" x2="12.01" y2="8"/>
                </svg>
                <span id="chat-info-menu-label" class="chat-menu-item-text" data-i18n="chat.chat_info">Chat info</span>
              </button>
            </div>
          </div>
        </div>
        <div id="toolbar-expanded">
          <div id="chat-search-wrap">
            <input id="chat-search-input" type="search" placeholder="Find in chat…" data-i18n-placeholder="chat.find_in_chat_placeholder" autocomplete="off">
            <button id="chat-search-clear" title="Clear search" data-i18n-title="sidebar.clear_search">&#10005;</button>
          </div>
          <div id="chat-search-nav" style="display:none;">
            <button class="nav-btn" id="search-prev" title="Previous" data-i18n-title="common.previous">&#8679;</button>
            <span id="chat-search-count"></span>
            <button class="nav-btn" id="search-next" title="Next" data-i18n-title="common.next">&#8681;</button>
          </div>
          <div id="date-group">
            <input id="date-picker-input" type="date" title="Jump to date" data-i18n-title="chat.jump_to_date">
            <button id="date-go-btn" data-i18n="common.go">Go</button>
            <button id="date-clear-btn" data-i18n="common.clear">Clear</button>
          </div>
        </div>
      </div>
      <div id="pinned-banner" class="pinned-banner-strip" role="region" aria-label="Pinned message" data-i18n-aria="pinned.banner_aria" style="display:none;">
        <div class="pinned-banner-content" tabindex="0" title="Jump to pinned message" data-i18n-title="pinned.jump_title">
          <span class="pinned-banner-icon" aria-hidden="true">📌</span>
          <div class="pinned-banner-text-wrap">
            <span id="pinned-banner-author" class="pinned-banner-author" data-i18n="pinned.label">Pinned</span>
            <span id="pinned-banner-snippet" class="pinned-banner-snippet"></span>
          </div>
        </div>
        <div class="pinned-banner-actions">
          <div id="pinned-banner-nav" class="pinned-banner-nav" style="display:none;">
            <button id="pinned-banner-prev" class="pinned-nav-btn" title="Previous pinned message" aria-label="Previous pinned message" data-i18n-title="pinned.prev_title" data-i18n-aria="pinned.prev_title">&#8249;</button>
            <span id="pinned-banner-count" class="pinned-banner-count">1 of 1</span>
            <button id="pinned-banner-next" class="pinned-nav-btn" title="Next pinned message" aria-label="Next pinned message" data-i18n-title="pinned.next_title" data-i18n-aria="pinned.next_title">&#8250;</button>
          </div>
          <button id="pinned-banner-dismiss" class="pinned-banner-dismiss" title="Dismiss pinned message" aria-label="Dismiss pinned message" data-i18n-title="pinned.dismiss_title" data-i18n-aria="pinned.dismiss_title">&#10005;</button>
        </div>
      </div>
      <div id="context-jump-banner" class="context-jump-banner" role="status" style="display:none;">
        <div class="context-jump-content">
          <span class="context-jump-icon" aria-hidden="true">⭐</span>
          <span id="context-jump-text" class="context-jump-text" data-i18n="chat.context_banner">Viewing message in context</span>
        </div>
        <button id="context-jump-switch-btn" class="context-jump-action-btn" data-i18n="chat.switch_to_chat_view">Switch to Chat View &#8599;</button>
      </div>
      <div id="message-scroll"></div>
      <button id="scroll-to-bottom" title="Jump to latest" data-i18n-title="chat.jump_to_latest">&#8627;</button>
      <div id="chat-jump-hud" class="chat-jump-hud" role="status" aria-live="polite">
        <span class="jump-hud-spinner"></span>
        <span id="chat-jump-hud-text" class="jump-hud-text" data-i18n="chat.jumping">Jumping to message…</span>
      </div>
      <div id="empty-pane" data-i18n="sidebar.select_chat">Select a chat to browse messages</div>
      <div id="chat-loading">
        <div class="loading-spinner"></div>
        <span data-i18n="chat.loading">Loading chat…</span>
        <span id="chat-loading-count"></span>
      </div>

      <!-- Call Inspector Detail Card -->
      <div id="pane-call-inspector" class="call-inspector-pane" style="display:none;" role="region" aria-label="Call details" data-i18n-aria="details.call_details">
        <button id="call-back-btn" class="pane-back-btn" type="button" title="Back to calls" aria-label="Back to calls" data-i18n-title="calls.back_to_calls" data-i18n-aria="calls.back_to_calls"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="15 18 9 12 15 6"/></svg></button>
        <div class="call-inspector-card">
          <div class="call-inspect-header">
            <div id="call-inspect-avatar" class="call-inspect-avatar">AW</div>
            <div class="call-inspect-caller-info">
              <h3 id="call-inspect-name" class="call-inspect-name">Alice Whitman</h3>
              <p id="call-inspect-phone" class="call-inspect-phone">+1 (555) 234-5678</p>
            </div>
            <div id="call-inspect-badge" class="call-inspect-type-badge">
              <span id="call-inspect-badge-icon">📹</span>
              <span id="call-inspect-badge-label" data-i18n="calls.video_call">Video Call</span>
            </div>
          </div>
          <div class="call-inspect-details">
            <div class="call-inspect-row">
              <span class="call-inspect-label" data-i18n="calls.direction">Call Direction</span>
              <span id="call-inspect-dir" class="call-inspect-val">Incoming</span>
            </div>
            <div class="call-inspect-row">
              <span class="call-inspect-label" data-i18n="calls.timestamp">Timestamp</span>
              <span id="call-inspect-time" class="call-inspect-val">Today at 14:20:18</span>
            </div>
            <div class="call-inspect-row">
              <span class="call-inspect-label" data-i18n="calls.duration">Duration</span>
              <span id="call-inspect-duration" class="call-inspect-val font-mono">12 minutes, 45 seconds</span>
            </div>
            <div id="call-inspect-data-row" class="call-inspect-row" style="display:none;">
              <span class="call-inspect-label" data-i18n="calls.data_transferred">Data Transferred</span>
              <span id="call-inspect-data" class="call-inspect-val font-mono">84.2 MB</span>
            </div>
          </div>
          <div class="call-inspect-actions">
            <button id="call-inspect-jump-btn" class="call-inspect-jump-btn">
              <svg class="btn-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
              <span data-i18n="calls.jump_to_moment">Jump to this Moment in Chat &#8599;</span>
            </button>
          </div>
        </div>
      </div>

      <!-- In-Chat Starred Messages Flyout Drawer -->
      <aside id="chat-starred-drawer" class="chat-drawer" role="region" aria-label="Starred messages" data-i18n-aria="chat.starred_messages" style="display:none;">
        <div class="chat-drawer-header">
          <div class="chat-drawer-header-left">
            <svg class="chat-drawer-star-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
            <span class="chat-drawer-title" data-i18n="starred.title">Starred Messages</span>
            <span id="chat-starred-header-count" class="chat-drawer-count-badge">0</span>
          </div>
          <button id="chat-starred-close" class="chat-drawer-close-btn" title="Close flyout" aria-label="Close flyout" data-i18n-title="starred.close_aria" data-i18n-aria="starred.close_aria">&#10005;</button>
        </div>
        <div id="chat-starred-body" class="chat-drawer-body"></div>
      </aside>

      <!-- Contact / Chat Info Drawer -->
      <aside id="chat-info-panel" class="chat-drawer" role="region" aria-label="Chat info" data-i18n-aria="chat.chat_info" style="display:none;">
        <div class="chat-drawer-header">
          <div class="chat-drawer-header-left">
            <span id="chat-info-header-heading" class="chat-drawer-title" data-i18n="chat_info.title_contact">Contact Info</span>
          </div>
          <button id="chat-info-close" class="chat-drawer-close-btn" title="Close chat info" aria-label="Close chat info" data-i18n-title="chat_info.close_aria" data-i18n-aria="chat_info.close_aria">&#10005;</button>
        </div>
        <div id="chat-info-body" class="chat-drawer-body">
          <div id="chat-info-hero" class="chat-info-hero">
            <div id="chat-info-avatar" class="chat-info-avatar"></div>
            <h3 id="chat-info-title" class="chat-info-name"></h3>
            <p id="chat-info-phone" class="chat-info-phone"></p>
            <div id="chat-info-desc" class="chat-info-desc" style="display:none"></div>
          </div>
          <div id="chat-info-details" class="chat-info-details"></div>
        </div>
      </aside>
    </div>
  </div>
</div>

<div id="media-gallery">
  <div id="media-gallery-header">
    <span id="media-gallery-title" data-i18n="gallery.title">Media</span>
    <div id="media-view-switcher" style="display:none">
      <button class="media-view-btn active" data-view="grid" title="Grid view" data-i18n-title="gallery.grid_view">&#8862;</button>
      <button class="media-view-btn" data-view="archive" title="Archive view" data-i18n-title="gallery.archive_view">&#128193;</button>
    </div>
    <button id="media-gallery-close" title="Close" data-i18n-title="common.close">&#10005;</button>
  </div>
  <div id="media-gallery-stats"></div>
  <div id="media-gallery-grid"></div>
  <div id="media-archive-view">
    <div id="media-archive-toolbar">
      <div id="media-archive-tabs">
        <button class="archive-tab active" data-dir="Received" data-i18n="gallery.received">Received</button>
        <button class="archive-tab" data-dir="Sent" data-i18n="gallery.sent">Sent</button>
      </div>
      <div id="media-archive-actions">
        <button id="archive-expand-all" data-i18n="gallery.expand_all">Expand all</button>
        <button id="archive-collapse-all" data-i18n="gallery.collapse_all">Collapse all</button>
      </div>
    </div>
    <div id="media-archive-tree"></div>
  </div>
</div>

<div id="settings-modal">
  <div id="settings-panel">
    <div id="settings-header">
      <span data-i18n="settings.title">Settings</span>
      <button id="settings-close">&#10005;</button>
    </div>
    <div id="settings-body">
      <div id="settings-app-info">
        <img id="settings-app-logo" src="{{ base_path }}/static/favicon.svg" alt="" width="52" height="52">
        <div id="settings-app-details">
          <h2 id="settings-app-name" data-i18n="app.title">WhatsApp Backup Viewer</h2>
          <p id="settings-app-subtitle" data-i18n="app.subtitle_tools">from WhatsApp Backup Tools</p>
          <div id="settings-app-links">
            <a id="settings-repo-link" href="https://github.com/auanasgheps/whatsapp-backup-tools" target="_blank" rel="noopener noreferrer" data-i18n="app.repository">Repository</a>
            <span id="settings-version">v{{ version }}</span>
          </div>
        </div>
      </div>
      <div class="settings-section">
        <div class="settings-label" data-i18n="settings.language">Language</div>
        <div class="settings-hint" data-i18n="settings.language_hint">Auto detects your browser language</div>
        <div class="settings-row">
          <select id="pref-language" class="pref-select" data-pref="language" aria-label="Language" data-i18n-aria="settings.language">
            <option value="auto" data-i18n="settings.language_auto">Auto (Browser)</option>
            <option value="en">English</option>
            <option value="de">Deutsch</option>
            <option value="fr">Français</option>
            <option value="it">Italiano</option>
            <option value="es">Español</option>
          </select>
        </div>
      </div>
      <div class="settings-section">
        <div class="settings-label" data-i18n="settings.theme">Theme</div>
        <div class="settings-row">
          <button class="pref-btn" data-pref="theme" data-value="dark" data-i18n="settings.theme_dark">Dark</button>
          <button class="pref-btn" data-pref="theme" data-value="light" data-i18n="settings.theme_light">Light</button>
        </div>
      </div>
      <div class="settings-section">
        <div class="settings-label" data-i18n="settings.date_format">Date format</div>
        <div class="settings-row">
          <button class="pref-btn" data-pref="date_format" data-value="DD/MM/YYYY">DD/MM/YYYY &nbsp;<em>25/07/2026</em></button>
          <button class="pref-btn" data-pref="date_format" data-value="MM/DD/YYYY">MM/DD/YYYY &nbsp;<em>07/25/2026</em></button>
          <button class="pref-btn" data-pref="date_format" data-value="YYYY/MM/DD">YYYY/MM/DD &nbsp;<em>2026/07/25</em></button>
        </div>
      </div>
      <div class="settings-section">
        <div class="settings-label" data-i18n="settings.display_scale">Display scale</div>
        <div class="settings-hint" data-i18n="settings.display_scale_hint">Scales text, sidebar, and media</div>
        <div class="settings-row">
          <button class="pref-btn" data-pref="font_size" data-value="small" data-i18n="settings.scale_small">Small</button>
          <button class="pref-btn" data-pref="font_size" data-value="medium" data-i18n="settings.scale_medium">Medium</button>
          <button class="pref-btn" data-pref="font_size" data-value="large" data-i18n="settings.scale_large">Large</button>
        </div>
      </div>
      <div class="settings-section">
        <div class="settings-label" data-i18n="settings.database">Database</div>
        <div class="settings-row" style="flex-direction:column;gap:8px;align-items:flex-start">
          <button id="optimize-db-btn" class="pref-btn" data-i18n="settings.compact_db">Compact &amp; optimize database</button>
          <div id="optimize-db-status" class="index-progress-label" style="display:none"></div>
          <button id="index-all-btn" class="pref-btn" data-i18n="settings.index_all">Index all chats for search</button>
          <div id="index-all-progress" style="display:none;width:100%">
            <div class="index-progress-bar-wrap">
              <div id="index-progress-bar" class="index-progress-bar"></div>
            </div>
            <div id="index-progress-label" class="index-progress-label"></div>
          </div>
          <button id="clear-index-btn" class="pref-btn" data-i18n="settings.clear_index">Clear search index</button>
        </div>
      </div>
    </div>
  </div>
</div>



<div id="msg-details-popup"></div>

<div id="map-chooser-modal" role="dialog" aria-modal="true" aria-labelledby="map-chooser-title" aria-describedby="map-chooser-subtitle">
  <div id="map-chooser-panel">
    <div id="map-chooser-header">
      <div class="map-chooser-header-text">
        <h3 id="map-chooser-title" data-i18n="maps.title">Open in Maps</h3>
        <div id="map-chooser-subtitle" class="map-chooser-subtitle"></div>
      </div>
      <button id="map-chooser-close" aria-label="Close dialog" data-i18n-aria="maps.close_aria">&#10005;</button>
    </div>
    <div id="map-chooser-body">
      <div class="map-provider-list">
        <a id="map-provider-google" class="map-provider-btn" target="_blank" rel="noopener noreferrer">
          <span class="map-provider-icon">🌐</span>
          <div class="map-provider-info">
            <span class="map-provider-name" data-i18n="maps.google_name">Google Maps</span>
            <span class="map-provider-desc" data-i18n="maps.google_desc">Open exact coordinates on Google Maps</span>
          </div>
          <span class="map-provider-arrow" aria-hidden="true">↗</span>
        </a>
        <a id="map-provider-bing" class="map-provider-btn" target="_blank" rel="noopener noreferrer">
          <span class="map-provider-icon">🧭</span>
          <div class="map-provider-info">
            <span class="map-provider-name" data-i18n="maps.bing_name">Bing Maps</span>
            <span class="map-provider-desc" data-i18n="maps.bing_desc">Open exact coordinates on Microsoft Bing Maps</span>
          </div>
          <span class="map-provider-arrow" aria-hidden="true">↗</span>
        </a>
        <a id="map-provider-osm" class="map-provider-btn" target="_blank" rel="noopener noreferrer">
          <span class="map-provider-icon">🗺️</span>
          <div class="map-provider-info">
            <span class="map-provider-name" data-i18n="maps.osm_name">OpenStreetMap</span>
            <span class="map-provider-desc" data-i18n="maps.osm_desc">Open exact coordinates on open community map</span>
          </div>
          <span class="map-provider-arrow" aria-hidden="true">↗</span>
        </a>
      </div>
      <div class="map-chooser-footer">
        <button id="map-copy-coords-btn" class="map-copy-btn" type="button">
          <span class="map-copy-icon">📋</span>
          <span id="map-copy-coords-text" data-i18n="maps.copy_coords">Copy Coordinates</span>
        </button>
      </div>
    </div>
  </div>
</div>

<script>window.OUTPUT_ROOT = {{ output_root | tojson }};
window.BASE_PATH = {{ base_path | tojson }};
window.INITIAL_LOCALE = {{ (lang | default('en')) | tojson }};
window.LOCALE_DATA = {{ (locale_data | default({})) | tojson }};
window.EN_FALLBACK_DATA = {{ (en_fallback_data | default({})) | tojson }};</script>
<script src="{{ base_path }}/static/app.js?v={{ version }}"></script>

</body>
</html>
"""
)
