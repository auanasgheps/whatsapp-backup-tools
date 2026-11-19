// ---- chat starred flyout & count badge -----------------------------------

  let _starredBadgePollGen = 0;

  async function updateChatStarredBadge(chatId) {
    const badge = document.getElementById('chat-starred-badge');
    if (!badge) return;
    const gen = ++_starredBadgePollGen;
    try {
      const res = await fetch(`/api/chat/${encodeURIComponent(chatId)}/starred?limit=1`);
      if (gen !== _starredBadgePollGen || !currentChat || String(currentChat.id) !== String(chatId)) return;
      if (!res.ok) {
        badge.style.display = 'none';
        return;
      }
      const data = await res.json();
      const count = data.count || 0;
      if (count > 0) {
        badge.textContent = count > 99 ? '99+' : String(count);
        badge.style.display = 'inline-flex';
      } else {
        badge.style.display = 'none';
      }
    } catch {
      if (gen === _starredBadgePollGen) badge.style.display = 'none';
    }
  }

  function closeChatStarred() {
    const drawer = document.getElementById('chat-starred-drawer');
    if (drawer) {
      drawer.classList.remove('open');
      drawer.style.display = 'none';
      const triggerBtn = document.getElementById('chat-starred-btn');
      if (triggerBtn && document.activeElement && drawer.contains(document.activeElement)) {
        triggerBtn.focus();
      }
    }
  }

  async function openChatStarred() {
    if (!currentChat) return;
    closeChatMenu();
    closeChatInfo();
    closeMediaGallery();

    const drawer = document.getElementById('chat-starred-drawer');
    const headerCount = document.getElementById('chat-starred-header-count');
    const body = document.getElementById('chat-starred-body');
    const closeBtn = document.getElementById('chat-starred-close');
    if (!drawer || !body) return;

    drawer.style.display = 'flex';
    drawer.classList.add('open');
    if (closeBtn) closeBtn.focus();
    body.innerHTML = '<div style="color:var(--text-muted);padding:24px;text-align:center"><div class="chat-info-spinner"></div> Loading starred messages…</div>';

    try {
      const res = await fetch(`/api/chat/${encodeURIComponent(currentChat.id)}/starred?limit=100`);
      if (!res.ok) {
        body.innerHTML = '<div class="starred-empty"><span class="starred-empty-icon">⚠️</span><span>Failed to load starred messages</span></div>';
        return;
      }
      const data = await res.json();
      const messages = data.messages || [];
      if (headerCount) {
        headerCount.textContent = String(data.count || messages.length);
      }

      if (messages.length === 0) {
        body.innerHTML = '<div class="starred-empty"><span class="starred-empty-icon">⭐</span><span>No starred messages in this chat</span></div>';
        return;
      }

      body.innerHTML = '';
      const subheader = document.createElement('div');
      subheader.className = 'chat-starred-subheader';
      subheader.textContent = `${messages.length} starred message${messages.length === 1 ? '' : 's'} in this conversation:`;
      body.appendChild(subheader);

      messages.forEach(m => {
        const card = document.createElement('div');
        card.className = 'starred-card';
        card.setAttribute('tabindex', '0');
        const rawSender = m.sender_name || '';
        const cleanSender = _isUnreadableToken(rawSender) ? '' : rawSender;
        const senderDisplay = cleanSender || 'Unknown';
        card.setAttribute('aria-label', `Starred message from ${senderDisplay}`);

        const topRow = document.createElement('div');
        topRow.className = 'starred-card-top';

        const metaEl = document.createElement('span');
        metaEl.className = 'starred-card-meta';
        const dateStr = fmtDateSep(m.timestamp * 1000);
        metaEl.textContent = `${senderDisplay} (${dateStr})`;

        const chevron = document.createElement('span');
        chevron.className = 'starred-card-chevron';
        chevron.textContent = '›';

        topRow.appendChild(metaEl);
        topRow.appendChild(chevron);

        const snippetEl = document.createElement('div');
        snippetEl.className = 'starred-card-snippet';
        const formattedPreview = _formatStarredPreview(m);
        snippetEl.textContent = formattedPreview ? `"${formattedPreview}"` : '…';

        card.appendChild(topRow);
        card.appendChild(snippetEl);

        card.addEventListener('click', () => {
          jumpToMessage({ msg_id: m.id, timestamp_ms: m.timestamp * 1000 }, 'gold');
        });
        card.addEventListener('keydown', e => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            card.click();
          }
        });

        body.appendChild(card);
      });
    } catch {
      body.innerHTML = '<div class="starred-empty"><span class="starred-empty-icon">⚠️</span><span>Unable to load starred messages</span></div>';
    }
  }

  function toggleChatStarred() {
    const drawer = document.getElementById('chat-starred-drawer');
    if (drawer && drawer.classList.contains('open')) {
      closeChatStarred();
    } else {
      openChatStarred();
    }
  }

  const starredBtn = document.getElementById('chat-starred-btn');
  if (starredBtn) {
    starredBtn.addEventListener('click', toggleChatStarred);
  }
  const starredCloseBtn = document.getElementById('chat-starred-close');
  if (starredCloseBtn) {
    starredCloseBtn.addEventListener('click', closeChatStarred);
  }
  const starredDrawer = document.getElementById('chat-starred-drawer');
  if (starredDrawer) {
    starredDrawer.addEventListener('keydown', e => {
      if (e.key === 'Tab') {
        const focusable = Array.from(starredDrawer.querySelectorAll('button, [tabindex="0"]')).filter(
          el => el.offsetParent !== null
        );
        if (focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    });
  }
