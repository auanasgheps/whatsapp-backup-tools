// ---- chat info panel -------------------------------------------------------

  function _formatBytes(bytes) {
    if (bytes >= 1073741824) return (bytes / 1073741824).toFixed(1) + ' GB';
    if (bytes >= 1048576)    return (bytes / 1048576).toFixed(1) + ' MB';
    if (bytes >= 1024)       return (bytes / 1024).toFixed(1) + ' KB';
    return bytes + ' B';
  }

  function _infoRow(label, valueNode) {
    const row = document.createElement('div');
    row.className = 'chat-info-row';
    const lbl = document.createElement('span');
    lbl.className = 'chat-info-label';
    lbl.textContent = label;
    row.appendChild(lbl);
    const val = document.createElement('span');
    val.className = 'chat-info-value';
    if (typeof valueNode === 'string') {
      val.textContent = valueNode;
    } else {
      val.appendChild(valueNode);
    }
    row.appendChild(val);
    return { row, val };
  }

  function _spinner() {
    const s = document.createElement('span');
    s.className = 'chat-info-spinner';
    return s;
  }

  function closeChatInfo() {
    const panel = document.getElementById('chat-info-panel');
    if (panel) {
      panel.classList.remove('open');
      panel.style.display = 'none';
    }
    const desc = document.getElementById('chat-info-desc');
    if (desc) {
      desc.textContent = '';
      desc.style.display = 'none';
    }
  }

  async function openChatInfo() {
    if (!currentChat) return;
    closeMediaGallery();
    closeChatStarred();
    const panel = document.getElementById('chat-info-panel');
    const heading = document.getElementById('chat-info-header-heading');
    const avatar = document.getElementById('chat-info-avatar');
    const title = document.getElementById('chat-info-title');
    const phone = document.getElementById('chat-info-phone');
    const desc  = document.getElementById('chat-info-desc');
    const details = document.getElementById('chat-info-details');
    const closeBtn = document.getElementById('chat-info-close');
    if (!panel || !details) return;

    if (heading) {
      heading.textContent = currentChat.type === 'group' ? 'Group Info' : 'Contact Info';
    }
    if (avatar) {
      const avatarKey = currentChat.phone_number || currentChat.id || currentChat.display_name;
      avatar.style.backgroundColor = getAvatarColor(avatarKey);
      avatar.textContent = getInitials(currentChat.display_name);
    }
    if (title) {
      title.textContent = currentChat.display_name || '';
    }
    if (phone) {
      if (currentChat.type === 'contact') {
        const fmtPhone = formatPhoneNumber(currentChat.phone_number || currentChat.id);
        phone.textContent = fmtPhone ? fmtPhone : '';
      } else {
        phone.textContent = 'Group conversation';
      }
    }
    if (desc) {
      desc.textContent = '';
      desc.style.display = 'none';
    }
    details.innerHTML = '';

    // Show loading skeleton
    const loadingRow = document.createElement('div');
    loadingRow.className = 'chat-info-row';
    loadingRow.appendChild(_spinner());
    details.appendChild(loadingRow);

    panel.style.display = 'flex';
    panel.classList.add('open');
    if (closeBtn) closeBtn.focus();

    const params = new URLSearchParams({ chat_id: currentChat.id, chat_type: currentChat.type });
    const chatAtOpen = currentChat;

    // Kick off both requests in parallel
    const sizePromise = fetch('/api/chat-info/media-size?' + params)
      .then(r => r.ok ? r.json() : { bytes: 0 })
      .catch(() => ({ bytes: 0 }));

    let info;
    try {
      const infoResp = await fetch('/api/chat-info?' + params);
      info = await infoResp.json();
    } catch (_) {
      if (currentChat === chatAtOpen && panel.classList.contains('open')) {
        details.innerHTML = '<div class="chat-info-row" style="color:var(--text-muted)">Could not load chat info.</div>';
      }
      return;
    }

    // Panel may have been closed or chat switched while awaiting
    if (currentChat !== chatAtOpen || !panel.classList.contains('open')) return;

    details.innerHTML = '';

    if (currentChat.type === 'group' && info.members && info.members.length) {
      if (phone) {
        phone.textContent = `${info.members.length} participants`;
      }
      const subEl = document.getElementById('chat-subtitle');
      if (subEl) {
        let subHtml = `${info.members.length} participants`;
        if (currentChat.is_preserved_only) {
          subHtml += ' <span class="chat-preserved-tag" title="Preserved from backup • Chat deleted on device">Preserved</span>';
        }
        subEl.innerHTML = subHtml;
      }
    }

    if (currentChat.type === 'group' && info.description && desc) {
      desc.textContent = info.description;
      desc.style.display = 'block';
    }

    const fmtTs = ts => ts ? new Date(ts).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : '—';
    const fmtNum = n => (n === null || n === undefined || n < 0) ? '—' : n.toLocaleString();

    if (currentChat.type === 'contact' && info.number) {
      details.appendChild(_infoRow('Phone', '+' + info.number).row);
    }

    if (currentChat.type === 'group' && info.created_ts) {
      details.appendChild(_infoRow('Group created', fmtTs(info.created_ts)).row);
      if (info.creator_number || info.creator_name) {
        const match = info.creator_number ? (info.members || []).find(m => m.number && m.number === info.creator_number) : null;
        const creatorName = (match && match.name) ? match.name : (info.creator_name || (info.creator_number ? '+' + info.creator_number : ''));
        if (creatorName) {
          details.appendChild(_infoRow('Created by', creatorName).row);
        }
      }
    }

    details.appendChild(_infoRow('Conversation since', fmtTs(info.first_ts)).row);

    if (info.first_ts && info.last_ts && info.last_ts !== info.first_ts) {
      const days = Math.round((info.last_ts - info.first_ts) / 86400000);
      const years = Math.floor(days / 365);
      const months = Math.floor((days % 365) / 30);
      let span = '';
      if (years > 0) span += years + (years === 1 ? ' year' : ' years');
      if (months > 0) span += (span ? ', ' : '') + months + (months === 1 ? ' month' : ' months');
      if (!span) span = days + (days === 1 ? ' day' : ' days');
      details.appendChild(_infoRow('Duration', span).row);
    }

    if (currentChat.type === 'contact') {
      const sentReceived = document.createElement('span');
      sentReceived.textContent = fmtNum(info.sent) + ' sent · ' + fmtNum(info.received) + ' received';
      details.appendChild(_infoRow('Messages', sentReceived).row);
    } else {
      details.appendChild(_infoRow('Total messages', fmtNum(info.total)).row);
    }

    if (info.calls_count) {
      details.appendChild(_infoRow('Calls', fmtNum(info.calls_count)).row);
    }

    // Gallery link row
    const galleryLink = document.createElement('button');
    galleryLink.className = 'chat-info-link';
    galleryLink.textContent = 'Open Media Gallery →';
    galleryLink.addEventListener('click', () => { closeChatInfo(); openMediaGallery(); });
    details.appendChild(_infoRow('Media', galleryLink).row);

    // Media size row
    const sizeRowObj = _infoRow('Media size', 'Calculating…');
    details.appendChild(sizeRowObj.row);
    sizePromise.then(sizeData => {
      if (currentChat === chatAtOpen && panel.classList.contains('open')) {
        sizeRowObj.val.textContent = _formatBytes(sizeData.bytes || 0);
      }
    });

    // Group members
    if (currentChat.type === 'group' && info.members && info.members.length > 0) {
      const SHOW = 10;
      const membersWrap = document.createElement('div');
      membersWrap.className = 'chat-info-members';
      const renderMember = m => {
        const el = document.createElement('div');
        el.className = 'chat-info-member';
        const displayName = m.name || (m.number ? '+' + m.number : '?');
        const numSuffix = m.number && ('+' + m.number) !== displayName ? ' (+' + m.number + ')' : '';
        el.textContent = displayName + numSuffix;
        return el;
      };
      info.members.slice(0, SHOW).forEach(m => membersWrap.appendChild(renderMember(m)));
      if (info.members.length > SHOW) {
        const more = document.createElement('button');
        more.className = 'chat-info-link';
        more.style.marginTop = '4px';
        const remaining = info.members.length - SHOW;
        more.textContent = '+ ' + remaining + ' more…';
        more.addEventListener('click', () => {
          info.members.slice(SHOW).forEach(m => membersWrap.insertBefore(renderMember(m), more));
          more.remove();
        });
        membersWrap.appendChild(more);
      }
      details.appendChild(_infoRow('Members (' + info.members.length + ')', membersWrap).row);
    }

    // Top senders
    if (currentChat.type === 'group' && info.top_senders && info.top_senders.length > 0) {
      const sendersWrap = document.createElement('div');
      info.top_senders.forEach(s => {
        const el = document.createElement('div');
        el.className = 'chat-info-member';
        el.textContent = (s.name || '?') + ': ' + s.count.toLocaleString();
        sendersWrap.appendChild(el);
      });
      details.appendChild(_infoRow('Top senders', sendersWrap).row);
    }
  }

  function toggleChatInfo() {
    const panel = document.getElementById('chat-info-panel');
    if (panel && panel.classList.contains('open')) {
      closeChatInfo();
    } else {
      openChatInfo();
    }
  }

  document.getElementById('chat-info-btn').addEventListener('click', toggleChatInfo);
  const headerInfo = document.getElementById('chat-header-info');
  if (headerInfo) {
    headerInfo.addEventListener('click', toggleChatInfo);
    headerInfo.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        toggleChatInfo();
      }
    });
  }
  document.getElementById('chat-info-close').addEventListener('click', closeChatInfo);

  // Dismiss open drawers when clicking into the conversation or chat pane outside the drawer
  document.addEventListener('pointerdown', e => {
    const infoDrawer = document.getElementById('chat-info-panel');
    const starredDrawer = document.getElementById('chat-starred-drawer');

    if (infoDrawer && infoDrawer.classList.contains('open')) {
      const infoBtn = document.getElementById('chat-info-btn');
      const headerInfo = document.getElementById('chat-header-info');
      const isTrigger = (infoBtn && infoBtn.contains(e.target)) || (headerInfo && headerInfo.contains(e.target));
      if (!infoDrawer.contains(e.target) && !isTrigger) {
        closeChatInfo();
      }
    }

    if (starredDrawer && starredDrawer.classList.contains('open')) {
      const starredBtn = document.getElementById('chat-starred-btn');
      const isTrigger = starredBtn && starredBtn.contains(e.target);
      if (!starredDrawer.contains(e.target) && !isTrigger) {
        closeChatStarred();
      }
    }
  });
