// ---- scroll trigger -------------------------------------------------------

  function setupScrollTrigger() {
    const scroll = document.getElementById('message-scroll');
    const btn = document.getElementById('scroll-to-bottom');
    let scrollRafPending = false;
    scroll.addEventListener('scroll', function () {
      if (suppressScroll || scrollRafPending) return;
      scrollRafPending = true;
      requestAnimationFrame(() => {
        scrollRafPending = false;
        if (suppressScroll) return;
        hideMsgDetails();
        const atBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 100;
        isUserAtBottom = atBottom;
        btn.classList.toggle('visible', !atBottom);
        if (scroll.scrollTop < 100) {
          if (loading) return;
          loadMessages('older');
        } else if (atBottom) {
          if (loading) return;
          loadMessages('newer');
        }
      });
    }, { passive: true });
    scroll.addEventListener('wheel', function (e) {
      if (e.deltaY < 0 && scroll.scrollTop < 100) {
        if (loading || noMoreOlder) return;
        loadMessages('older');
      }
    }, { passive: true });
    btn.addEventListener('click', async () => {
      isUserAtBottom = true;
      if (noMoreNewer) {
        await _pinToBottom(_loadGen);
        return;
      }
      const gen = ++_loadGen;
      msgList = [];
      noMoreOlder = false;
      noMoreNewer = true;
      await loadMessages('older');
      if (gen !== _loadGen) return;
      suppressScroll = true;
      scroll.scrollTop = scroll.scrollHeight;
      suppressScroll = false;
      if (gen !== _loadGen) return;
      await _pinToBottom(gen);
    });
  }
  setupScrollTrigger();

  function createMetaPinIcon() {
    const pinIcon = document.createElement('span');
    pinIcon.className = 'msg-meta-icon msg-pinned-icon';
    pinIcon.title = t('msg.pinned_icon');
    pinIcon.setAttribute('aria-label', t('msg.pinned_icon'));
    pinIcon.innerHTML = '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="17" x2="12" y2="22"></line><path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z"></path></svg>';
    return pinIcon;
  }

  function createMetaStarIcon() {
    const starIcon = document.createElement('span');
    starIcon.className = 'msg-meta-icon msg-starred-icon';
    starIcon.title = t('msg.starred_icon');
    starIcon.setAttribute('aria-label', t('msg.starred_icon'));
    starIcon.innerHTML = '<svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"></polygon></svg>';
    return starIcon;
  }

  function getSenderColorClass(senderName) {
    const key = (senderName || '').trim();
    if (!key || key === 'You') return '';
    let h = 2166136261;
    for (let i = 0; i < key.length; i++) {
      h ^= key.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return 'sender-color-' + ((h >>> 0) % 10);
  }

  function createSenderEl(senderName) {
    const senderEl = document.createElement('div');
    const colorCls = (currentChat && currentChat.type === 'group') ? getSenderColorClass(senderName) : '';
    senderEl.className = colorCls ? ('msg-sender ' + colorCls) : 'msg-sender';
    senderEl.textContent = senderName || '';
    return senderEl;
  }
