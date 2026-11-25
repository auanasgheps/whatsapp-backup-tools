// ---- pinned message banner -----------------------------------------------

  const _RAW_STANZA_ID_RE = /^(?:3EB0[0-9A-Za-z_-]{4,60}|[0-9A-Fa-f]{16,64}|[0-9]{15,20}[-_][0-9]+)$/;
  const _BASE64_TOKEN_RE = /^(?:[A-Za-z0-9+/]{6,}={1,2}|[A-Za-z0-9+/]{16,}={0,2}|C[A-Za-z0-9+/]{6,}={0,2})$/;
  const _LID_RE = /^[0-9]+@lid$/;

  function _isRawStanzaId(str) {
    if (!str || typeof str !== 'string') return false;
    return _RAW_STANZA_ID_RE.test(str.trim());
  }

  function _isUnreadableToken(str) {
    if (!str || typeof str !== 'string') return false;
    const s = str.trim();
    return _RAW_STANZA_ID_RE.test(s) || _BASE64_TOKEN_RE.test(s) || _LID_RE.test(s);
  }

  function _formatMessagePreview(m) {
    let preview = m.text || m.text_body || '';
    if (m.mentions && typeof resolveMentionsPlainText === 'function') {
      preview = resolveMentionsPlainText(preview, m.mentions);
    }
    if (_isUnreadableToken(preview)) {
      preview = '';
    }
    const mt = (m.media_type || '').toLowerCase();
    if (mt) {
      if (mt === 'photo' || mt === 'image') {
        return preview ? `📷 Photo • ${preview}` : '📷 Photo';
      }
      if (mt === 'video') {
        const durStr = m.duration > 0 ? ` (${fmtDuration(m.duration)})` : '';
        const cap = preview ? ` • ${preview}` : '';
        return `📹 Video${durStr}${cap}`;
      }
      if (mt === 'audio') {
        const durStr = m.duration > 0 ? ` (${fmtDuration(m.duration)})` : '';
        return `🎤 Voice message${durStr}`;
      }
      if (mt === 'document') {
        const rawFname = m.file_name || '';
        const cleanFname = _isUnreadableToken(rawFname) ? '' : rawFname;
        const cleanPreview = _isUnreadableToken(preview) ? '' : preview;
        if (cleanFname && cleanPreview && cleanFname !== cleanPreview) {
          return `📄 ${cleanFname} • ${cleanPreview}`;
        }
        const display = cleanFname || cleanPreview || 'Document';
        return `📄 ${display}`;
      }
      if (mt === 'location') {
        const rawLoc = m.place_name || preview;
        const loc = _isUnreadableToken(rawLoc) ? '' : rawLoc;
        return loc ? `📍 ${t('media.location')} • ${loc}` : `📍 ${t('media.location')}`;
      }
      if (mt === 'live_location') {
        return `📍 ${t('media.live_location')}`;
      }
      if (mt === 'vcard') {
        const rawContact = preview || t('media.contact');
        const contact = _isUnreadableToken(rawContact) ? t('media.contact') : rawContact;
        return `👤 ${contact}`;
      }
      if (mt === 'sticker') {
        return `🎭 ${t('media.sticker')}`;
      }
      if (mt === 'poll') {
        return preview ? `📊 ${t('media.poll')} • ${preview}` : `📊 ${t('media.poll')}`;
      }
      if (mt === 'gif') {
        return preview ? `🎞 ${t('media.gif')} • ${preview}` : `🎞 ${t('media.gif')}`;
      }
      if (mt === 'deleted') {
        return `🚫 ${t('msg.deleted_other')}`;
      }
      if (mt === 'link') {
        return preview ? `🔗 ${preview}` : `🔗 ${t('media.link')}`;
      }
      const label = getMediaLabel(mt);
      return preview ? `${label} • ${preview}` : label;
    }
    return preview || t(m.is_pinned ? 'msg.pinned_icon' : 'pinned.label');
  }

  function _formatStarredPreview(m) {
    return _formatMessagePreview(m);
  }

  const _dismissedPins = new Set();
  const _chatPinsCache = new Map();
  let _chatPins = [];
  let _currentPinIndex = 0;
  let _pinnedBannerPollGen = 0;

  async function loadChatPinnedBanner(chatId, chatType) {
    const banner = document.getElementById('pinned-banner');
    if (!banner) return;
    const gen = ++_pinnedBannerPollGen;
    try {
      const params = new URLSearchParams({ chat_type: chatType });
      const res = await fetch(`${API}/chat/${encodeURIComponent(chatId)}/pinned?` + params);
      if (gen !== _pinnedBannerPollGen || !currentChat || String(currentChat.id) !== String(chatId)) return;
      if (!res.ok) {
        banner.style.display = 'none';
        return;
      }
      const data = await res.json();
      const rawPins = data.pinned_messages || (data.pinned_message ? [data.pinned_message] : []);
      _chatPinsCache.set(String(chatId), rawPins);
      _chatPins = rawPins.filter(p => !_dismissedPins.has(p.id));
      _currentPinIndex = 0;
      renderPinnedBanner();
      _syncRenderedMessagePins();
    } catch {
      if (gen === _pinnedBannerPollGen) {
        banner.style.display = 'none';
      }
    }
  }

  function _syncRenderedMessagePins() {
    const activePinIds = new Set((_chatPins || []).map(p => String(p.id)));
    document.querySelectorAll('.msg-row .msg-pinned-icon').forEach(icon => {
      const row = icon.closest('.msg-row');
      if (row && !activePinIds.has(String(row.dataset.msgId))) {
        icon.remove();
      }
    });
    activePinIds.forEach(id => {
      const escId = (window.CSS && CSS.escape) ? CSS.escape(id) : id;
      const row = document.querySelector(`.msg-row[data-msg-id="${escId}"]`);
      if (row && !row.querySelector('.msg-pinned-icon')) {
        const meta = row.querySelector('.msg-meta');
        if (meta) {
          const pinIcon = document.createElement('span');
          pinIcon.className = 'msg-meta-icon msg-pinned-icon';
          pinIcon.title = 'Pinned message';
          pinIcon.setAttribute('aria-label', 'Pinned message');
          pinIcon.innerHTML = '<svg viewBox="0 0 16 16" width="12" height="12" fill="currentColor"><path d="M4.5 1.5A.5.5 0 0 1 5 1h6a.5.5 0 0 1 .5.5v2.879a2.5 2.5 0 0 0 .732 1.768l1.06 1.06A1.5 1.5 0 0 1 13 8.268V9a.5.5 0 0 1-.5.5H9v5.5a.5.5 0 0 1-1 0V9.5H3.5A.5.5 0 0 1 3 9v-.732a1.5 1.5 0 0 1 .707-1.27l1.061-1.06A2.5 2.5 0 0 0 5.5 4.379V1.5z"/></svg>';
          meta.insertBefore(pinIcon, meta.firstChild);
        }
      }
    });
  }

  function renderPinnedBanner() {
    const banner = document.getElementById('pinned-banner');
    if (!banner) return;
    if (!_chatPins || _chatPins.length === 0 || _currentPinIndex >= _chatPins.length) {
      banner.style.display = 'none';
      updateJumpHudPosition();
      return;
    }
    banner.style.display = 'flex';
    const pin = _chatPins[_currentPinIndex];
    const authorEl = document.getElementById('pinned-banner-author');
    const snippetEl = document.getElementById('pinned-banner-snippet');
    if (authorEl) {
      const pinLabel = _chatPins.length > 1
        ? `Pinned Message (${_currentPinIndex + 1} of ${_chatPins.length})`
        : 'Pinned';
      const cleanAuthor = _isUnreadableToken(pin.sender_name) ? '' : localizeSender(pin.sender_name || '');
      authorEl.textContent = cleanAuthor ? `${pinLabel} • ${cleanAuthor}:` : `${pinLabel}:`;
    }
    if (snippetEl) {
      snippetEl.textContent = _formatMessagePreview(pin);
    }

    const navEl = document.getElementById('pinned-banner-nav');
    const countEl = document.getElementById('pinned-banner-count');
    if (navEl && countEl) {
      if (_chatPins.length > 1) {
        navEl.style.display = 'flex';
        countEl.textContent = t('pinned.counter', { current: _currentPinIndex + 1, total: _chatPins.length });
      } else {
        navEl.style.display = 'none';
      }
    }
    updateJumpHudPosition();
  }

  const pinnedBanner = document.getElementById('pinned-banner');
  if (pinnedBanner) {
    const contentEl = pinnedBanner.querySelector('.pinned-banner-content');
    if (contentEl) {
      contentEl.addEventListener('click', async () => {
        if (_chatPins && _chatPins.length > 0 && _currentPinIndex < _chatPins.length) {
          const pin = _chatPins[_currentPinIndex];
          await jumpToMessage({ msg_id: pin.id, timestamp_ms: pin.timestamp * 1000 }, 'emerald');
        }
      });
      contentEl.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          contentEl.click();
        }
      });
    }

    const prevBtn = document.getElementById('pinned-banner-prev');
    if (prevBtn) {
      prevBtn.addEventListener('click', e => {
        e.stopPropagation();
        if (_chatPins.length > 1) {
          _currentPinIndex = (_currentPinIndex - 1 + _chatPins.length) % _chatPins.length;
          renderPinnedBanner();
        }
      });
    }

    const nextBtn = document.getElementById('pinned-banner-next');
    if (nextBtn) {
      nextBtn.addEventListener('click', e => {
        e.stopPropagation();
        if (_chatPins.length > 1) {
          _currentPinIndex = (_currentPinIndex + 1) % _chatPins.length;
          renderPinnedBanner();
        }
      });
    }

    const dismissBtn = document.getElementById('pinned-banner-dismiss');
    if (dismissBtn) {
      dismissBtn.addEventListener('click', e => {
        e.stopPropagation();
        if (_chatPins && _chatPins.length > 0 && _currentPinIndex < _chatPins.length) {
          const pin = _chatPins[_currentPinIndex];
          _dismissedPins.add(pin.id);
          _chatPins = _chatPins.filter(p => !_dismissedPins.has(p.id));
          if (_currentPinIndex >= _chatPins.length) {
            _currentPinIndex = 0;
          }
          renderPinnedBanner();
        }
      });
    }
  }
