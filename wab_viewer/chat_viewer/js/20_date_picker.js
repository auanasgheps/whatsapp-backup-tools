// ---- date picker ----------------------------------------------------------

  document.getElementById('date-picker-input').max = new Date().toISOString().slice(0, 10);

  document.getElementById('date-picker-input').addEventListener('change', function () {
    const hasVal = !!this.value;
    document.getElementById('date-go-btn').style.display = hasVal ? 'inline-block' : 'none';
    document.getElementById('date-clear-btn').style.display = hasVal ? 'inline-block' : 'none';
  });

  document.getElementById('date-go-btn').addEventListener('click', async () => {
    const val = document.getElementById('date-picker-input').value;
    if (!val || !currentChat) return;
    const btn = document.getElementById('date-go-btn');
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    try {
      await jumpToTimestamp(new Date(val).getTime(), null, false, 'default');
    } finally {
      btn.disabled = false;
      btn.removeAttribute('aria-busy');
    }
  });

  document.getElementById('date-clear-btn').addEventListener('click', () => {
    document.getElementById('date-picker-input').value = '';
    document.getElementById('date-go-btn').style.display = 'none';
    document.getElementById('date-clear-btn').style.display = 'none';
  });

  // Scroll to bottom and hold it there as lazy media loads and shifts layout.
  // Re-snaps whenever images finish loading or layout dimensions change,
  // until layout holds steady or 1500 ms have passed. Bails immediately if a newer load superseded this one
  // or if the user manually initiates scrolling.
  async function _pinToBottom(gen) {
    const scroll = document.getElementById('message-scroll');
    if (!scroll) return;

    const snap = () => {
      suppressScroll = true;
      scroll.scrollTop = scroll.scrollHeight;
      suppressScroll = false;
    };
    snap();

    let done = false;
    let ro = null;
    const cleanup = () => {
      if (done) return;
      done = true;
      if (ro) ro.disconnect();
      scroll.removeEventListener('wheel', onUserScroll, { capture: true });
      scroll.removeEventListener('touchstart', onUserScroll, { capture: true });
      scroll.removeEventListener('pointerdown', onUserScroll, { capture: true });
      pendingImgs.forEach(img => {
        img.removeEventListener('load', onMediaLoad);
        img.removeEventListener('error', onMediaLoad);
      });
    };

    const onUserScroll = () => {
      isUserAtBottom = (scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight) < 100;
      cleanup();
    };

    scroll.addEventListener('wheel', onUserScroll, { capture: true, passive: true });
    scroll.addEventListener('touchstart', onUserScroll, { capture: true, passive: true });
    scroll.addEventListener('pointerdown', onUserScroll, { capture: true, passive: true });

    const pendingImgs = [...scroll.querySelectorAll('img')].filter(img => !img.complete);
    let remaining = pendingImgs.length;
    const onMediaLoad = () => {
      if (done || gen !== _loadGen) return;
      snap();
      remaining--;
      if (remaining <= 0) {
        requestAnimationFrame(() => {
          if (done || gen !== _loadGen) return;
          snap();
          cleanup();
        });
      }
    };
    pendingImgs.forEach(img => {
      img.addEventListener('load', onMediaLoad, { once: true });
      img.addEventListener('error', onMediaLoad, { once: true });
    });

    if (window.ResizeObserver) {
      ro = new ResizeObserver(() => {
        if (done || gen !== _loadGen) return;
        snap();
      });
      ro.observe(scroll);
    }

    if (pendingImgs.length === 0) {
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
      if (gen === _loadGen) {
        snap();
      }
      cleanup();
      return;
    }

    const start = performance.now();
    while (!done && performance.now() - start < 1500) {
      if (gen !== _loadGen) {
        cleanup();
        return;
      }
      await new Promise(r => requestAnimationFrame(r));
    }
    cleanup();
  }

  // Center targetEl and hold it there as lazy media above it loads and shifts layout.
  // Re-centers whenever images finish loading or layout dimensions change,
  // until layout holds steady or 1500 ms have passed. Bails immediately if a newer load superseded this one.
  async function _pinToMsg(targetEl, gen) {
    const scroll = document.getElementById('message-scroll');
    if (!scroll || !targetEl) return;

    const recenter = () => {
      suppressScroll = true;
      targetEl.scrollIntoView({ behavior: 'instant', block: 'center' });
      suppressScroll = false;
    };
    recenter();

    let done = false;
    let ro = null;
    const cleanup = () => {
      if (done) return;
      done = true;
      if (ro) ro.disconnect();
      scroll.removeEventListener('wheel', onUserScroll, { capture: true });
      scroll.removeEventListener('touchstart', onUserScroll, { capture: true });
      scroll.removeEventListener('pointerdown', onUserScroll, { capture: true });
      pendingImgs.forEach(img => {
        img.removeEventListener('load', onMediaLoad);
        img.removeEventListener('error', onMediaLoad);
      });
    };

    const onUserScroll = () => {
      cleanup();
    };

    const onMediaLoad = () => {
      if (done || gen !== _loadGen) return;
      recenter();
    };

    scroll.addEventListener('wheel', onUserScroll, { capture: true, passive: true });
    scroll.addEventListener('touchstart', onUserScroll, { capture: true, passive: true });
    scroll.addEventListener('pointerdown', onUserScroll, { capture: true, passive: true });

    const pendingImgs = Array.from(scroll.querySelectorAll('img')).filter(img => !img.complete);
    pendingImgs.forEach(img => {
      img.addEventListener('load', onMediaLoad, { once: true });
      img.addEventListener('error', onMediaLoad, { once: true });
    });

    if (typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(() => {
        if (done || gen !== _loadGen) return;
        recenter();
      });
      ro.observe(scroll);
      if (targetEl) ro.observe(targetEl);
    }

    const deadline = performance.now() + 1500;
    while (!done && performance.now() < deadline) {
      await new Promise(r => requestAnimationFrame(r));
      if (gen !== _loadGen) {
        cleanup();
        return;
      }
      if (pendingImgs.length === 0 || pendingImgs.every(img => img.complete)) {
        if (performance.now() > deadline - 1000) {
          recenter();
          break;
        }
      }
    }
    cleanup();
    recenter();
  }

  let _jumpHudTimeout = null;
  let _jumpAbortController = null;

  function updateJumpHudPosition() {
    const hud = document.getElementById('chat-jump-hud');
    const scroll = document.getElementById('message-scroll');
    if (!hud) return;

    let topOffset = scroll ? scroll.offsetTop : 0;
    if (topOffset <= 0) {
      const header = document.getElementById('chat-header');
      const pinned = document.getElementById('pinned-banner');
      const context = document.getElementById('context-jump-banner');
      topOffset = 0;
      if (header && header.style.display !== 'none') topOffset += header.offsetHeight;
      if (pinned && pinned.style.display !== 'none') topOffset += pinned.offsetHeight;
      if (context && context.style.display !== 'none') topOffset += context.offsetHeight;
    }

    const finalTop = topOffset > 0 ? (topOffset + 12) : 72;
    hud.style.top = `${finalTop}px`;
  }

  window.addEventListener('resize', () => {
    const hud = document.getElementById('chat-jump-hud');
    if (hud && hud.classList.contains('visible')) {
      updateJumpHudPosition();
    }
  });

  function showJumpHud(text) {
    const hud = document.getElementById('chat-jump-hud');
    const label = document.getElementById('chat-jump-hud-text');
    if (!hud || !label) return;
    if (_jumpHudTimeout) {
      clearTimeout(_jumpHudTimeout);
      _jumpHudTimeout = null;
    }
    updateJumpHudPosition();
    hud.classList.remove('error');
    label.textContent = text;
    hud.classList.add('visible');
  }

  function hideJumpHud() {
    const hud = document.getElementById('chat-jump-hud');
    if (!hud) return;
    if (_jumpHudTimeout) {
      clearTimeout(_jumpHudTimeout);
      _jumpHudTimeout = null;
    }
    hud.classList.remove('visible', 'error');
  }

  function showJumpHudError(errorText) {
    const hud = document.getElementById('chat-jump-hud');
    const label = document.getElementById('chat-jump-hud-text');
    if (!hud || !label) return;
    if (_jumpHudTimeout) {
      clearTimeout(_jumpHudTimeout);
    }
    updateJumpHudPosition();
    hud.classList.add('error', 'visible');
    label.textContent = errorText;
    _jumpHudTimeout = setTimeout(() => {
      hud.classList.remove('visible', 'error');
      _jumpHudTimeout = null;
    }, 3000);
  }

  // exactMsgId: when the caller needs a specific message in the DOM (e.g. jumpToMessage,
  // chat search), pass its msg_id to bypass the nearest-ts early-exit.
  // isExactTarget: true when jumping to a specific message or quote (tightens threshold to 1500ms
  // instead of the 24-hour date-picker window).
  async function jumpToTimestamp(ts, exactMsgId, isExactTarget, highlightType) {
    if (exactMsgId == null) {
      const near = nearestMsgToTs(ts);
      const threshold = isExactTarget ? 1500 : 86400000;
      const existing = near && Math.abs(near.timestamp_ms - ts) <= threshold && rowForMsgId(near.msg_id);
      if (existing) {
        const gen = ++_loadGen;
        isUserAtBottom = false;
        const loadingEl = document.getElementById('chat-loading');
        if (loadingEl) loadingEl.style.display = 'none';
        const btn = document.getElementById('scroll-to-bottom');
        if (btn) btn.classList.add('visible');
        await _pinToMsg(existing, gen);
        if (isExactTarget) flashHighlightRow(existing, highlightType);
        return existing;
      }
    }

    if (_jumpAbortController) {
      _jumpAbortController.abort();
    }
    _jumpAbortController = new AbortController();
    const abortSignal = _jumpAbortController.signal;

    const gen = ++_loadGen;
    loading = true;
    isUserAtBottom = false;
    const scroll = document.getElementById('message-scroll');
    if (scroll) scroll.classList.add('is-navigating');

    const dateLabel = ts ? fmtDateSep(ts) : '';
    const hudText = dateLabel ? `Jumping to message · ${dateLabel}` : 'Jumping to message…';
    showJumpHud(hudText);

    try {
      const params = new URLSearchParams({
        chat_id: currentChat.id,
        chat_type: currentChat.type,
        limit: 50
      });
      if (ts != null) params.set('ts', ts);
      if (exactMsgId != null) params.set('msg_id', exactMsgId);
      const res = await fetch('/api/messages/at?' + params, { signal: abortSignal });
      if (!res.ok) {
        if (gen === _loadGen) {
          showJumpHudError(`Failed to load message (${res.status})`);
        }
        throw new Error(`Server returned HTTP ${res.status} when loading messages at timestamp ${ts}`);
      }
      const msgs = await res.json();
      if (gen !== _loadGen) return null;
      if (!msgs.length) {
        showJumpHudError('Message not found');
        return null;
      }

      msgList = [];
      noMoreOlder = false;
      noMoreNewer = false;
      mergePage(msgs);
      renderWindow();
      const loadingEl = document.getElementById('chat-loading');
      if (loadingEl) loadingEl.style.display = 'none';
      const btn = document.getElementById('scroll-to-bottom');
      if (btn) btn.classList.add('visible');

      let targetEl = exactMsgId != null ? rowForMsgId(exactMsgId) : null;
      if (!targetEl) {
        const near = nearestMsgToTs(ts);
        targetEl = near ? rowForMsgId(near.msg_id) : null;
      }
      if (targetEl) {
        await _pinToMsg(targetEl, gen);
        if (isExactTarget) flashHighlightRow(targetEl, highlightType);
      }
      if (gen !== _loadGen) return null;
      hideJumpHud();
      return targetEl || null;
    } catch (err) {
      if (err.name === 'AbortError') {
        return null;
      }
      if (gen === _loadGen) {
        showJumpHudError('Unable to load message');
      }
      throw err;
    } finally {
      if (scroll) scroll.classList.remove('is-navigating');
      if (gen === _loadGen) {
        loading = false;
        const loadingEl = document.getElementById('chat-loading');
        if (loadingEl) loadingEl.style.display = 'none';
      }
    }
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
