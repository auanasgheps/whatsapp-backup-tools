// ---- sidebar search -------------------------------------------------------

  let searchTimer = null;
  const searchInputEl = document.getElementById('search-input');
  const searchClearBtn = document.getElementById('search-clear');

  function updateSidebarSearchClearBtn() {
    if (searchClearBtn && searchInputEl) {
      searchClearBtn.style.display = searchInputEl.value ? 'flex' : 'none';
    }
  }

  function clearActiveSidebarSearch() {
    if (searchInputEl) {
      searchInputEl.value = '';
    }
    updateSidebarSearchClearBtn();
    if (currentNavTab === 'chats') {
      clearSearchResults();
    } else if (currentNavTab === 'calls') {
      loadCallsList(true);
    } else if (currentNavTab === 'starred') {
      loadStarredList(true);
    } else if (currentNavTab === 'archived') {
      renderArchivedList('');
    }
    if (searchInputEl) {
      searchInputEl.focus();
    }
  }

  if (searchClearBtn) {
    searchClearBtn.addEventListener('click', clearActiveSidebarSearch);
  }

  if (searchInputEl) {
    searchInputEl.addEventListener('input', function () {
      clearTimeout(searchTimer);
      updateSidebarSearchClearBtn();
      const q = this.value.trim();

      if (currentNavTab === 'chats') {
        if (!q) {
          clearSearchResults();
          return;
        }
        searchTimer = setTimeout(() => doSearch(q), 300);
      } else if (currentNavTab === 'calls') {
        searchTimer = setTimeout(() => loadCallsList(true), 250);
      } else if (currentNavTab === 'starred') {
        searchTimer = setTimeout(() => loadStarredList(true), 250);
      } else if (currentNavTab === 'archived') {
        renderArchivedList(q);
      }
    });

    searchInputEl.addEventListener('search', function () {
      updateSidebarSearchClearBtn();
      if (!this.value.trim()) {
        clearActiveSidebarSearch();
      }
    });
  }

  async function doSearch(q) {
    // Chat and contact matches — client-side, instant
    const ql = q.toLowerCase();
    const chatMatches = allChats.filter(c => {
      if (c.display_name && c.display_name.toLowerCase().includes(ql)) return true;
      if (c.id && c.id.toLowerCase().includes(ql)) return true;
      if (c.phone_number && c.phone_number.toLowerCase().includes(ql)) return true;
      if (c.description && c.description.toLowerCase().includes(ql)) return true;
      return false;
    });

    // Show loading state while FTS fetch is in flight
    const container = document.getElementById('search-results');
    container.classList.add('has-results');
    container.innerHTML = `<div class="sr-section-label">${esc(t('search.searching'))}</div>`;

    // Full-text search across indexed chats
    const res = await fetch(API + '/search?' + new URLSearchParams({ q }));
    const data = await res.json();

    renderSearchResults(q, chatMatches, data.results, data.indexed_count);
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

  function makeChatResultItem(chat, q) {
    const el = document.createElement('div');
    el.className = 'search-result-item';

    let subHtml = '';
    if (chat.type === 'group') {
      const desc = (chat.description || '').trim();
      subHtml = desc ? highlight(desc.replace(/\s+/g, ' '), q, null, false) : esc(t('chat.group_conversation'));
    } else {
      const formattedPhone = formatPhoneNumber(chat.phone_number || chat.id);
      subHtml = formattedPhone ? highlight(formattedPhone, q, null, false) : '';
    }

    const tagHtml = chat.is_preserved_only
      ? ` <span class="chat-preserved-tag" title="${esc(t('sidebar.preserved_badge_title'))}">${esc(t('common.preserved'))}</span>`
      : '';

    el.innerHTML = `<div class="sr-chat">${highlight(chat.display_name, q, null, false)}${tagHtml}</div><div class="sr-text">${subHtml}</div>`;
    el.addEventListener('click', () => {
      document.getElementById('search-input').value = '';
      clearSearchResults();
      resetFilterTo('all');
      const el2 = _findChatListItem(chat.id, chat.type);
      if (el2) selectChat(chat, el2);
    });
    return el;
  }
  const makeContactResultItem = makeChatResultItem;

  function makeTextResultItem(r, q) {
    const el = document.createElement('div');
    el.className = 'search-result-item';
    const chatName = allChats.find(c => c.id === r.chat_id && c.type === r.chat_type)?.display_name || r.chat_id;
    const snippet = r.text_body || (r.media_name ? '[📎 ' + r.media_name + ']' : '');
    el.innerHTML = `
      <div class="sr-chat">${esc(chatName)}</div>
      <div class="sr-text">${highlight(resolveMentionsPlainText(snippet, r.mentions, r.chat_type === 'group'), q, null, false)}</div>
      <div class="sr-time">${fmtTime(r.timestamp_ms)}</div>
    `;
    el.addEventListener('click', async () => {
      document.getElementById('search-input').value = '';
      clearSearchResults();
      const chat = allChats.find(c => c.id === r.chat_id && c.type === r.chat_type);
      if (chat) {
        resetFilterTo('all');
        const el2 = _findChatListItem(r.chat_id, r.chat_type);
        if (el2) {
          await selectChat(chat, el2);
          expandMessageRowById(r.msg_id, null);
          const row = await jumpToTimestamp(r.timestamp_ms, null, true, 'default');
          if (row) {
            expandMessageRowById(row.dataset.msgId, row);
            const bubble = row.querySelector('.msg-bubble, .msg-service-bubble');
            if (bubble) {
              bubble.classList.add('search-jump-highlight');
              bubble.addEventListener('animationend', () => bubble.classList.remove('search-jump-highlight'), { once: true });
            }
          }
        }
      }
    });
    return el;
  }

  function renderSearchResults(q, chatMatches, textResults, indexedCount) {
    const container = document.getElementById('search-results');
    if (!chatMatches.length && !textResults.length) {
      clearSearchResults();
      return;
    }
    container.classList.add('has-results');
    container.innerHTML = '';

    if (chatMatches.length) {
      container.appendChild(makeSectionLabel(t('search.chats_section')));
      chatMatches.forEach(chat => container.appendChild(makeChatResultItem(chat, q)));
    }

    if (textResults.length) {
      container.appendChild(makeSectionLabel(t('search.messages_section')));
      textResults.slice(0, 50).forEach(r => container.appendChild(makeTextResultItem(r, q)));
    }

    if (indexedCount != null && indexedCount < allChats.length) {
      const notice = document.createElement('div');
      notice.className = 'search-index-notice';
      notice.textContent = t('search.indexed_progress', { indexed: indexedCount, total: allChats.length });
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
    const statusRes = await fetch(API + '/chat-index-status?chat_id=' +
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
    const res = await fetch(API + '/search?' + params);
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
      count.textContent = t('search.no_results');
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
    const existing = rowForMsgId(result.msg_id);
    expandMessageRowById(result.msg_id, existing);
    if (existing) {
      highlightMessageRow(existing);
      return;
    }
    await jumpToTimestamp(result.timestamp_ms, result.msg_id, true, 'default');
    const loaded = rowForMsgId(result.msg_id);
    if (loaded) highlightMessageRow(loaded);
  }

  function highlightMessageRow(row) {
    const bubble = row.querySelector('.msg-bubble, .msg-service-bubble');
    if (bubble) bubble.classList.add('search-highlight');
    row.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
