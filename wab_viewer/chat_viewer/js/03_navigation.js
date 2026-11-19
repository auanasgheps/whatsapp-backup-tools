// ---- load chats ----------------------------------------------------------

  async function loadChats() {
    if (currentNavTab === 'chats') {
      document.getElementById('chat-list').innerHTML = '<div id="chat-list-notice"><div class="loading-spinner"></div>Loading Chats…</div>';
    }
    const res = await fetch('/api/chats');
    allChats = await res.json();
    const hasArchived = allChats.some(c => c.is_archived);
    setTabEnabled('tab-btn-archived', hasArchived, 'Archived', 'No archived chats in this backup');
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
        const r = await fetch('/api/chat-index-status?' + params);
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

  const MEDIA_LABELS = {image:'📷 Photo', video:'🎥 Video', audio:'🎵 Voice message',
                        gif:'🎞 GIF', sticker:'🎭 Sticker', document:'📄 Document', link:'🔗 Link',
                        location:'📍 Location', live_location:'📍 Live location', vcard:'👤 Contact'};
  function chatPreview(chat) {
    const prefix = chat.last_msg_from_me ? 'You: ' : '';
    if (chat.last_msg_type === 'service') {
      return esc(chat.last_msg_preview || '…');
    }
    if (chat.last_msg_type === 'deleted') {
      return '<span class="deleted-preview">🚫 ' + esc(chat.last_msg_preview || 'This message was deleted') + '</span>';
    }
    if (chat.last_msg_type === 'poll') {
      return prefix + '📊 ' + esc(chat.last_msg_preview || 'Poll');
    }
    if (chat.is_view_once) {
      const voLabel = chat.last_msg_type === 'video' ? 'Video' : (chat.last_msg_type === 'audio' ? 'Voice message' : 'Photo');
      return prefix + '<span class="vo-preview">① ' + voLabel + '</span>';
    }
    if (chat.last_msg_type !== 'text') {
      return prefix + (MEDIA_LABELS[chat.last_msg_type] || '📎 Media');
    }
    const text = chat.last_msg_preview || '';
    return (prefix + esc(text.slice(0, 60))) || '…';
  }

  function _findChatListItem(chatId, chatType) {
    const idSel = CSS.escape(String(chatId));
    const typeSel = CSS.escape(String(chatType));
    return document.querySelector(`.chat-item[data-id="${idSel}"][data-type="${typeSel}"]`);
  }

  function _createChatListItem(chat) {
    const el = document.createElement('div');
    const isActive = currentChat && String(currentChat.id) === String(chat.id) && currentChat.type === chat.type;
    el.className = 'chat-item' + (chat.type === 'group' ? ' group' : '') + (isActive ? ' active' : '');
    el.dataset.id = chat.id;
    el.dataset.type = chat.type;

    const avatar = document.createElement('div');
    avatar.className = 'chat-item-avatar';
    const avatarKey = chat.phone_number || chat.id || chat.display_name;
    avatar.style.backgroundColor = getAvatarColor(avatarKey);
    avatar.textContent = getInitials(chat.display_name);
    avatar.setAttribute('aria-hidden', 'true');
    el.appendChild(avatar);

    const body = document.createElement('div');
    body.className = 'chat-item-body';
    const tagHtml = chat.is_preserved_only
      ? '<span class="chat-preserved-tag" title="Preserved from backup • Chat deleted on device">Preserved</span>'
      : '';
    body.innerHTML = `<div class="chat-name">${esc(chat.display_name)}${tagHtml}</div><div class="chat-meta">${chatPreview(chat)} · ${fmtTime(chat.newest_ts)}</div>`;
    el.appendChild(body);

    el.addEventListener('click', () => selectChat(chat, el));
    return el;
  }

  function renderChatList() {
    const list = document.getElementById('chat-list');
    list.innerHTML = '';
    const activeChats = allChats.filter(c => !c.is_archived);
    const visible = currentFilter === 'all' ? activeChats : activeChats.filter(c => c.type === currentFilter);
    visible.forEach(chat => list.appendChild(_createChatListItem(chat)));
  }

  function renderArchivedList(searchQuery) {
    const list = document.getElementById('chat-list');
    if (!list) return;
    list.innerHTML = '';

    const archivedChats = allChats.filter(c => Boolean(c.is_archived));
    if (archivedChats.length === 0) {
      list.innerHTML = '<div class="sidebar-tab-placeholder"><span class="placeholder-icon">📦</span><strong>Archived Chats</strong><span>Chats tucked away will appear here</span></div>';
      return;
    }

    const q = (searchQuery || '').trim().toLowerCase();
    const matches = q
      ? archivedChats.filter(c => (c.display_name || '').toLowerCase().includes(q) || (c.last_msg_preview || '').toLowerCase().includes(q) || String(c.id || '').toLowerCase().includes(q))
      : archivedChats;

    if (matches.length === 0) {
      list.innerHTML = `<div class="sidebar-tab-placeholder"><span class="placeholder-icon">🔍</span><strong>No Matches</strong><span>No archived chats matching "${esc(searchQuery)}"</span></div>`;
      return;
    }

    matches.forEach(chat => list.appendChild(_createChatListItem(chat)));
  }

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
      if (searchInput) searchInput.placeholder = 'Search messages & chats…';
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
          emptyPane.textContent = 'Select a chat to browse messages';
          emptyPane.style.display = 'flex';
        }
      }
      renderChatList();
    } else {
      if (filtersEl) filtersEl.style.display = 'none';
      const callInspector = document.getElementById('pane-call-inspector');
      if (callInspector) callInspector.style.display = 'none';

      if (tabName === 'calls') {
        if (searchInput) searchInput.placeholder = 'Search call history…';
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
        closeMediaGallery();

        if (emptyPane) {
          emptyPane.textContent = 'Select a call from the history to view connection metrics and duration.';
          emptyPane.style.display = 'flex';
        }

        loadCallsList(true);
      } else if (tabName === 'starred') {
        _activeStarredMsgId = null;
        if (searchInput) searchInput.placeholder = 'Search starred bookmarks…';
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
        closeMediaGallery();

        if (emptyPane) {
          emptyPane.textContent = 'Select a starred message to jump to its conversational context.';
          emptyPane.style.display = 'flex';
        }

        loadStarredList(true);
      } else if (tabName === 'archived') {
        if (searchInput) searchInput.placeholder = 'Search archived chats…';
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
        closeMediaGallery();

        if (emptyPane) {
          emptyPane.textContent = 'Select an archived chat to browse messages.';
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
        if (tab) switchNavTab(tab);
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
          if (tab) switchNavTab(tab);
        }
      });
    });
  }

  // ---- select chat ---------------------------------------------------------

  function _prepareChatPane(chat, isJumping) {
    closeChatInfo();
    closeChatStarred();
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

    const avatarEl = document.getElementById('chat-header-avatar');
    if (avatarEl) {
      const avatarKey = chat.phone_number || chat.id || chat.display_name;
      avatarEl.style.backgroundColor = getAvatarColor(avatarKey);
      avatarEl.textContent = getInitials(chat.display_name);
    }

    const subtitleEl = document.getElementById('chat-subtitle');
    if (subtitleEl) {
      if (chat.type === 'group') {
        let subHtml = 'Group chat';
        if (chat.is_preserved_only) {
          subHtml += ' <span class="chat-preserved-tag" title="Preserved from backup • Chat deleted on device">Preserved</span>';
        }
        subtitleEl.innerHTML = subHtml;
      } else {
        const formattedPhone = formatPhoneNumber(chat.phone_number || chat.id);
        let subHtml = formattedPhone ? esc(formattedPhone) : '';
        if (chat.is_preserved_only) {
          subHtml += (subHtml ? ' ' : '') + '<span class="chat-preserved-tag" title="Preserved from backup • Chat deleted on device">Preserved</span>';
        }
        subtitleEl.innerHTML = subHtml;
      }
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
