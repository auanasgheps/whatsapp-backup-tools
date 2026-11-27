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
    s.setAttribute('role', 'status');
    s.setAttribute('aria-label', t('common.loading'));
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
    closeChatMenu();
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
      heading.textContent = t(currentChat.type === 'group' ? 'chat_info.title_group' : 'chat_info.title_contact');
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
        phone.textContent = t('chat.group_conversation');
      }
    }
    if (desc) {
      desc.textContent = '';
      desc.style.display = 'none';
    }
    details.innerHTML = '';

    // Show loading skeleton
    const loadingRow = document.createElement('div');
    loadingRow.className = 'chat-info-loading';
    loadingRow.appendChild(_spinner());
    details.appendChild(loadingRow);

    panel.style.display = 'flex';
    panel.classList.add('open');
    if (closeBtn) closeBtn.focus();

    const params = new URLSearchParams({ chat_id: currentChat.id, chat_type: currentChat.type });
    const chatAtOpen = currentChat;

    // Kick off both requests in parallel
    const sizePromise = fetch(API + '/chat-info/media-size?' + params)
      .then(r => r.ok ? r.json() : { bytes: 0 })
      .catch(() => ({ bytes: 0 }));

    let info;
    try {
      const infoResp = await fetch(API + '/chat-info?' + params);
      info = await infoResp.json();
    } catch (_) {
      if (currentChat === chatAtOpen && panel.classList.contains('open')) {
        details.innerHTML = `<div class="chat-info-row" style="color:var(--text-muted)">${esc(t('chat_info.load_failed'))}</div>`;
      }
      return;
    }

    // Panel may have been closed or chat switched while awaiting
    if (currentChat !== chatAtOpen || !panel.classList.contains('open')) return;

    details.innerHTML = '';

    if (currentChat.type === 'group' && info.members && info.members.length) {
      const partLabel = tp('chat.participants', info.members.length);
      if (phone) {
        phone.textContent = partLabel;
      }
      const subEl = document.getElementById('chat-subtitle');
      if (subEl) {
        let subHtml = esc(partLabel);
        if (currentChat.is_preserved_only) {
          subHtml += ` <span class="chat-preserved-tag" title="${esc(t('sidebar.preserved_badge_title'))}">${esc(t('common.preserved'))}</span>`;
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

    const overviewCard = document.createElement('div');
    overviewCard.className = 'chat-info-card';

    if (currentChat.type === 'contact' && info.number) {
      overviewCard.appendChild(_infoRow(t('chat_info.phone'), '+' + info.number).row);
    }

    if (currentChat.type === 'group' && info.created_ts) {
      overviewCard.appendChild(_infoRow(t('chat_info.group_created'), fmtTs(info.created_ts)).row);
      if (info.creator_number || info.creator_name) {
        const match = info.creator_number ? (info.members || []).find(m => m.number && m.number === info.creator_number) : null;
        const creatorName = (match && match.name) ? match.name : (info.creator_name || (info.creator_number ? '+' + info.creator_number : ''));
        if (creatorName) {
          overviewCard.appendChild(_infoRow(t('chat_info.created_by'), creatorName).row);
        }
      }
    }

    overviewCard.appendChild(_infoRow(t('chat_info.conversation_since'), fmtTs(info.first_ts)).row);

    if (info.first_ts && info.last_ts && info.last_ts !== info.first_ts) {
      const days = Math.round((info.last_ts - info.first_ts) / 86400000);
      const years = Math.floor(days / 365);
      const months = Math.floor((days % 365) / 30);
      let span = '';
      if (years > 0) span += tp('time.years', years);
      if (months > 0) span += (span ? ', ' : '') + tp('time.months', months);
      if (!span) span = tp('time.days', days);
      overviewCard.appendChild(_infoRow(t('calls.duration'), span).row);
    }

    if (currentChat.type === 'contact') {
      const sentReceived = document.createElement('span');
      sentReceived.textContent = t('chat_info.sent_received', { sent: fmtNum(info.sent), received: fmtNum(info.received) });
      overviewCard.appendChild(_infoRow(t('search.messages_section'), sentReceived).row);
    } else {
      overviewCard.appendChild(_infoRow(t('search.messages_section'), fmtNum(info.total)).row);
    }

    if (info.calls_count) {
      overviewCard.appendChild(_infoRow(t('nav.calls'), fmtNum(info.calls_count)).row);
    }

    const ephemDur = Number(info.ephemeral_duration) || 0;
    const ephemVal = document.createElement('span');
    ephemVal.textContent = _formatEphemeralDuration(ephemDur);
    if (ephemDur > 0 && info.has_preservation) {
      ephemVal.title = t('sidebar.disappearing_preserved_note');
    }
    overviewCard.appendChild(_infoRow(t('chat_info.disappearing_messages'), ephemVal).row);

    if (currentChat.type === 'contact' && info.is_business) {
      overviewCard.appendChild(
        _infoRow(t('chat_info.business_account'), info.business_name || t('chat.business_badge')).row
      );
    }

    // Media size row (placed before gallery link button)
    const sizeRowObj = _infoRow(t('chat_info.media_size'), t('chat_info.calculating'));
    overviewCard.appendChild(sizeRowObj.row);
    sizePromise.then(sizeData => {
      if (currentChat === chatAtOpen && panel.classList.contains('open')) {
        sizeRowObj.val.textContent = _formatBytes(sizeData.bytes || 0);
      }
    });

    // Gallery link row (last button/row of the overview section)
    const galleryLink = document.createElement('button');
    galleryLink.className = 'chat-info-link';
    galleryLink.textContent = t('chat_info.open_gallery');
    galleryLink.addEventListener('click', () => { closeChatInfo(); openMediaGallery(); });
    overviewCard.appendChild(_infoRow(t('gallery.title'), galleryLink).row);

    details.appendChild(overviewCard);

    // Group members (Card 2: before Top Senders, with grey card background)
    if (currentChat.type === 'group' && info.members && info.members.length > 0) {
      const SHOW = 10;
      const membersCard = document.createElement('div');
      membersCard.className = 'chat-info-card';
      const membersTitle = document.createElement('div');
      membersTitle.className = 'chat-info-card-title';
      membersTitle.textContent = t('chat_info.members_header', { count: info.members.length });
      membersCard.appendChild(membersTitle);

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
        more.style.marginTop = '6px';
        const remaining = info.members.length - SHOW;
        more.textContent = t('chat_info.more_members', { count: remaining });
        more.addEventListener('click', () => {
          info.members.slice(SHOW).forEach(m => membersWrap.insertBefore(renderMember(m), more));
          more.remove();
        });
        membersWrap.appendChild(more);
      }
      membersCard.appendChild(membersWrap);
      details.appendChild(membersCard);
    }

    // Top senders (Card 3: after Members, with grey card background)
    if (currentChat.type === 'group' && info.top_senders && info.top_senders.length > 0) {
      const sendersCard = document.createElement('div');
      sendersCard.className = 'chat-info-card';
      const sendersTitle = document.createElement('div');
      sendersTitle.className = 'chat-info-card-title';
      sendersTitle.textContent = t('chat_info.top_senders');
      sendersCard.appendChild(sendersTitle);

      const sendersWrap = document.createElement('div');
      sendersWrap.className = 'chat-info-senders';
      info.top_senders.forEach(s => {
        const el = document.createElement('div');
        el.className = 'chat-info-member';
        el.textContent = (s.name || '?') + ': ' + s.count.toLocaleString();
        sendersWrap.appendChild(el);
      });
      sendersCard.appendChild(sendersWrap);
      details.appendChild(sendersCard);
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

  // Dismiss open drawers and menus when clicking into the conversation or chat pane outside
  document.addEventListener('pointerdown', e => {
    const menuContainer = document.getElementById('chat-menu-container');
    if (menuContainer && !menuContainer.contains(e.target)) {
      closeChatMenu();
    }

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
