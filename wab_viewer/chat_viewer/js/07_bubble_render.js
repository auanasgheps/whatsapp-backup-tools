function renderBubble(unit) {
    if (unit.type === 'group') {
      const group = unit.members;
      const msg = group[0];
      const row = document.createElement('div');
      row.className = 'msg-row ' + (msg.from_me ? 'sent' : 'recv');
      row.dataset.msgId = String(msg.msg_id);
      row.dataset.ts = String(msg.timestamp_ms || 0);
      row.dataset.groupIds = group.map(m => String(m.msg_id)).join(' ');

      const bubble = document.createElement('div');
      bubble.className = 'msg-bubble has-media';

      if (!msg.from_me && msg.sender) {
        bubble.appendChild(createSenderEl(msg.sender));
      }

      bubble.appendChild(renderMediaGrid(group));

      const captionMsg = group.find(m => m.text_body && m.text_body.trim());
      if (captionMsg) {
        const cap = document.createElement('div');
        cap.className = 'msg-caption';
        renderCollapsibleMessageText(cap, captionMsg.text_body, captionMsg.msg_id, captionMsg.mentions);
        bubble.appendChild(cap);
      }

      const meta = document.createElement('div');
      meta.className = 'msg-meta';
      if (msg.from_me) {
        const badge = document.createElement('span');
        badge.className = 'direction-badge sent';
        badge.textContent = 'You';
        meta.appendChild(badge);
      }
      const isGroupEdited = unit.members.some(m => m.is_edited);
      if (isGroupEdited) {
        const editedSpan = document.createElement('span');
        editedSpan.className = 'msg-edited';
        editedSpan.textContent = 'Edited';
        const editedMember = unit.members.find(m => m.edited_ts || m.original_text_body);
        if (editedMember && editedMember.edited_ts) {
          editedSpan.title = 'Edited: ' + fmtTime(editedMember.edited_ts);
        }
        editedSpan.addEventListener('click', e => {
          e.stopPropagation();
          showMsgDetails(
            null,
            editedSpan,
            null,
            null,
            editedMember ? { original_text: editedMember.original_text_body || null, edited_ts: editedMember.edited_ts || null, mentions: editedMember.mentions || null } : null
          );
        });
        meta.appendChild(editedSpan);
      }
      const isGroupPinned = unit.members.some(m => m.is_pinned) || Boolean(
        msg.is_pinned || (_chatPins && _chatPins.some(p => String(p.id) === String(msg.msg_id)))
      );
      if (isGroupPinned) {
        meta.appendChild(createMetaPinIcon());
      }
      const isGroupStarred = unit.members.some(m => m.is_starred) || Boolean(msg.is_starred);
      if (isGroupStarred) {
        meta.appendChild(createMetaStarIcon());
      }
      meta.appendChild(document.createTextNode(fmtTime(msg.timestamp_ms)));
      bubble.appendChild(meta);

      row.appendChild(bubble);
      return row;
    }

    const msg = unit.msg;
    if (currentChat && currentChat.type === 'contact' && msg.media_type === 'service') {
      if (
        !msg.text_body ||
        !msg.text_body.trim() ||
        msg.text_body.includes('subject') ||
        msg.text_body.includes('description') ||
        msg.text_body.includes('deleted')
      ) {
        msg.media_type = 'deleted';
        msg.text_body = msg.from_me ? 'You deleted this message' : 'This message was deleted';
      }
    }
    if (msg.media_type === 'service') {
      if (!msg.text_body || !msg.text_body.trim()) {
        return document.createDocumentFragment();
      }
      const row = document.createElement('div');
      row.className = 'msg-row service';
      row.dataset.msgId = String(msg.msg_id);
      row.dataset.ts = String(msg.timestamp_ms || 0);

      const bubble = document.createElement('div');
      bubble.className = 'msg-service-bubble';
      bubble.textContent = msg.text_body;
      if (msg.timestamp_ms) {
        bubble.title = fmtTime(msg.timestamp_ms);
      }

      row.appendChild(bubble);
      return row;
    }

    const row = document.createElement('div');
    row.className = 'msg-row ' + (msg.from_me ? 'sent' : 'recv');
    row.dataset.msgId = String(msg.msg_id);
    row.dataset.ts = String(msg.timestamp_ms || 0);

    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble';

    if (msg.sender && !msg.from_me && currentChat && currentChat.type === 'group') {
      bubble.appendChild(createSenderEl(msg.sender));
    }

    if ((msg.quoted_text || msg.quoted_stanza_id || msg.quoted_msg_id) && msg.media_type !== 'deleted') {
      const quote = document.createElement('div');
      const qs = msg.quoted_sender;
      const qColorCls = (currentChat && currentChat.type === 'group') ? getSenderColorClass(qs) : '';
      quote.className = qColorCls ? ('msg-quote ' + qColorCls) : 'msg-quote';
      const qContent = document.createElement('div');
      qContent.className = 'msg-quote-content';
      const qSender = document.createElement('div');
      qSender.className = 'msg-quote-sender';
      qSender.textContent = qs || (currentChat && currentChat.type === 'contact' ? currentChat.display_name : '');
      const qText = document.createElement('div');
      qText.className = 'msg-quote-text';
      qText.textContent = resolveMentionsPlainText(msg.quoted_text, msg.mentions) || (msg.quoted_msg_id ? 'Quoted message' : '');
      qContent.appendChild(qSender);
      qContent.appendChild(qText);
      quote.appendChild(qContent);

      if (msg.quoted_has_thumb && (msg.quoted_msg_id || msg.msg_id)) {
        const qThumb = document.createElement('img');
        qThumb.className = 'msg-quote-thumb';
        qThumb.alt = '';
        qThumb.loading = 'lazy';
        qThumb.decoding = 'async';
        const primaryId = msg.quoted_msg_id || msg.msg_id;
        const fallbackId = (msg.quoted_msg_id && msg.msg_id && msg.quoted_msg_id !== msg.msg_id) ? msg.msg_id : null;
        qThumb.src = API + '/thumbnail/' + primaryId;
        qThumb.onerror = () => {
          if (fallbackId && qThumb.src.indexOf('/' + fallbackId) === -1) {
            qThumb.src = API + '/thumbnail/' + fallbackId;
          } else {
            qThumb.remove();
          }
        };
        quote.appendChild(qThumb);
      }

      if (msg.quoted_msg_id || msg.quoted_ts) {
        quote.style.cursor = 'pointer';
        quote.title = 'Jump to original message';
        quote.addEventListener('click', async (e) => {
          e.stopPropagation();
          if (msg.quoted_msg_id) {
            expandMessageRowById(msg.quoted_msg_id, rowForMsgId(msg.quoted_msg_id));
            await jumpToMessage({ msg_id: msg.quoted_msg_id, timestamp_ms: msg.quoted_ts }, 'default');
          } else {
            const targetNear = nearestMsgToTs(msg.quoted_ts);
            if (targetNear) {
              expandMessageRowById(targetNear.msg_id, rowForMsgId(targetNear.msg_id));
            }
            const row = await jumpToTimestamp(msg.quoted_ts, null, true, 'default');
            if (row && row.dataset.msgId) {
              expandMessageRowById(row.dataset.msgId, row);
            }
          }
        });
      }
      bubble.appendChild(quote);
    }

    const meta = document.createElement('div');
    meta.className = 'msg-meta';
    if (msg.from_me) {
      const badge = document.createElement('span');
      badge.className = 'direction-badge sent';
      badge.textContent = 'You';
      meta.appendChild(badge);
    }
    const isCallEvent = msg.media_type === 'call' && !!msg.call;
    const showInfo = isCallEvent ||
                     (msg.from_me && !msg.is_preserved && !!msg.msg_id && msg.media_type !== 'deleted') ||
                     (!!msg.archive_path && msg.media_type !== 'deleted') ||
                     !!msg.is_preserved ||
                     (msg.is_edited && !!msg.original_text_body);
    if (showInfo) {
      const infoBtn = document.createElement('button');
      infoBtn.type = 'button';
      infoBtn.className = 'msg-info-btn';
      infoBtn.title = isCallEvent ? 'Call details' : 'Message details';
      infoBtn.setAttribute('aria-label', isCallEvent ? 'Call details' : 'Message details');
      infoBtn.textContent = 'ⓘ';
      infoBtn.addEventListener('click', e => {
        e.stopPropagation();
        if (isCallEvent) {
          showCallDetails(msg, infoBtn);
          return;
        }
        showMsgDetails(
          (msg.from_me && !msg.is_preserved) ? msg.msg_id : null,
          infoBtn,
          msg.archive_path || null,
          msg.is_preserved ? { reason: msg.preservation_reason, at: msg.preserved_at, platform: msg.source_platform || null } : null,
          msg.is_edited ? { original_text: msg.original_text_body || null, edited_ts: msg.edited_ts || null, mentions: msg.mentions || null } : null
        );
      });
      meta.appendChild(infoBtn);
    }
    if (msg.is_preserved) {
      const presSpan = document.createElement('span');
      presSpan.className = 'msg-preserved';
      presSpan.textContent = 'Preserved';
      const reasonLabel = msg.preservation_reason === 'revoked_by_sender'
        ? 'Revoked by sender'
        : (msg.preservation_reason === 'chat_deleted'
          ? 'Chat deleted on device'
          : 'Deleted on device');
      const presTs = typeof msg.preserved_at === 'number' ? msg.preserved_at * 1000 : Date.parse(msg.preserved_at);
      const presDate = !isNaN(presTs) ? fmtTime(presTs) : (msg.preserved_at || '');
      const backupPrefix = presDate ? `Preserved from backup (${presDate})` : 'Preserved from backup';
      presSpan.title = `${backupPrefix} • ${reasonLabel}`;
      meta.appendChild(presSpan);
    }
    if (msg.is_edited && msg.media_type !== 'deleted') {
      const editedSpan = document.createElement('span');
      editedSpan.className = 'msg-edited';
      editedSpan.textContent = 'Edited';
      if (msg.edited_ts) {
        editedSpan.title = 'Edited: ' + fmtTime(msg.edited_ts);
      }
      editedSpan.addEventListener('click', e => {
        e.stopPropagation();
        showMsgDetails(
          (msg.from_me && !msg.is_preserved) ? msg.msg_id : null,
          editedSpan,
          msg.archive_path || null,
          msg.is_preserved ? { reason: msg.preservation_reason, at: msg.preserved_at, platform: msg.source_platform || null } : null,
          { original_text: msg.original_text_body || null, edited_ts: msg.edited_ts || null, mentions: msg.mentions || null }
        );
      });
      meta.appendChild(editedSpan);
    }
    const isPinned = Boolean(
      msg.is_pinned || (_chatPins && _chatPins.some(p => String(p.id) === String(msg.msg_id)))
    );
    if (isPinned) {
      meta.appendChild(createMetaPinIcon());
    }
    if (msg.is_starred) {
      meta.appendChild(createMetaStarIcon());
    }
    meta.appendChild(document.createTextNode(fmtTime(msg.timestamp_ms)));
    if (msg.media_type === 'deleted') {
      bubble.classList.add('deleted');
      const delEl = document.createElement('div');
      delEl.className = 'msg-deleted-content';
      const icon = document.createElement('span');
      icon.className = 'deleted-icon';
      icon.textContent = '⃠';
      const txt = document.createElement('span');
      txt.className = 'deleted-text';
      txt.textContent = msg.text_body || 'This message was deleted';
      delEl.appendChild(icon);
      delEl.appendChild(document.createTextNode(' '));
      delEl.appendChild(txt);
      bubble.appendChild(delEl);
    } else if (msg.is_view_once && !msg.archive_path) {
      bubble.classList.add('view-once-opened');
      const voEl = document.createElement('div');
      voEl.className = 'msg-view-once-pill';
      const voIcon = document.createElement('span');
      voIcon.className = 'vo-icon';
      voIcon.textContent = '①';
      const voLabel = document.createElement('span');
      voLabel.className = 'vo-label';
      const typeLabel = msg.media_type === 'video' ? 'Video' : (msg.media_type === 'audio' ? 'Voice message' : 'Photo');
      voLabel.textContent = typeLabel + ' · Opened';
      voEl.appendChild(voIcon);
      voEl.appendChild(voLabel);
      bubble.appendChild(voEl);
    } else if (isCallEvent) {
      bubble.classList.add('call-event');
      bubble.setAttribute('role', 'article');
      const callMeta = msg.call;
      const callLabelPrefix = callMeta.is_group ? 'Group ' : '';
      const callKindLabel = callLabelPrefix + (callMeta.video ? 'video call' : 'voice call');
      const outcomeLabel = _formatCallOutcomeText(
        callMeta.call_type || (msg.from_me ? 'outgoing' : 'incoming'),
        callMeta.outcome || 'unanswered',
        callMeta.duration_sec || 0
      );
      bubble.setAttribute('aria-label', `${callKindLabel}, ${outcomeLabel}, ${fmtTime(msg.timestamp_ms)}`);
      bubble.appendChild(renderCallCard(msg));
    } else if (msg.media_type === 'poll') {
      bubble.appendChild(renderPoll(msg));
    } else if (msg.media_type === 'location' || msg.media_type === 'live_location') {
      bubble.classList.add('has-media');
      bubble.appendChild(renderLocation(msg));
    } else if (msg.media_type === 'text' || !msg.archive_path) {
      if (!msg.archive_path && msg.media_type !== 'text' && msg.media_type !== 'link') {
        bubble.classList.add('has-media');
        if (msg.media_type === 'audio' && isVoiceNote(msg) && (msg.waveform || msg.audio_waveform)) {
          bubble.appendChild(renderVoiceNoteBubble(msg));
        } else {
          bubble.appendChild(renderMissingMedia(msg));
        }
        if (msg.text_body && msg.media_type !== 'vcard') {
          const cap = document.createElement('div');
          cap.className = 'msg-caption';
          renderCollapsibleMessageText(cap, msg.text_body, msg.msg_id, msg.mentions);
          bubble.appendChild(cap);
        }
      } else {
        const txt = document.createElement('div');
        txt.className = 'msg-text';
        renderCollapsibleMessageText(txt, msg.text_body, msg.msg_id, msg.mentions);
        bubble.appendChild(txt);
        if (msg.link_title || msg.link_url || msg.link_desc) {
          bubble.appendChild(renderLinkPreview(msg));
        }
      }
    } else {
      bubble.classList.add('has-media');
      const mediaEl = renderMedia(msg);
      bubble.appendChild(mediaEl);
      if (msg.text_body && msg.media_type !== 'vcard') {
        const cap = document.createElement('div');
        cap.className = 'msg-caption';
        renderCollapsibleMessageText(cap, msg.text_body, msg.msg_id, msg.mentions);
        bubble.appendChild(cap);
      }
    }

    bubble.appendChild(meta);

    if (msg.reactions && msg.media_type !== 'deleted') {
      const emojis = msg.reactions.split(',').filter(Boolean);
      if (emojis.length) {
        row.classList.add('has-reactions');
        const rxEl = document.createElement('div');
        rxEl.className = 'msg-reactions';
        const distinct = Array.from(new Set(emojis));
        const emojiSpan = document.createElement('span');
        emojiSpan.className = 'rx-emojis';
        emojiSpan.textContent = distinct.slice(0, 3).join(' ');
        rxEl.appendChild(emojiSpan);

        if (emojis.length > 1) {
          const countSpan = document.createElement('span');
          countSpan.className = 'rx-count';
          countSpan.textContent = String(emojis.length);
          rxEl.appendChild(countSpan);
        }

        if (msg.msg_id !== undefined) {
          rxEl.addEventListener('click', e => {
            e.stopPropagation();
            showReactionDetails(msg.msg_id, rxEl);
          });
        }
        bubble.appendChild(rxEl);
      }
    }

    row.appendChild(bubble);
    return row;
  }

  function extractDomain(url) {
    if (!url) return '';
    try {
      const u = new URL(url);
      return u.hostname.replace(/^www\./, '').toUpperCase();
    } catch {
      return '';
    }
  }

  function renderLinkPreview(msg) {
    const card = document.createElement('div');
    const isBanner = Number(msg.link_preview_type) === 1;
    card.className = 'msg-link-preview' + (isBanner ? ' banner' : ' compact');

    const anchor = document.createElement('a');
    anchor.className = 'link-preview-anchor';
    anchor.href = msg.link_url || '#';
    anchor.target = '_blank';
    anchor.rel = 'noopener noreferrer';
    const domain = extractDomain(msg.link_url);
    const ariaTitle = msg.link_title || domain || 'Web page';
    anchor.setAttribute('aria-label', 'Link preview: ' + ariaTitle);

    if (msg.has_thumbnail && msg.msg_id) {
      const mediaBox = document.createElement('div');
      mediaBox.className = 'link-preview-media';
      const img = document.createElement('img');
      img.className = 'link-preview-thumb';
      img.src = API + '/thumbnail/' + msg.msg_id;
      img.alt = '';
      img.loading = 'lazy';
      img.onerror = () => {
        mediaBox.style.display = 'none';
      };
      mediaBox.appendChild(img);
      anchor.appendChild(mediaBox);
    }

    const body = document.createElement('div');
    body.className = 'link-preview-body';

    if (domain) {
      const domEl = document.createElement('div');
      domEl.className = 'link-preview-domain';
      domEl.textContent = domain;
      body.appendChild(domEl);
    }

    if (msg.link_title) {
      const titleEl = document.createElement('div');
      titleEl.className = 'link-preview-title';
      titleEl.textContent = msg.link_title;
      body.appendChild(titleEl);
    }

    if (msg.link_desc) {
      const descEl = document.createElement('div');
      descEl.className = 'link-preview-desc';
      descEl.textContent = msg.link_desc;
      body.appendChild(descEl);
    }

    anchor.appendChild(body);
    card.appendChild(anchor);
    return card;
  }

  function renderMissingMedia(msg) {
    const videoNote = msg.media_type === 'video' && isVideoNote(msg);
    const card = document.createElement('div');
    card.className = videoNote ? 'media-missing-card is-video-note' : 'media-missing-card';

    let ratio = msg.aspect_ratio || 0;
    if (!ratio && msg.media_width > 0 && msg.media_height > 0) {
      ratio = msg.media_width / msg.media_height;
    }
    if (videoNote) {
      card.style.aspectRatio = '1 / 1';
    } else if (ratio > 0) {
      card.style.aspectRatio = ratio.toFixed(3);
    } else if (msg.media_type === 'video' || msg.media_type === 'gif') {
      card.style.aspectRatio = '16 / 9';
    } else if (msg.media_type === 'image') {
      card.style.aspectRatio = '4 / 3';
    }

    if (msg.has_thumbnail) {
      const img = document.createElement('img');
      img.className = 'media-blurred-bg';
      img.src = API + '/thumbnail/' + encodeURIComponent(msg.msg_id);
      img.loading = 'lazy';
      img.alt = '';
      img.onerror = () => {
        img.style.display = 'none';
      };
      card.appendChild(img);
    }

    const overlay = document.createElement('div');
    overlay.className = 'media-missing-overlay';

    const isVn = msg.media_type === 'audio' && isVoiceNote(msg);
    const badge = document.createElement('div');
    badge.className = 'media-missing-badge';
    const icon = videoNote ? '🎥' :
                 isVn ? '🎤' :
                 msg.media_type === 'video' ? '🎥' :
                 msg.media_type === 'audio' ? '🎵' :
                 msg.media_type === 'sticker' ? '🩹' :
                 msg.media_type === 'gif' ? '🎞️' :
                 msg.media_type === 'vcard' ? '📇' :
                 msg.media_type === 'document' ? '📄' : '🖼️';
    badge.textContent = icon;
    overlay.appendChild(badge);

    const info = document.createElement('div');
    info.className = 'media-missing-info';

    const label = document.createElement('div');
    label.className = 'media-missing-label';
    label.textContent = videoNote ? 'Video message' :
                        isVn ? 'Voice message' :
                        msg.media_type === 'video' ? 'Video' :
                        msg.media_type === 'gif' ? 'GIF' :
                        msg.media_type === 'audio' ? 'Audio' :
                        msg.media_type === 'document' ? 'Document' :
                        msg.media_type === 'sticker' ? 'Sticker' : 'Photo';
    info.appendChild(label);

    const sizeStr = _formatBytes(msg.media_size);
    const durStr = (msg.media_duration && msg.media_duration > 0) ? _formatTimer(msg.media_duration) : '';

    const metaParts = [];
    if (sizeStr) metaParts.push(sizeStr);
    if (durStr) metaParts.push(durStr);
    if (metaParts.length > 0) {
      const meta = document.createElement('div');
      meta.className = 'media-missing-meta';
      meta.textContent = metaParts.join(' · ');
      info.appendChild(meta);
    }

    overlay.appendChild(info);
    card.appendChild(overlay);
    return card;
  }

  function _extractMsgAspectRatio(msg) {
    let ratio = Number(msg.aspect_ratio) || 0;
    if (!ratio && msg.media_width > 0 && msg.media_height > 0) {
      ratio = msg.media_width / msg.media_height;
    }
    return ratio;
  }

  function _applyMediaOrientationBox(wrap, rawRatio) {
    wrap.classList.remove('is-portrait', 'is-landscape');
    if (rawRatio > 0 && rawRatio < 1) {
      wrap.classList.add('is-portrait');
      const clamped = Math.max(0.75, Math.min(rawRatio, 0.999));
      wrap.style.aspectRatio = clamped.toFixed(3);
    } else if (rawRatio >= 1) {
      wrap.classList.add('is-landscape');
      const clamped = Math.max(1.0, Math.min(rawRatio, 1.778));
      wrap.style.aspectRatio = clamped.toFixed(3);
    } else {
      wrap.classList.add('is-landscape');
      wrap.style.aspectRatio = '1.333';
    }
  }
