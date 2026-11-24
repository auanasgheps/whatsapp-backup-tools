// ---- starred messages sidebar list ---------------------------------------

  let _starredList = [];
  let _starredOffset = 0;
  let _starredHasMore = false;
  let _starredLoading = false;
  let _activeStarredMsgId = null;

  async function loadStarredList(reset) {
    if (_starredLoading) return;
    _starredLoading = true;
    const chatListEl = document.getElementById('chat-list');
    if (!chatListEl) {
      _starredLoading = false;
      return;
    }

    const searchInput = document.getElementById('search-input');
    const q = (currentNavTab === 'starred' && searchInput) ? searchInput.value.trim() : '';

    if (reset) {
      _starredOffset = 0;
      _starredList = [];
      chatListEl.innerHTML = '<div id="chat-list-notice"><div class="loading-spinner"></div>Loading starred messages…</div>';
    }

    try {
      const params = new URLSearchParams({
        offset: String(_starredOffset),
        limit: '50'
      });
      if (q) params.set('q', q);

      const res = await fetch(`${API}/starred?${params}`);
      if (!res.ok) {
        throw new Error(`Server returned HTTP ${res.status} when fetching starred messages`);
      }
      const data = await res.json();
      if (currentNavTab !== 'starred') return;

      const msgs = data.messages || [];
      _starredHasMore = Boolean(data.has_more);
      _starredOffset += msgs.length;

      if (reset) {
        chatListEl.innerHTML = '';
        if (msgs.length === 0) {
          if (q) {
            chatListEl.innerHTML = `<div class="sidebar-tab-placeholder"><span class="placeholder-icon">🔍</span><strong>No Matches</strong><span>No starred bookmarks matching "${esc(q)}"</span></div>`;
          } else {
            chatListEl.innerHTML = '<div class="sidebar-tab-placeholder"><span class="placeholder-icon">⭐</span><strong>Starred Messages</strong><span>No starred bookmarks found</span></div>';
          }
          return;
        }
      }

      _starredList.push(...msgs);
      msgs.forEach(m => {
        chatListEl.appendChild(renderStarredItem(m));
      });
    } catch (err) {
      if (currentNavTab !== 'starred') return;
      if (reset) {
        chatListEl.innerHTML = '<div class="sidebar-tab-placeholder"><span class="placeholder-icon">⭐</span><strong>Starred Messages</strong><span>Failed to load bookmarks</span></div>';
      }
      console.error('Failed to load starred bookmarks:', err);
    } finally {
      _starredLoading = false;
    }
  }

  function renderStarredItem(m) {
    const el = document.createElement('div');
    const isActive = _activeStarredMsgId !== null && _activeStarredMsgId === String(m.id);
    el.className = 'chat-item' + (m.chat_type === 'group' ? ' group' : '') + (isActive ? ' active' : '');
    el.dataset.msgId = String(m.id);
    el.dataset.chatId = String(m.chat_id);
    if (m.chat_type) el.dataset.chatType = m.chat_type;

    const avatar = document.createElement('div');
    avatar.className = 'chat-item-avatar';
    const chatName = m.chat_name || 'Chat';
    const avatarKey = m.chat_phone || m.chat_id || chatName;
    avatar.style.backgroundColor = getAvatarColor(avatarKey);
    avatar.textContent = getInitials(chatName);
    avatar.setAttribute('aria-hidden', 'true');
    el.appendChild(avatar);

    const body = document.createElement('div');
    body.className = 'chat-item-body';
    const preview = _formatStarredPreview(m);
    const rawSender = m.sender_name || '';
    const cleanSender = _isUnreadableToken(rawSender) ? '' : rawSender;
    const authorPrefix = cleanSender ? `${cleanSender}: ` : '';
    body.innerHTML = `<div class="chat-name">${esc(chatName)}</div><div class="chat-meta">${esc(authorPrefix + preview)} · ${fmtTime(m.timestamp * 1000)}</div>`;
    el.appendChild(body);

    el.addEventListener('click', () => {
      openChatAtMessage(m.chat_id, m.id, m.chat_type, m.chat_name);
    });
    return el;
  }

  function filterStarredList(searchQuery) {
    const list = document.getElementById('chat-list');
    if (!list) return;

    if (_starredList.length === 0) {
      list.innerHTML = '<div class="sidebar-tab-placeholder"><span class="placeholder-icon">⭐</span><strong>Starred Messages</strong><span>No starred bookmarks found</span></div>';
      return;
    }

    const q = (searchQuery || '').trim().toLowerCase();
    const matches = q
      ? _starredList.filter(m => (m.chat_name || '').toLowerCase().includes(q) || (m.sender_name || '').toLowerCase().includes(q) || (m.text || m.text_body || '').toLowerCase().includes(q))
      : _starredList;

    list.innerHTML = '';
    if (matches.length === 0) {
      list.innerHTML = `<div class="sidebar-tab-placeholder"><span class="placeholder-icon">🔍</span><strong>No Matches</strong><span>No starred bookmarks matching "${esc(searchQuery)}"</span></div>`;
      return;
    }

    matches.forEach(m => {
      list.appendChild(renderStarredItem(m));
    });
  }
