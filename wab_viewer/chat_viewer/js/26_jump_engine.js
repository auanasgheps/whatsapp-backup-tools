// ---- cross-chat jump engine ----------------------------------------------

  function showContextJumpBanner(icon, text, onSwitch) {
    const contextBanner = document.getElementById('context-jump-banner');
    if (!contextBanner) return;
    contextBanner.style.display = 'flex';
    const bannerIcon = contextBanner.querySelector('.context-jump-icon');
    if (bannerIcon) bannerIcon.textContent = icon;
    const bannerText = document.getElementById('context-jump-text');
    if (bannerText) bannerText.textContent = text;
    const switchBtn = document.getElementById('context-jump-switch-btn');
    if (switchBtn) {
      switchBtn.onclick = onSwitch;
    }
    updateJumpHudPosition();
  }

  function _findChatInList(chatId, chatType) {
    const cid = String(chatId);
    if (chatType) {
      const exact = allChats.find(c => String(c.id) === cid && c.type === chatType);
      if (exact) return exact;
    }
    return allChats.find(c => String(c.id) === cid || c.jid === chatId) || null;
  }

  async function navigateToMentionChat(chatId, chatType) {
    if (!chatId) return;
    let chat = _findChatInList(chatId, chatType || 'contact');
    if (!chat) {
      try {
        await loadChats();
        chat = _findChatInList(chatId, chatType || 'contact');
      } catch (err) {
        console.warn('Failed to reload chats for mention navigation:', err);
      }
    }
    if (!chat) return;
    if (chat.is_archived) {
      if (currentNavTab !== 'archived') switchNavTab('archived');
    } else {
      if (currentNavTab !== 'chats') switchNavTab('chats');
      if (currentFilter !== 'all' && currentFilter !== chat.type) {
        resetFilterTo('all');
      }
    }
    const itemEl = _findChatListItem(chat.id, chat.type);
    await selectChat(chat, itemEl);
  }

  const _msgScrollForMentions = document.getElementById('message-scroll');
  if (_msgScrollForMentions) {
    _msgScrollForMentions.addEventListener('click', e => {
      const link = e.target.closest('a.wa-mention-link');
      if (!link) return;
      e.preventDefault();
      e.stopPropagation();
      navigateToMentionChat(link.dataset.chatId, link.dataset.chatType);
    });
  }

  async function openChatAtMessage(chatId, messageId, chatType, chatName) {
    if (_jumpAbortController) {
      _jumpAbortController.abort();
    }
    _jumpAbortController = new AbortController();
    const abortSignal = _jumpAbortController.signal;
    const gen = ++_loadGen;
    loading = true;

    let chat = _findChatInList(chatId, chatType);
    if (!chat) {
      try {
        await loadChats();
        chat = _findChatInList(chatId, chatType);
      } catch (err) {
        if (err.name === 'AbortError') return null;
      }
    }
    if (!chat) {
      if (chatId !== undefined && chatId !== null && String(chatId) !== '') {
        chat = {
          id: String(chatId),
          type: chatType || 'contact',
          display_name: chatName || String(chatId),
          phone_number: '',
          is_archived: 0,
          is_preserved_only: false
        };
      } else {
        loading = false;
        showJumpHudError('Unable to locate chat for this message');
        return null;
      }
    }

    if (currentNavTab === 'starred') {
      _activeStarredMsgId = String(messageId);
      document.querySelectorAll('.chat-item').forEach(e => {
        const match = e.dataset.msgId === String(messageId);
        e.classList.toggle('active', match);
        if (match) e.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      });
    } else {
      _activeStarredMsgId = null;
      document.querySelectorAll('.chat-item').forEach(e => {
        const match = e.dataset.id === String(chat.id) && e.dataset.type === chat.type;
        e.classList.toggle('active', match);
        if (match) e.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      });
    }

    const scroll = _prepareChatPane(chat, true);
    if (!_chatPinsCache.has(String(chat.id))) {
      await loadChatPinnedBanner(chat.id, chat.type);
    }

    const loadingEl = document.getElementById('chat-loading');
    loadingEl.style.display = 'flex';
    showJumpHud(t('chat.jumping_context'));

    try {
      const params = new URLSearchParams({
        chat_id: chat.id,
        chat_type: chat.type,
        msg_id: messageId,
        limit: 50
      });
      const res = await fetch(API + '/messages/at?' + params, { signal: abortSignal });
      if (!res.ok) {
        throw new Error(`Server returned HTTP ${res.status} when loading messages at msg_id ${messageId}`);
      }
      const msgs = await res.json();
      if (gen !== _loadGen) return null;

      mergePage(msgs);
      renderWindow();

      let targetEl = rowForMsgId(messageId);
      if (targetEl) {
        await _pinToMsg(targetEl, gen);
        flashHighlightRow(targetEl, 'gold');
      }

      showContextJumpBanner('⭐', t('chat.context_banner'), () => {
        const contextBanner = document.getElementById('context-jump-banner');
        if (contextBanner) contextBanner.style.display = 'none';
        _activeStarredMsgId = null;
        if (chat.is_archived) {
          switchNavTab('archived');
        } else {
          switchNavTab('chats');
          resetFilterTo('all');
        }
        const activeItem = _findChatListItem(chat.id, chat.type);
        if (activeItem) activeItem.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      });

      hideJumpHud();
      return targetEl || null;
    } catch (err) {
      if (err.name === 'AbortError') return null;
      showJumpHudError(t('chat.unable_to_load'));
      throw err;
    } finally {
      if (scroll) scroll.classList.remove('is-navigating');
      if (gen === _loadGen) {
        loadingEl.style.display = 'none';
        loading = false;
      }
    }
  }

  async function openChatAtTimestamp(chatId, timestampMs) {
    const targetCallMsgId = _pendingJumpCallId;
    _pendingJumpCallId = null;
    if (_jumpAbortController) {
      _jumpAbortController.abort();
    }
    _jumpAbortController = new AbortController();
    const abortSignal = _jumpAbortController.signal;
    const gen = ++_loadGen;
    loading = true;

    let chat = allChats.find(c => String(c.id) === String(chatId) || c.jid === chatId);
    if (!chat) {
      await loadChats();
      chat = allChats.find(c => String(c.id) === String(chatId) || c.jid === chatId);
    }
    if (!chat) {
      loading = false;
      showJumpHudError(t('calls.no_chat_found'));
      return null;
    }

    document.querySelectorAll('.chat-item').forEach(e => {
      const match = e.dataset.id === String(chat.id) && e.dataset.type === chat.type;
      e.classList.toggle('active', match);
      if (match) e.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    });

    const scroll = _prepareChatPane(chat, true);
    if (!_chatPinsCache.has(String(chat.id))) {
      await loadChatPinnedBanner(chat.id, chat.type);
    }

    const loadingEl = document.getElementById('chat-loading');
    loadingEl.style.display = 'flex';
    showJumpHud(t('chat.jumping_moment'));

    try {
      const params = new URLSearchParams({
        chat_id: chat.id,
        chat_type: chat.type,
        ts: timestampMs,
        limit: 50
      });
      const res = await fetch(API + '/messages/at?' + params, { signal: abortSignal });
      if (!res.ok) {
        throw new Error(`Server returned HTTP ${res.status} when loading messages at timestamp ${timestampMs}`);
      }
      const msgs = await res.json();
      if (gen !== _loadGen) return null;

      mergePage(msgs);
      renderWindow();

      let targetEl = targetCallMsgId ? rowForMsgId(targetCallMsgId) : null;
      let minDiff = targetEl ? Math.abs(Number(targetEl.dataset.ts || timestampMs) - timestampMs) : Infinity;
      let targetTs = targetEl ? Number(targetEl.dataset.ts || timestampMs) : 0;
      if (!targetEl) {
        document.querySelectorAll('.msg-row').forEach(row => {
          const rowTs = Number(row.dataset.ts);
          if (rowTs) {
            const diff = Math.abs(rowTs - timestampMs);
            if (diff < minDiff) {
              minDiff = diff;
              targetEl = row;
              targetTs = rowTs;
            }
          }
        });
      }

      if (targetEl) {
        await _pinToMsg(targetEl, gen);
        flashHighlightRow(targetEl, 'emerald');
      }

      const gapSuffix = targetTs ? fmtTimeGap(minDiff, targetTs < timestampMs) : '';
      showContextJumpBanner('📞', `Viewing Call Moment in context${gapSuffix}`, () => {
        const contextBanner = document.getElementById('context-jump-banner');
        if (contextBanner) contextBanner.style.display = 'none';
        if (chat.is_archived) {
          switchNavTab('archived');
        } else {
          switchNavTab('chats');
          resetFilterTo('all');
        }
        const activeItem = _findChatListItem(chat.id, chat.type);
        if (activeItem) activeItem.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      });

      hideJumpHud();
      return targetEl || null;
    } catch (err) {
      if (err.name === 'AbortError') return null;
      showJumpHudError('Unable to load moment');
      throw err;
    } finally {
      if (scroll) scroll.classList.remove('is-navigating');
      if (gen === _loadGen) {
        loadingEl.style.display = 'none';
        loading = false;
      }
    }
  }
