// ---- calls history & inspector -------------------------------------------

  let _callsList = [];
  let _callsOffset = 0;
  let _callsHasMore = false;
  let _callsLoading = false;

  function filterCallsList(searchQuery) {
    const list = document.getElementById('chat-list');
    if (!list) return;

    if (_callsList.length === 0) {
      list.innerHTML = '<div class="sidebar-tab-placeholder"><span class="placeholder-icon">📞</span><strong>No Call History</strong><span>No calls found in this backup</span></div>';
      return;
    }

    const q = (searchQuery || '').trim().toLowerCase();
    const matches = q
      ? _callsList.filter(c => (c.caller_name || '').toLowerCase().includes(q) || (c.caller_phone || '').toLowerCase().includes(q))
      : _callsList;

    list.innerHTML = '';
    if (matches.length === 0) {
      list.innerHTML = `<div class="sidebar-tab-placeholder"><span class="placeholder-icon">🔍</span><strong>No Matches</strong><span>No calls matching "${esc(searchQuery)}"</span></div>`;
      return;
    }

    matches.forEach(c => {
      list.appendChild(renderCallItem(c));
    });
  }

  async function loadCallsList(reset) {
    if (_callsLoading) return;
    _callsLoading = true;
    const chatListEl = document.getElementById('chat-list');
    if (!chatListEl) {
      _callsLoading = false;
      return;
    }

    const searchInput = document.getElementById('search-input');
    const q = (currentNavTab === 'calls' && searchInput) ? searchInput.value.trim() : '';

    if (reset) {
      _callsOffset = 0;
      _callsList = [];
      chatListEl.innerHTML = '<div id="chat-list-notice"><div class="loading-spinner"></div>Loading Call History…</div>';
    }

    try {
      const params = new URLSearchParams({
        offset: String(_callsOffset),
        limit: '50'
      });
      if (q) params.set('q', q);

      const res = await fetch(`${API}/calls?${params}`);
      if (!res.ok) {
        throw new Error(`Server returned HTTP ${res.status} when fetching calls`);
      }
      const data = await res.json();
      if (currentNavTab !== 'calls') return;

      const calls = data.calls || [];
      _callsHasMore = Boolean(data.has_more);
      _callsOffset += calls.length;

      if (reset) {
        chatListEl.innerHTML = '';
        if (calls.length === 0) {
          if (q) {
            chatListEl.innerHTML = `<div class="sidebar-tab-placeholder"><span class="placeholder-icon">🔍</span><strong>No Matches</strong><span>No calls matching "${esc(q)}"</span></div>`;
          } else {
            chatListEl.innerHTML = '<div class="sidebar-tab-placeholder"><span class="placeholder-icon">📞</span><strong>No Call History</strong><span>No calls found in this backup</span></div>';
          }
          return;
        }
      }

      _callsList.push(...calls);
      calls.forEach(c => {
        chatListEl.appendChild(renderCallItem(c));
      });
    } catch (err) {
      if (currentNavTab !== 'calls') return;
      if (reset) {
        chatListEl.innerHTML = '<div class="sidebar-tab-placeholder"><span class="placeholder-icon">⚠️</span><strong>Call History Error</strong><span>Unable to load call records</span></div>';
      }
      console.error('Failed to load call history:', err);
    } finally {
      _callsLoading = false;
    }
  }

  function _getCallMediaSvg(isVideo, size) {
    const px = size || 16;
    if (isVideo) {
      return `<svg width="${px}" height="${px}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>`;
    }
    return `<svg width="${px}" height="${px}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"/></svg>`;
  }

  function _getCallDirectionSvg(callType) {
    if (callType === 'missed') {
      return '<svg class="call-item-arrow call-icon-missed" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/></svg>';
    }
    if (callType === 'incoming') {
      return '<svg class="call-item-arrow call-icon-incoming" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><polyline points="16 18 22 18 22 12"/><line x1="22" y1="18" x2="11" y2="7"/></svg>';
    }
    return '<svg class="call-item-arrow call-icon-outgoing" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><polyline points="8 6 2 6 2 12"/><line x1="2" y1="6" x2="13" y2="17"/></svg>';
  }

  function _formatCallOutcomeText(callType, outcome, durationSec) {
    if (callType === 'missed' && (!durationSec || durationSec <= 0)) {
      return outcome === 'declined' ? 'Declined' : 'Missed';
    }
    const dirLabel = callType === 'outgoing' ? 'Outgoing' : 'Incoming';
    if (durationSec > 0) {
      return `${dirLabel} · ${fmtDuration(durationSec)}`;
    }
    if (outcome === 'canceled') {
      return `${dirLabel} · Canceled`;
    }
    return `${dirLabel} · No answer`;
  }

  function renderCallCard(msg) {
    const c = msg.call || {};
    let callType = c.call_type || (msg.from_me ? 'outgoing' : 'incoming');
    if ((c.duration_sec && c.duration_sec > 0) && callType === 'missed') {
      callType = c.from_me ? 'outgoing' : 'incoming';
    }
    const isMissed = callType === 'missed';

    const card = document.createElement('div');
    card.className = 'msg-call-card';

    const iconWrap = document.createElement('div');
    iconWrap.className = 'msg-call-icon' + (isMissed ? ' missed' : '');
    iconWrap.innerHTML = _getCallMediaSvg(!!c.video, 18);
    card.appendChild(iconWrap);

    const body = document.createElement('div');
    body.className = 'msg-call-body';

    const title = document.createElement('div');
    title.className = 'msg-call-title';
    const baseLabel = c.video ? 'Video call' : 'Voice call';
    title.textContent = c.is_group ? `Group ${baseLabel.toLowerCase()}` : baseLabel;
    body.appendChild(title);

    const status = document.createElement('div');
    status.className = 'msg-call-status' + (isMissed ? ' missed' : '');

    const arrowSpan = document.createElement('span');
    arrowSpan.className = 'msg-call-arrow';
    arrowSpan.innerHTML = _getCallDirectionSvg(callType);
    status.appendChild(arrowSpan);

    const statusLabel = document.createElement('span');
    statusLabel.textContent = _formatCallOutcomeText(callType, c.outcome, c.duration_sec || 0);
    status.appendChild(statusLabel);

    body.appendChild(status);
    card.appendChild(body);
    return card;
  }

  function renderCallItem(call) {
    const el = document.createElement('div');
    el.className = 'call-item';
    el.dataset.callId = call.id;
    el.tabIndex = 0;
    el.setAttribute('role', 'button');

    const callerName = call.caller_name || 'Unknown';
    const tsMs = call.timestamp_ms ?? (call.timestamp * 1000);
    const timeLabel = fmtTime(tsMs);
    const durSec = call.duration || 0;
    let callType = call.call_type;
    if (durSec > 0 && callType === 'missed') {
      callType = call.from_me ? 'outgoing' : 'incoming';
    }
    const isMissed = callType === 'missed';
    const outcomeText = isMissed
      ? (call.outcome === 'declined' ? 'Declined' : 'Missed')
      : _formatCallOutcomeText(callType, durSec > 0 ? 'answered' : (call.outcome || 'unanswered'), durSec);
    el.setAttribute(
      'aria-label',
      `${callerName}, ${call.is_video ? 'Video call' : 'Voice call'}, ${outcomeText}, ${timeLabel}`
    );

    const avatar = document.createElement('div');
    avatar.className = 'call-item-avatar';
    const avatarKey = call.caller_phone || call.caller_name || 'Call';
    avatar.style.backgroundColor = getAvatarColor(avatarKey);
    avatar.textContent = getInitials(callerName);
    el.appendChild(avatar);

    const body = document.createElement('div');
    body.className = 'call-item-body';

    const row1 = document.createElement('div');
    row1.className = 'call-item-row-1';

    const nameEl = document.createElement('div');
    nameEl.className = 'call-item-name';
    nameEl.textContent = callerName;
    if (call.is_group) {
      const grpIcon = document.createElement('span');
      grpIcon.className = 'call-item-group-icon';
      grpIcon.title = 'Group call';
      grpIcon.innerHTML = ' 👥';
      nameEl.appendChild(grpIcon);
    }
    if (call.is_preserved) {
      const presBadge = document.createElement('span');
      presBadge.className = 'call-item-pres-badge';
      presBadge.textContent = 'Preserved';
      nameEl.appendChild(presBadge);
    }
    row1.appendChild(nameEl);

    const mediaIcon = document.createElement('span');
    mediaIcon.className = 'call-item-media-icon';
    mediaIcon.innerHTML = _getCallMediaSvg(!!call.is_video, 15);
    mediaIcon.title = call.is_video ? 'Video call' : 'Voice call';
    row1.appendChild(mediaIcon);
    body.appendChild(row1);

    const row2 = document.createElement('div');
    row2.className = 'call-item-row-2';

    const sub = document.createElement('div');
    sub.className = 'call-item-sub';
    const subClass = isMissed ? ' call-icon-missed' : '';
    sub.innerHTML = _getCallDirectionSvg(callType) +
      `<span class="call-item-sub-text${subClass}">${esc(outcomeText)}</span>`;
    row2.appendChild(sub);

    const timeEl = document.createElement('span');
    timeEl.className = 'call-item-time';
    timeEl.textContent = timeLabel;
    row2.appendChild(timeEl);

    body.appendChild(row2);
    el.appendChild(body);

    el.addEventListener('click', () => {
      selectCallItem(call, el);
    });
    el.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        selectCallItem(call, el);
      }
    });

    return el;
  }

  function selectCallItem(call, el) {
    document.querySelectorAll('.call-item').forEach(item => {
      item.classList.remove('active');
    });
    if (el) el.classList.add('active');

    const emptyPane = document.getElementById('empty-pane');
    if (emptyPane) emptyPane.style.display = 'none';

    const chatHeader = document.getElementById('chat-header');
    if (chatHeader) chatHeader.style.display = 'none';

    const pinnedBanner = document.getElementById('pinned-banner');
    if (pinnedBanner) pinnedBanner.style.display = 'none';

    const contextBanner = document.getElementById('context-jump-banner');
    if (contextBanner) contextBanner.style.display = 'none';

    const msgScroll = document.getElementById('message-scroll');
    if (msgScroll) msgScroll.style.display = 'none';

    const scrollBottom = document.getElementById('scroll-to-bottom');
    if (scrollBottom) scrollBottom.classList.remove('visible');

    closeChatInfo();
    closeChatStarred();
    closeMediaGallery();

    const callInspector = document.getElementById('pane-call-inspector');
    if (!callInspector) return;
    callInspector.style.display = 'flex';
    showDetailPane();

    const avatarEl = document.getElementById('call-inspect-avatar');
    if (avatarEl) {
      avatarEl.textContent = getInitials(call.caller_name || 'Call');
      avatarEl.style.backgroundColor = getAvatarColor(call.caller_phone || call.caller_name || 'Call');
    }

    const nameEl = document.getElementById('call-inspect-name');
    if (nameEl) nameEl.textContent = call.caller_name || 'Unknown';

    const phoneEl = document.getElementById('call-inspect-phone');
    if (phoneEl) {
      const formatted = formatPhoneNumber(call.caller_phone || '');
      phoneEl.textContent = formatted ? formatted : (call.caller_phone || '');
    }

    const badgeIcon = document.getElementById('call-inspect-badge-icon');
    const badgeLabel = document.getElementById('call-inspect-badge-label');
    if (badgeIcon && badgeLabel) {
      const typeStr = call.is_group ? 'Group ' : '';
      if (call.is_video) {
        badgeIcon.textContent = '📹';
        badgeLabel.textContent = `${typeStr}Video Call`;
      } else {
        badgeIcon.textContent = call.is_group ? '👥' : '📞';
        badgeLabel.textContent = `${typeStr}Voice Call`;
      }
      if (call.is_preserved) {
        badgeLabel.textContent += ' • Preserved';
      }
    }

    let inspectCallType = call.call_type;
    if (call.duration > 0 && inspectCallType === 'missed') {
      inspectCallType = call.from_me ? 'outgoing' : 'incoming';
    }

    const dirEl = document.getElementById('call-inspect-dir');
    if (dirEl) {
      if (inspectCallType === 'missed') {
        dirEl.innerHTML = '<span class="call-icon-missed call-dir-label">Missed</span>';
      } else if (inspectCallType === 'incoming') {
        dirEl.innerHTML = '<svg class="call-item-arrow call-icon-incoming" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="16 18 22 18 22 12"/><line x1="22" y1="18" x2="11" y2="7"/></svg> <span class="call-icon-incoming call-dir-label">Incoming</span>';
      } else {
        dirEl.innerHTML = '<svg class="call-item-arrow call-icon-outgoing" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="8 6 2 6 2 12"/><line x1="2" y1="6" x2="13" y2="17"/></svg> <span class="call-icon-outgoing call-dir-label">Outgoing</span>';
      }
    }

    const timeEl = document.getElementById('call-inspect-time');
    if (timeEl) {
      const d = new Date(call.timestamp * 1000);
      timeEl.textContent = d.toLocaleString(undefined, {
        weekday: 'short',
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
      });
    }

    const durEl = document.getElementById('call-inspect-duration');
    if (durEl) {
      if (call.duration > 0) {
        durEl.textContent = fmtDurationLong(call.duration);
      } else if (inspectCallType === 'missed') {
        durEl.textContent = call.outcome === 'declined' ? '0s (Declined)' : '0s (Missed)';
      } else if (inspectCallType === 'outgoing') {
        durEl.textContent = call.outcome === 'canceled' ? '0s (Canceled)' : '0s (No answer)';
      } else {
        durEl.textContent = '0s';
      }
    }

    const dataRow = document.getElementById('call-inspect-data-row');
    const dataVal = document.getElementById('call-inspect-data');
    if (dataRow && dataVal) {
      if (call.data_transferred && call.data_transferred > 0) {
        dataRow.style.display = 'flex';
        dataVal.textContent = _formatBytes(call.data_transferred);
      } else {
        dataRow.style.display = 'none';
      }
    }

    const jumpBtn = document.getElementById('call-inspect-jump-btn');
    if (jumpBtn) {
      let matchedChat = null;
      if (call.chat_id) {
        matchedChat = allChats.find(c => String(c.id) === String(call.chat_id) || c.jid === call.chat_id);
      }
      if (!matchedChat && call.caller_phone) {
        const cleanPhone = call.caller_phone.replace(/[^0-9]/g, '');
        if (cleanPhone) {
          matchedChat = allChats.find(c => {
            const cp = (c.phone_number || '').replace(/[^0-9]/g, '');
            if (!cp) return false;
            if (cp === cleanPhone) return true;
            if (cleanPhone.length >= 7 && cp.length >= 7) {
              return cp.endsWith(cleanPhone) || cleanPhone.endsWith(cp);
            }
            return false;
          });
        }
      }

      if (matchedChat) {
        jumpBtn.disabled = false;
        jumpBtn.title = 'Jump to this moment in conversation';
        jumpBtn.onclick = () => {
          jumpFromCallToChat(call, matchedChat);
        };
      } else {
        jumpBtn.disabled = true;
        jumpBtn.title = 'No archived conversation found for this caller';
        jumpBtn.onclick = null;
      }
    }
  }

  let _pendingJumpCallId = null;

  function jumpFromCallToChat(call, matchedChat) {
    if (!matchedChat) return;
    const callInspector = document.getElementById('pane-call-inspector');
    if (callInspector) callInspector.style.display = 'none';
    _pendingJumpCallId = call && call.id ? ('call:' + call.id) : null;
    openChatAtTimestamp(matchedChat.id, call.timestamp_ms ?? (call.timestamp * 1000));
  }

  const chatListScroll = document.getElementById('chat-list');
  if (chatListScroll) {
    chatListScroll.addEventListener('scroll', () => {
      if (currentNavTab === 'calls' && _callsHasMore && !_callsLoading) {
        if (chatListScroll.scrollTop + chatListScroll.clientHeight >= chatListScroll.scrollHeight - 80) {
          loadCallsList(false);
        }
      } else if (currentNavTab === 'starred' && _starredHasMore && !_starredLoading) {
        if (chatListScroll.scrollTop + chatListScroll.clientHeight >= chatListScroll.scrollHeight - 80) {
          loadStarredList(false);
        }
      }
    });
  }
