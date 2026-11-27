// ---- load chats ----------------------------------------------------------

  async function loadChats() {
    if (currentNavTab === 'chats') {
      document.getElementById('chat-list').innerHTML = '<div id="chat-list-notice"><div class="loading-spinner"></div>' + esc(t('sidebar.loading_chats')) + '</div>';
    }
    const res = await fetch(API + '/chats');
    allChats = await res.json();
    const hasArchived = allChats.some(c => c.is_archived);
    setTabEnabled('tab-btn-archived', hasArchived, t('nav.archived'), t('nav.archived_empty_title'));
    await checkCallsAvailability();
    if (currentNavTab === 'chats') {
      renderChatList();
    } else if (currentNavTab === 'archived') {
      const q = document.getElementById('search-input')?.value || '';
      renderArchivedList(q);
    }
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

  function resetFilterTo(filter) {
    if (currentFilter === filter) return;
    currentFilter = filter;
    document.querySelectorAll('.filter-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.filter === filter);
    });
    if (currentNavTab === 'chats') {
      renderChatList();
    }
  }

  let _indexPollGen = 0;

  function _startIndexPoll(chat) {
    const gen = ++_indexPollGen;
    const params = new URLSearchParams({chat_id: chat.id, chat_type: chat.type});
    async function poll() {
      if (gen !== _indexPollGen) return;
      try {
        const r = await fetch(API + '/chat-index-status?' + params);
        const d = await r.json();
        if (gen !== _indexPollGen) return;
        if (d.status === 'done') {
          _findChatListItem(chat.id, chat.type)?.querySelector('.index-spinner')?.remove();
          return;
        }
        const liveEl = _findChatListItem(chat.id, chat.type);
        if (liveEl && !liveEl.querySelector('.index-spinner')) {
          const sp = document.createElement('span');
          sp.className = 'index-spinner';
          liveEl.querySelector('.chat-name').appendChild(sp);
        }
        setTimeout(poll, 500);
      } catch (_) {
        setTimeout(poll, 1000);
      }
    }
    poll();
  }

  function getMediaLabel(type) {
    const map = {
      image: '📷 ' + t('media.photo'),
      video: '🎥 ' + t('media.video'),
      audio: '🎵 ' + t('media.voice_message'),
      gif: '🎞 ' + t('media.gif'),
      sticker: '🎭 ' + t('media.sticker'),
      document: '📄 ' + t('media.document'),
      link: '🔗 ' + t('media.link'),
      location: '📍 ' + t('media.location'),
      live_location: '📍 ' + t('media.live_location'),
      vcard: '👤 ' + t('media.contact'),
    };
    return map[type] || ('📎 ' + t('gallery.title'));
  }
  function chatPreview(chat) {
    const prefix = chat.last_msg_from_me ? (t('common.you') + ': ') : '';
    if (chat.last_msg_type === 'service') {
      const sysText = chat.last_msg_system_event
        ? formatSystemEventText({ system_event: chat.last_msg_system_event, text_body: chat.last_msg_preview })
        : chat.last_msg_preview;
      return esc(sysText || '…');
    }
    if (chat.last_msg_type === 'deleted') {
      const delText = chat.last_msg_preview === 'This message was deleted by an admin'
        ? t('msg.deleted_admin')
        : t(chat.last_msg_from_me ? 'msg.deleted_you' : 'msg.deleted_other');
      return '<span class="deleted-preview">🚫 ' + esc(delText) + '</span>';
    }
    if (chat.last_msg_type === 'poll') {
      return prefix + '📊 ' + esc(chat.last_msg_preview || t('msg.poll'));
    }
    if (chat.is_view_once) {
      const voLabel = chat.last_msg_type === 'video' ? t('media.video') : (chat.last_msg_type === 'audio' ? t('media.voice_message') : t('media.photo'));
      return prefix + '<span class="vo-preview">① ' + esc(voLabel) + '</span>';
    }
    if (chat.last_msg_type !== 'text') {
      return prefix + esc(getMediaLabel(chat.last_msg_type));
    }
    const text = chat.last_msg_preview || '';
    return (prefix + esc(text.slice(0, 60))) || '…';
  }

  function _findChatListItem(chatId, chatType) {
    const idSel = CSS.escape(String(chatId));
    const typeSel = CSS.escape(String(chatType));
    return document.querySelector(`.chat-item[data-id="${idSel}"][data-type="${typeSel}"]`);
  }

  function _formatEphemeralDuration(sec) {
    const s = Number(sec) || 0;
    if (s <= 0) return t('chat_info.ephemeral_off');
    if (s === 86400) return t('chat_info.ephemeral_24h');
    if (s === 604800) return t('chat_info.ephemeral_7d');
    if (s === 7776000) return t('chat_info.ephemeral_90d');
    if (s >= 86400) return tp('time.days', Math.round(s / 86400));
    return Math.max(1, Math.round(s / 3600)) + 'h';
  }

  function _ephemeralTooltip(sec, hasPreservation) {
    const dur = _formatEphemeralDuration(sec);
    let tip = t('sidebar.disappearing_title', { duration: dur });
    if (hasPreservation) {
      tip += ' • ' + t('sidebar.disappearing_preserved_note');
    }
    return tip;
  }

  function _createChatListItem(chat) {
    const el = document.createElement('div');
    const isActive = currentChat && String(currentChat.id) === String(chat.id) && currentChat.type === chat.type;
    el.className = 'chat-item' + (chat.type === 'group' ? ' group' : '') + (isActive ? ' active' : '');
    el.dataset.id = chat.id;
    el.dataset.type = chat.type;

    const avatarWrap = document.createElement('div');
    avatarWrap.className = 'chat-item-avatar-wrap';

    const avatar = document.createElement('div');
    avatar.className = 'chat-item-avatar';
    const avatarKey = chat.phone_number || chat.id || chat.display_name;
    avatar.style.backgroundColor = getAvatarColor(avatarKey);
    avatar.textContent = getInitials(chat.display_name);
    avatar.setAttribute('aria-hidden', 'true');
    avatarWrap.appendChild(avatar);

    const ephemSec = Number(chat.ephemeral_duration) || 0;
    if (ephemSec > 0) {
      const ephemBadge = document.createElement('span');
      ephemBadge.className = 'chat-ephemeral-badge';
      const ephemTip = _ephemeralTooltip(ephemSec, Boolean(chat.has_preservation));
      ephemBadge.title = ephemTip;
      ephemBadge.setAttribute('aria-label', ephemTip);
      ephemBadge.innerHTML = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg>';
      avatarWrap.appendChild(ephemBadge);
    }
    el.appendChild(avatarWrap);

    const body = document.createElement('div');
    body.className = 'chat-item-body';
    const tagHtml = chat.is_preserved_only
      ? `<span class="chat-preserved-tag" title="${esc(t('sidebar.preserved_badge_title'))}">${esc(t('common.preserved'))}</span>`
      : '';
    const unreadCount = Number(chat.unread_count) || 0;
    const timeStr = fmtTime(chat.newest_ts);
    const timeCls = 'chat-time' + (unreadCount > 0 ? ' is-unread' : '');

    let badgesHtml = '';
    if (chat.is_pinned) {
      const pinTitle = esc(t('sidebar.pinned_title'));
      badgesHtml += `<span class="chat-pin-icon" title="${pinTitle}" aria-label="${pinTitle}"><svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M16 12V4h1V2H7v2h1v8l-2 2v2h5.2v6h1.6v-6H18v-2l-2-2z"/></svg></span>`;
    }
    if (unreadCount > 0) {
      const unreadText = unreadCount > 99 ? '99+' : String(unreadCount);
      const unreadTip = esc(tp('sidebar.unread_tooltip', unreadCount, { count: unreadCount }));
      badgesHtml += `<span class="chat-unread-badge" title="${unreadTip}" aria-label="${unreadTip}">${unreadText}</span>`;
    }
    const badgesWrap = badgesHtml ? `<div class="chat-badges">${badgesHtml}</div>` : '';

    body.innerHTML = `<div class="chat-item-top"><div class="chat-name">${esc(chat.display_name)}${tagHtml}</div><span class="${timeCls}">${esc(timeStr)}</span></div><div class="chat-item-bottom"><div class="chat-meta">${chatPreview(chat)}</div>${badgesWrap}</div>`;
    el.appendChild(body);

    el.addEventListener('click', () => selectChat(chat, el));
    return el;
  }

  function renderChatList() {
    const list = document.getElementById('chat-list');
    list.innerHTML = '';
    const activeChats = allChats.filter(c => !c.is_archived);
    let visible;
    if (currentFilter === 'all') {
      visible = activeChats;
    } else if (currentFilter === 'unread') {
      visible = activeChats.filter(c => (Number(c.unread_count) || 0) > 0);
    } else {
      visible = activeChats.filter(c => c.type === currentFilter);
    }
    if (visible.length === 0 && currentFilter === 'unread') {
      list.innerHTML = `<div class="sidebar-tab-placeholder"><span class="placeholder-icon">✓</span><strong>${esc(t('sidebar.no_unread_chats'))}</strong></div>`;
      return;
    }
    visible.forEach(chat => list.appendChild(_createChatListItem(chat)));
  }

  function renderArchivedList(searchQuery) {
    const list = document.getElementById('chat-list');
    if (!list) return;
    list.innerHTML = '';

    const archivedChats = allChats.filter(c => Boolean(c.is_archived));
    if (archivedChats.length === 0) {
      list.innerHTML = `<div class="sidebar-tab-placeholder"><span class="placeholder-icon">📦</span><strong>${esc(t('sidebar.archived_title'))}</strong><span>${esc(t('sidebar.archived_empty'))}</span></div>`;
      return;
    }

    const q = (searchQuery || '').trim().toLowerCase();
    const matches = q
      ? archivedChats.filter(c => (c.display_name || '').toLowerCase().includes(q) || (c.last_msg_preview || '').toLowerCase().includes(q) || String(c.id || '').toLowerCase().includes(q))
      : archivedChats;

    if (matches.length === 0) {
      list.innerHTML = `<div class="sidebar-tab-placeholder"><span class="placeholder-icon">🔍</span><strong>${esc(t('sidebar.no_matches'))}</strong><span>${esc(t('sidebar.no_archived_match', { query: searchQuery }))}</span></div>`;
      return;
    }

    matches.forEach(chat => list.appendChild(_createChatListItem(chat)));
  }

  // ---- phone layout: list and pane as two levels -------------------------------

  // On phones (see 18_phone_layout.css) only one of the two is visible: the sidebar list, or the pane with
  // the open chat or call details (body.pane-open). Opening the pane on a phone adds a history entry, so the
  // browser back gesture returns to the list instead of leaving the page. The entry and the class always go
  // together: the entry is only removed by going back, which also closes the pane.
  const phoneLayout = window.matchMedia(PHONE_LAYOUT_QUERY);

  function _paneHistoryEntry() {
    return !!(history.state && history.state.wabPane);
  }

  function _keyboardFocus() {
    const el = document.activeElement;
    return !!el && el !== document.body && el.matches(':focus-visible');
  }

  function showDetailPane() {
    if (document.body.classList.contains('pane-open')) return;
    const fromKeyboard = _keyboardFocus();
    document.body.classList.add('pane-open');
    if (!phoneLayout.matches) return;
    if (!_paneHistoryEntry()) history.pushState({ wabPane: true }, '');
    // Keyboard users would be left on an element that is now hidden: take them to the back button.
    // Not after a tap, where focusing it would only draw a focus ring.
    if (fromKeyboard) {
      const backBtn = Array.from(document.querySelectorAll('.pane-back-btn')).find(b => b.offsetWidth > 0 || b.offsetParent !== null);
      if (backBtn) backBtn.focus({ preventScroll: true });
    }
  }

  function showListPane() {
    if (!document.body.classList.contains('pane-open')) return;
    const fromKeyboard = _keyboardFocus();
    document.body.classList.remove('pane-open');
    if (!phoneLayout.matches) return;
    if (document.activeElement && document.activeElement.closest('#chat-pane')) document.activeElement.blur();
    // Back on the list from the keyboard: focus the item that was open, when it can take focus (call items can).
    const active = document.querySelector('#chat-list .active');
    if (fromKeyboard && active && active.tabIndex >= 0) active.focus({ preventScroll: true });
  }

  // Closing the pane from the page (back button, Escape, a tab of the bottom bar) also drops its history entry.
  let _ownBackPending = false;

  function closeDetailPane() {
    showListPane();
    if (_paneHistoryEntry()) {
      _ownBackPending = true;
      history.back();
    }
  }

  // The topmost layer opened over the pane, with the function that closes it. Back closes that first.
  function _closeTopPaneOverlay() {
    const lb = document.getElementById('img-lightbox');
    if (lb && !lb.classList.contains('is-closing')) { _closeLb(); return true; }
    const layers = [
      ['msg-details-popup', 'visible', hideMsgDetails],
      ['map-chooser-modal', 'open', closeMapChooser],
      ['chat-menu-dropdown', 'open', closeChatMenu],
      ['chat-starred-drawer', 'open', closeChatStarred],
      ['chat-info-panel', 'open', closeChatInfo],
      ['media-gallery', 'open', closeMediaGallery],
    ];
    for (const [id, cls, close] of layers) {
      const el = document.getElementById(id);
      if (el && el.classList.contains(cls)) { close(); return true; }
    }
    return false;
  }

  document.querySelectorAll('.pane-back-btn').forEach(btn => {
    btn.addEventListener('click', closeDetailPane);
  });

  window.addEventListener('popstate', () => {
    if (_ownBackPending) {
      // Our own history.back() from closeDetailPane(). If a chat was opened again before it landed, the
      // pane found the old entry still there and did not add one: add it now.
      _ownBackPending = false;
      if (document.body.classList.contains('pane-open') && phoneLayout.matches) history.pushState({ wabPane: true }, '');
      return;
    }
    if (!document.body.classList.contains('pane-open')) return;
    if (phoneLayout.matches && _closeTopPaneOverlay()) {
      // The pane stays open: put its entry back for the next back gesture.
      history.pushState({ wabPane: true }, '');
      return;
    }
    showListPane();
  });

  // Rotating or resizing across the breakpoint: a pane opened on a wide screen gets its entry when the
  // screen becomes a phone one. Going wide keeps the entry, so rotating back does not add another.
  phoneLayout.addEventListener('change', () => {
    if (phoneLayout.matches && document.body.classList.contains('pane-open') && !_paneHistoryEntry()) {
      history.pushState({ wabPane: true }, '');
    }
  });

  // A reload starts on the list: an entry left by the previous page would make the next back a no-op.
  if (_paneHistoryEntry()) history.replaceState(null, '');

  // ---- bottom navigation bar -----------------------------------------------

  let currentNavTab = 'chats';

  function switchNavTab(tabName) {
    if (currentNavTab === tabName) return;
    currentNavTab = tabName;

    const tabs = ['chats', 'calls', 'starred', 'archived'];
    tabs.forEach(t => {
      const btn = document.getElementById('tab-btn-' + t);
      if (btn) {
        const isActive = t === tabName;
        btn.classList.toggle('active', isActive);
        btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
        btn.setAttribute('tabindex', isActive ? '0' : '-1');
      }
    });

    const filtersEl = document.getElementById('sidebar-filters');
    const searchInput = document.getElementById('search-input');
    const chatListEl = document.getElementById('chat-list');
    const searchResults = document.getElementById('search-results');
    if (searchResults) {
      searchResults.classList.remove('has-results');
      searchResults.innerHTML = '';
    }
    if (searchInput) {
      searchInput.value = '';
    }
    updateSidebarSearchClearBtn();

    if (tabName === 'chats') {
      if (searchInput) searchInput.placeholder = t('sidebar.search_chats_placeholder');
      if (filtersEl) filtersEl.style.display = 'flex';
      const callInspector = document.getElementById('pane-call-inspector');
      if (callInspector) callInspector.style.display = 'none';
      if (currentChat) {
        const chatHeader = document.getElementById('chat-header');
        const msgScroll = document.getElementById('message-scroll');
        const emptyPane = document.getElementById('empty-pane');
        if (chatHeader) chatHeader.style.display = 'flex';
        if (msgScroll) msgScroll.style.display = 'flex';
        if (emptyPane) emptyPane.style.display = 'none';
      } else {
        const emptyPane = document.getElementById('empty-pane');
        if (emptyPane) {
          emptyPane.textContent = t('sidebar.select_chat');
          emptyPane.style.display = 'flex';
        }
      }
      renderChatList();
    } else {
      if (filtersEl) filtersEl.style.display = 'none';
      const callInspector = document.getElementById('pane-call-inspector');
      if (callInspector) callInspector.style.display = 'none';

      if (tabName === 'calls') {
        if (searchInput) searchInput.placeholder = t('sidebar.search_calls_placeholder');
        const emptyPane = document.getElementById('empty-pane');
        const chatHeader = document.getElementById('chat-header');
        const msgScroll = document.getElementById('message-scroll');
        const pinnedBanner = document.getElementById('pinned-banner');
        const contextBanner = document.getElementById('context-jump-banner');

        if (chatHeader) chatHeader.style.display = 'none';
        if (msgScroll) msgScroll.style.display = 'none';
        if (pinnedBanner) pinnedBanner.style.display = 'none';
        if (contextBanner) contextBanner.style.display = 'none';
        closeChatInfo();
        closeChatStarred();
        closeChatMenu();
        closeMediaGallery();

        if (emptyPane) {
          emptyPane.textContent = t('sidebar.select_call');
          emptyPane.style.display = 'flex';
        }

        loadCallsList(true);
      } else if (tabName === 'starred') {
        _activeStarredMsgId = null;
        if (searchInput) searchInput.placeholder = t('sidebar.search_starred_placeholder');
        const emptyPane = document.getElementById('empty-pane');
        const chatHeader = document.getElementById('chat-header');
        const msgScroll = document.getElementById('message-scroll');
        const pinnedBanner = document.getElementById('pinned-banner');
        const contextBanner = document.getElementById('context-jump-banner');

        if (chatHeader) chatHeader.style.display = 'none';
        if (msgScroll) msgScroll.style.display = 'none';
        if (pinnedBanner) pinnedBanner.style.display = 'none';
        if (contextBanner) contextBanner.style.display = 'none';
        closeChatInfo();
        closeChatStarred();
        closeChatMenu();
        closeMediaGallery();

        if (emptyPane) {
          emptyPane.textContent = t('sidebar.select_starred');
          emptyPane.style.display = 'flex';
        }

        loadStarredList(true);
      } else if (tabName === 'archived') {
        if (searchInput) searchInput.placeholder = t('sidebar.search_archived_placeholder');
        const emptyPane = document.getElementById('empty-pane');
        const chatHeader = document.getElementById('chat-header');
        const msgScroll = document.getElementById('message-scroll');
        const pinnedBanner = document.getElementById('pinned-banner');
        const contextBanner = document.getElementById('context-jump-banner');

        if (chatHeader) chatHeader.style.display = 'none';
        if (msgScroll) msgScroll.style.display = 'none';
        if (pinnedBanner) pinnedBanner.style.display = 'none';
        if (contextBanner) contextBanner.style.display = 'none';
        closeChatInfo();
        closeChatStarred();
        closeChatMenu();
        closeMediaGallery();

        if (emptyPane) {
          emptyPane.textContent = t('sidebar.select_archived');
          emptyPane.style.display = 'flex';
        }

        renderArchivedList('');
      }
    }
  }

  const bottomBar = document.getElementById('sidebar-bottom-bar');
  if (bottomBar) {
    const tabBtns = Array.from(bottomBar.querySelectorAll('.tab-btn'));
    tabBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        if (btn.classList.contains('disabled') || btn.disabled) return;
        const tab = btn.dataset.tab;
        if (tab) {
          closeDetailPane();
          switchNavTab(tab);
        }
      });
      btn.addEventListener('keydown', e => {
        const enabledBtns = tabBtns.filter(b => !b.classList.contains('disabled') && !b.disabled);
        const currentIdx = enabledBtns.indexOf(btn);
        if (currentIdx === -1) return;
        let targetIdx = -1;
        if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
          targetIdx = (currentIdx + 1) % enabledBtns.length;
        } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
          targetIdx = (currentIdx - 1 + enabledBtns.length) % enabledBtns.length;
        } else if (e.key === 'Home') {
          targetIdx = 0;
        } else if (e.key === 'End') {
          targetIdx = enabledBtns.length - 1;
        }
        if (targetIdx !== -1) {
          e.preventDefault();
          const targetBtn = enabledBtns[targetIdx];
          targetBtn.focus();
          const tab = targetBtn.dataset.tab;
          if (tab) {
            closeDetailPane();
            switchNavTab(tab);
          }
        }
      });
    });
  }

  // ---- select chat ---------------------------------------------------------

  function _prepareChatPane(chat, isJumping) {
    showDetailPane();
    closeChatInfo();
    closeChatStarred();
    closeChatMenu();
    closeMediaGallery();
    currentChat = chat;
    msgList = [];
    expandedMsgIds.clear();
    noMoreOlder = false;
    noMoreNewer = !isJumping;
    isUserAtBottom = !isJumping;

    const callInspector = document.getElementById('pane-call-inspector');
    if (callInspector) callInspector.style.display = 'none';

    const scroll = document.getElementById('message-scroll');
    scroll.innerHTML = '';
    scroll.style.display = 'flex';
    scroll.classList.toggle('is-navigating', isJumping);
    document.getElementById('scroll-to-bottom').classList.remove('visible');

    document.getElementById('chat-header').style.display = 'flex';
    document.getElementById('chat-title').textContent = chat.display_name;

    const bizBadge = document.getElementById('chat-header-business-badge');
    if (bizBadge) {
      bizBadge.style.display = 'none';
      const applyBizBadge = (bizInfo) => {
        if (currentChat !== chat || !bizInfo || !bizInfo.is_business) return;
        bizBadge.title = bizInfo.business_name
          ? `${t('chat.business_badge_title')}: ${bizInfo.business_name}`
          : t('chat.business_badge_title');
        bizBadge.style.display = 'inline-flex';
      };
      if (chat._bizInfo) {
        applyBizBadge(chat._bizInfo);
      } else if (chat.type === 'contact') {
        const bizParams = new URLSearchParams({ chat_id: chat.id, chat_type: chat.type });
        fetch(API + '/chat-info?' + bizParams)
          .then(r => (r.ok ? r.json() : null))
          .then(info => {
            if (!info) return;
            chat._bizInfo = {
              is_business: Boolean(info.is_business),
              business_name: info.business_name || null,
            };
            applyBizBadge(chat._bizInfo);
          })
          .catch(() => {});
      }
    }

    const avatarEl = document.getElementById('chat-header-avatar');
    if (avatarEl) {
      const avatarKey = chat.phone_number || chat.id || chat.display_name;
      avatarEl.style.backgroundColor = getAvatarColor(avatarKey);
      avatarEl.textContent = getInitials(chat.display_name);
    }

    const subtitleEl = document.getElementById('chat-subtitle');
    if (subtitleEl) {
      if (chat.type === 'group') {
        let subHtml = esc(t('chat.group_conversation'));
        if (chat.is_preserved_only) {
          subHtml += ` <span class="chat-preserved-tag" title="${esc(t('sidebar.preserved_badge_title'))}">${esc(t('common.preserved'))}</span>`;
        }
        subtitleEl.innerHTML = subHtml;
      } else {
        const formattedPhone = formatPhoneNumber(chat.phone_number || chat.id);
        let subHtml = formattedPhone ? esc(formattedPhone) : '';
        if (chat.is_preserved_only) {
          subHtml += (subHtml ? ' ' : '') + `<span class="chat-preserved-tag" title="${esc(t('sidebar.preserved_badge_title'))}">${esc(t('common.preserved'))}</span>`;
        }
        subtitleEl.innerHTML = subHtml;
      }
    }

    const infoMenuLabel = document.getElementById('chat-info-menu-label');
    if (infoMenuLabel) {
      infoMenuLabel.textContent = t(chat.type === 'group' ? 'chat.group_info' : 'chat.contact_info');
    }

    document.getElementById('empty-pane').style.display = 'none';
    const sr = document.getElementById('search-results');
    if (sr) {
      sr.classList.remove('has-results');
      sr.innerHTML = '';
    }

    if (_jumpAbortController && !isJumping) {
      _jumpAbortController.abort();
    }
    if (!isJumping) {
      hideJumpHud();
    }
    document.getElementById('chat-search-input').value = '';
    document.getElementById('chat-search-clear').style.display = 'none';
    clearChatSearch();
    document.getElementById('date-picker-input').value = '';
    document.getElementById('date-go-btn').style.display = 'none';
    document.getElementById('date-clear-btn').style.display = 'none';

    if (!isJumping) {
      const contextBanner = document.getElementById('context-jump-banner');
      if (contextBanner) contextBanner.style.display = 'none';
    }

    updateChatStarredBadge(chat.id);
    const banner = document.getElementById('pinned-banner');
    const cachedPins = _chatPinsCache.get(String(chat.id));
    if (cachedPins && cachedPins.length > 0) {
      _chatPins = cachedPins.filter(p => !_dismissedPins.has(p.id));
      _currentPinIndex = 0;
      renderPinnedBanner();
    } else if (banner) {
      _chatPins = [];
      banner.style.display = 'none';
    }
    loadChatPinnedBanner(chat.id, chat.type);
    return scroll;
  }

  async function selectChat(chat, el) {
    _indexPollGen++;
    _activeStarredMsgId = null;
    const gen = ++_loadGen;
    loading = false;
    document.querySelectorAll('.chat-item').forEach(e => e.classList.remove('active'));
    if (el) {
      el.classList.add('active');
      el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    const scroll = _prepareChatPane(chat, false);

    const loadingEl = document.getElementById('chat-loading');
    loadingEl.style.display = 'flex';

    try {
      await loadMessages('older');
      if (gen !== _loadGen) return;
      suppressScroll = true;
      scroll.scrollTop = scroll.scrollHeight;
      suppressScroll = false;
    } catch (err) {
      console.error('Failed to load chat messages:', err);
    } finally {
      if (gen === _loadGen) {
        loadingEl.style.display = 'none';
        suppressScroll = false;
      }
    }
    if (gen === _loadGen) {
      await _pinToBottom(gen);
    }

    _startIndexPoll(chat);
  }
