// ---- message details popup ------------------------------------------------

  const msgDetailsPopup = document.getElementById('msg-details-popup');

  function hideMsgDetails() {
    msgDetailsPopup.classList.remove('visible');
  }

  document.addEventListener('click', e => {
    if (!msgDetailsPopup.contains(e.target)) hideMsgDetails();
  });


  function fmtReceipt(ts) {
    if (!ts) return '—';
    return new Date(ts).toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
    });
  }

  // Recent iOS messages record that a message was read (aggregate status) but no
  // longer store the precise read time. Show that state instead of a blank dash.
  function fmtRead(entry) {
    if (entry.read_ts) return fmtReceipt(entry.read_ts);
    if (entry.read_known) return '<span class="msg-details-na">Read (time not stored)</span>';
    return '—';
  }

  function showCallDetails(msg, anchorEl) {
    const c = msg.call || {};
    const kind = (c.is_group ? 'Group ' : '') + (c.video ? 'Video call' : 'Voice call');
    const dir = c.from_me ? 'Outgoing' : 'Incoming';
    const outcomeMap = {
      answered: 'Answered',
      missed: t('calls.missed'),
      declined: t('calls.declined'),
      canceled: t('calls.canceled'),
      unanswered: t('calls.no_answer')
    };
    const statusLabel = outcomeMap[c.outcome] || t('calls.no_answer');
    const durText = (c.duration_sec && c.duration_sec > 0) ? fmtDurationLong(c.duration_sec) : '—';

    let html =
      `<div class="msg-details-row"><span class="msg-details-label">${esc(t('details.outcome'))}</span><span class="msg-details-value">${esc(kind)}</span></div>` +
      `<div class="msg-details-row"><span class="msg-details-label">${esc(t('calls.direction'))}</span><span class="msg-details-value">${esc(dir)}</span></div>` +
      `<div class="msg-details-row"><span class="msg-details-label">${esc(t('details.outcome'))}</span><span class="msg-details-value">${esc(statusLabel)}</span></div>` +
      `<div class="msg-details-row"><span class="msg-details-label">${esc(t('calls.duration'))}</span><span class="msg-details-value">${esc(durText)}</span></div>` +
      `<div class="msg-details-row"><span class="msg-details-label">${esc(t('details.time'))}</span><span class="msg-details-value">${esc(fmtTime(msg.timestamp_ms))}</span></div>`;

    if (c.is_group && c.participants > 1) {
      html += `<div class="msg-details-row"><span class="msg-details-label">${esc(t('details.participants'))}</span><span class="msg-details-value">${esc(String(c.participants))}</span></div>`;
    }
    if (msg.is_preserved) {
      html += `<div class="msg-details-row"><span class="msg-details-label">${esc(t('details.source'))}</span><span class="msg-details-value">${esc(t('details.preserved_from_backup'))}</span></div>`;
    }

    msgDetailsPopup.innerHTML = html;
    positionPopup(anchorEl);
    msgDetailsPopup.classList.add('visible');
  }

  async function showMsgDetails(msgId, anchorEl, archivePath, presInfo, editInfo) {
    let html = '';

    if (presInfo) {
      const reasonText = presInfo.reason === 'revoked_by_sender'
        ? t('details.reason_msg_deleted')
        : (presInfo.reason === 'chat_deleted' ? t('details.reason_chat_deleted') : t('details.reason_msg_deleted'));
      const presTs = typeof presInfo.at === 'number' ? presInfo.at * 1000 : Date.parse(presInfo.at);
      const atText = !isNaN(presTs) ? fmtTime(presTs) : (presInfo.at || '');
      const platformText = presInfo.platform ? (' • ' + esc(presInfo.platform)) : '';
      const backupPrefix = atText ? `${t('details.preserved_from_backup')} (${atText})` : t('details.preserved_from_backup');
      html += `<div class="msg-details-row"><span class="msg-details-label">${esc(t('details.preserved_label'))}</span><span class="msg-details-value">${backupPrefix} • ${reasonText}${platformText}</span></div>`;
    }

    if (editInfo && (editInfo.original_text || editInfo.edited_ts)) {
      html += '<div class="msg-details-section msg-edit-history">';
      if (editInfo.original_text) {
        html += `<div class="msg-details-row"><span class="msg-details-label">${esc(t('details.original_pre_edit'))}</span><span class="msg-details-value msg-pre-edit-text">${esc(resolveMentionsPlainText(editInfo.original_text, editInfo.mentions, Boolean(currentChat && currentChat.type === 'group')))}</span></div>`;
      }
      if (editInfo.edited_ts) {
        html += `<div class="msg-details-row"><span class="msg-details-label">${esc(t('details.edited_on'))}</span><span class="msg-details-value">${fmtTime(editInfo.edited_ts)}</span></div>`;
      }
      html += '</div>';
    }

    if (msgId !== null) {
      msgDetailsPopup.innerHTML = `<span style="color:var(--text-muted)">${esc(t('common.loading'))}</span>`;
      positionPopup(anchorEl);
      msgDetailsPopup.classList.add('visible');

      let data;
      try {
        const res = await fetch(API + '/message_receipts/' + msgId);
        data = await res.json();
      } catch (_) {
        data = { available: false };
      }

      if (!data.available || !(data.members || []).length) {
        if (!html) {
          html = `<span class="msg-details-na">${esc(t('details.no_receipt_data'))}</span>`;
        }
      } else if (data.members.length === 1) {
        html +=
          `<div class="msg-details-row"><span class="msg-details-label">${esc(t('details.delivered'))}</span><span class="msg-details-value">${fmtReceipt(data.delivered_ts)}</span></div>` +
          `<div class="msg-details-row"><span class="msg-details-label">${esc(t('details.read'))}</span><span class="msg-details-value">${fmtRead(data)}</span></div>`;
      } else {
        let rows = '';
        for (const m of data.members) {
          rows += `<tr><td>${esc(m.name || m.jid || '?')}</td><td>${fmtReceipt(m.delivered_ts)}</td><td>${fmtRead(m)}</td></tr>`;
        }
        html += `<table class="msg-details-table"><thead><tr><th>${esc(t('details.participants'))}</th><th>${esc(t('details.delivered'))}</th><th>${esc(t('details.read'))}</th></tr></thead><tbody>${rows}</tbody></table>`;
      }
    }

    if (archivePath) {
      html += `<div class="msg-details-copy-row"><button class="msg-details-copy-btn" data-path="${esc(archivePath)}">${esc(t('common.copy_path'))}</button></div>`;
    }

    msgDetailsPopup.innerHTML = html;
    msgDetailsPopup.querySelector('.msg-details-copy-btn')?.addEventListener('click', e => {
      e.stopPropagation();
      const btn = e.currentTarget;
      const sep = outputRoot.includes('\\') ? '\\' : '/';
      const fullPath = outputRoot.replace(/[/\\]+$/, '') + sep + btn.dataset.path.replace(/\//g, sep);
      navigator.clipboard.writeText(fullPath).then(() => {
        btn.textContent = t('common.copied');
        setTimeout(() => { btn.textContent = t('common.copy_path'); }, 1500);
      });
    });

    positionPopup(anchorEl);
    msgDetailsPopup.classList.add('visible');
  }

  function positionPopup(anchorEl) {
    const rect = anchorEl.getBoundingClientRect();
    const pane = document.getElementById('chat-pane').getBoundingClientRect();
    const popupW = msgDetailsPopup.offsetWidth || 200;
    let left = rect.right - popupW;
    if (left < pane.left + 4) left = pane.left + 4;
    if (left + popupW > pane.right - 4) left = pane.right - popupW - 4;
    let top = rect.bottom + 4;
    if (top + msgDetailsPopup.offsetHeight > window.innerHeight - 8) {
      top = rect.top - msgDetailsPopup.offsetHeight - 4;
    }
    msgDetailsPopup.style.left = left + 'px';
    msgDetailsPopup.style.top = top + 'px';
  }

  async function showReactionDetails(msgId, anchorEl) {
    msgDetailsPopup.innerHTML = '<span style="color:var(--text-muted)">Loading…</span>';
    positionPopup(anchorEl);
    msgDetailsPopup.classList.add('visible');

    let data;
    try {
      const res = await fetch(API + '/reaction_details/' + msgId);
      data = await res.json();
    } catch (_) {
      data = { available: false };
    }

    let html;
    if (!data.available || !data.reactors || !data.reactors.length) {
      html = '<span class="msg-details-na">No reaction data available.</span>';
    } else {
      const label = data.total === 1 ? '1 reaction' : `${data.total} reactions`;
      html = `<div class="rx-details-total">${label}</div>`;
      for (const r of data.reactors) {
        const name = r.from_me ? 'You' : esc(r.name || '?');
        html += `<div class="msg-details-row"><span class="msg-details-label">${esc(r.emoji)}</span><span class="msg-details-value">${name}</span></div>`;
      }
    }

    msgDetailsPopup.innerHTML = html;
    positionPopup(anchorEl);
    msgDetailsPopup.classList.add('visible');
  }

  async function showPollDetails(msgId, anchorEl) {
    msgDetailsPopup.innerHTML = '<span style="color:var(--text-muted)">Loading…</span>';
    positionPopup(anchorEl);
    msgDetailsPopup.classList.add('visible');

    let data;
    try {
      const res = await fetch(API + '/poll_details/' + msgId);
      data = await res.json();
    } catch (_) {
      data = { available: false };
    }

    let html;
    if (!data.available || !data.options) {
      html = '<span class="msg-details-na">No poll vote data available.</span>';
    } else {
      const totalVoters = data.total_voters || 0;
      const label = totalVoters === 1 ? '1 voter' : `${totalVoters} voters`;
      html = `<div class="rx-details-total" style="margin-bottom:8px;font-weight:600">Poll votes · ${label}</div>`;

      const getVoteCount = opt =>
        opt.vote_count !== undefined ? opt.vote_count : (opt.voters ? opt.voters.length : 0);

      let maxVotes = 0;
      for (const opt of data.options) {
        const count = getVoteCount(opt);
        if (count > maxVotes) maxVotes = count;
      }

      const sortedOptions = [...data.options].sort((a, b) => getVoteCount(b) - getVoteCount(a));

      let hasAnyVotes = false;
      for (const opt of sortedOptions) {
        const count = getVoteCount(opt);
        if (!count && (!opt.voters || !opt.voters.length)) continue;
        hasAnyVotes = true;
        const isWinner = maxVotes > 0 && count === maxVotes;
        const star = isWinner
          ? '<span class="msg-poll-winner-star" title="Winning option" aria-label="Winner">★</span>'
          : '';
        html += `<div class="msg-poll-details-option-hdr"><span>${esc(opt.name)}${star ? ' ' + star : ''}</span><span class="msg-poll-details-option-count">(${count})</span></div>`;
        if (opt.voters && opt.voters.length) {
          for (const v of opt.voters) {
            const name = esc(v.name || '?');
            const timeStr = v.timestamp_ms ? fmtTime(v.timestamp_ms) : '';
            html += `<div class="msg-details-row"><span class="msg-details-label">${name}</span><span class="msg-details-value">${timeStr}</span></div>`;
          }
        }
      }
      if (!hasAnyVotes) {
        html += '<span class="msg-details-na">No votes recorded yet.</span>';
      }
    }

    msgDetailsPopup.innerHTML = html;
    positionPopup(anchorEl);
    msgDetailsPopup.classList.add('visible');
  }
