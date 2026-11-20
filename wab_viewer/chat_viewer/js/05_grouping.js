// ---- media grouping -------------------------------------------------------
  //
  // Groups consecutive image/video messages from the same sender (within 2 min gaps)
  // into a 2x2 grid bubble. A run must have >= 4 items to qualify.

  const MEDIA_GROUP_GAP_MS = 2 * 60 * 1000;
  const MEDIA_GROUP_MIN = 4;

  function isVideoNote(msg) {
    if (!msg) return false;
    if (Boolean(msg.is_video_note) || msg.raw_msg_type === 28 || msg.raw_msg_type === 81 || msg.raw_msg_type === 54) return true;
    const p = (msg.archive_path || msg.original_path || '').toLowerCase().replace(/\\/g, '/');
    return p.includes('/whatsapp video notes/') || p.startsWith('whatsapp video notes/') || /(^|\/)ptv-\d+/i.test(p);
  }

  function isVoiceNote(msg) {
    if (!msg) return false;
    if (msg.is_voice_note === 1 || msg.is_voice_note === true) return true;
    if (msg.waveform || msg.audio_waveform) return true;
    const p = String(msg.archive_path || msg.original_path || msg.media_name || '').toLowerCase();
    return p.includes('voice notes') || p.includes('ptt-') || /(^|\/)audi_|\.opus($|\?)/i.test(p);
  }

  function isGrouableMedia(msg) {
    return !msg.is_view_once && msg.archive_path && !isVideoNote(msg) &&
      (msg.media_type === 'image' || msg.media_type === 'video' || msg.media_type === 'gif');
  }

  function sameSender(a, b) {
    if (a.from_me !== b.from_me) return false;
    // For 1:1 chats, from_me alone is sufficient.
    // For groups, check display name; unknown sender (empty string) never matches.
    if (currentChat && currentChat.type === 'group') {
      if (!a.sender || !b.sender) return false;
      return a.sender === b.sender;
    }
    return true;
  }

  function rowForMsgId(id) {
    const key = String(id);
    const escKey = (typeof CSS !== 'undefined' && typeof CSS.escape === 'function')
      ? CSS.escape(key)
      : key.replace(/(["\\:])/g, '\\$1');
    const direct = document.querySelector(`.msg-row[data-msg-id="${escKey}"]`);
    if (direct) return direct;
    return document.querySelector(`.msg-row[data-group-ids~="${escKey}"]`) || null;
  }

  function nearestMsgToTs(ts) {
    if (!msgList.length) return null;
    return msgList.reduce((best, m) =>
      Math.abs(m.timestamp_ms - ts) < Math.abs(best.timestamp_ms - ts) ? m : best
    );
  }

  function captureAnchor(direction) {
    const scroll = document.getElementById('message-scroll');
    const top = scroll.getBoundingClientRect().top;
    const bottom = scroll.getBoundingClientRect().bottom;
    const rows = scroll.querySelectorAll('.msg-row[data-msg-id]');
    if (!rows.length) return null;

    if (direction === 'newer') {
      for (let i = rows.length - 1; i >= 0; i--) {
        const row = rows[i];
        const rect = row.getBoundingClientRect();
        if (rect.top < bottom && rect.bottom > top) {
          return { id: row.dataset.msgId, offset: rect.top - top };
        }
      }
    } else {
      for (let i = 0; i < rows.length; i++) {
        const row = rows[i];
        const rect = row.getBoundingClientRect();
        if (rect.bottom > top) {
          return { id: row.dataset.msgId, offset: rect.top - top };
        }
      }
    }
    return null;
  }

  function restoreAnchor(anchor) {
    if (!anchor) return;
    const row = rowForMsgId(anchor.id);
    if (!row) return;
    const scroll = document.getElementById('message-scroll');
    const top = scroll.getBoundingClientRect().top;
    scroll.scrollTop += (row.getBoundingClientRect().top - top) - anchor.offset;
  }

  function mergePage(newMsgs) {
    const seenIds = new Map(msgList.map(m => [String(m.msg_id), m]));
    let addedCount = 0;
    for (const m of newMsgs) {
      if (!seenIds.has(String(m.msg_id))) {
        seenIds.set(String(m.msg_id), m);
        msgList.push(m);
        addedCount++;
      }
    }
    // Discard any preserved duplicates when matching non-deleted live messages are present
    const liveStanzas = new Set();
    const liveKeys = new Set();
    for (const m of msgList) {
      if (!m.is_preserved && m.media_type !== 'deleted') {
        if (m.stanza_id) liveStanzas.add(m.stanza_id);
        liveKeys.add(`${m.timestamp_ms}_${m.from_me ? 1 : 0}`);
      }
    }
    if (liveStanzas.size > 0 || liveKeys.size > 0) {
      msgList = msgList.filter(m => {
        if (m.is_preserved) {
          if (m.stanza_id && liveStanzas.has(m.stanza_id)) return false;
          if (liveKeys.has(`${m.timestamp_ms}_${m.from_me ? 1 : 0}`)) return false;
        }
        return true;
      });
    }
    msgList.sort((a, b) => {
      const tsDiff = (a.timestamp_ms || 0) - (b.timestamp_ms || 0);
      if (tsDiff !== 0) return tsDiff;
      const sa = Number(a.sort_id ?? a.msg_id);
      const sb = Number(b.sort_id ?? b.msg_id);
      if (!Number.isNaN(sa) && !Number.isNaN(sb) && sa !== sb) return sa - sb;
      return String(a.msg_id ?? '').localeCompare(String(b.msg_id ?? ''));
    });
    return addedCount;
  }

  function pruneList(direction) {
    if (msgList.length <= MAX_WINDOW) return;
    const drop = msgList.length - MAX_WINDOW;
    if (direction === 'older') {
      msgList.splice(msgList.length - drop, drop);
      noMoreNewer = false;
    } else {
      msgList.splice(0, drop);
      noMoreOlder = false;
    }
  }

  function computeRenderUnits(list) {
    const units = [];
    for (let i = 0; i < list.length;) {
      const first = list[i];
      if (first.is_live_photo_video) {
        i++;
        continue;
      }
      if (isGrouableMedia(first)) {
        const run = [first];
        let j = i + 1;
        while (j < list.length) {
          const prev = run[run.length - 1];
          const curr = list[j];
          if (curr.is_live_photo_video) {
            j++;
            continue;
          }
          if (!isGrouableMedia(curr)) break;
          if (!sameSender(first, curr)) break;
          if (curr.timestamp_ms - prev.timestamp_ms > MEDIA_GROUP_GAP_MS) break;
          run.push(curr);
          j++;
        }
        if (run.length >= MEDIA_GROUP_MIN) {
          units.push({ type: 'group', members: run });
          i = j;
          continue;
        }
      }
      units.push({ type: 'single', msg: first });
      i++;
    }
    return units;
  }

  function renderWindow() {
    const scroll = document.getElementById('message-scroll');
    scroll.innerHTML = '';
    const units = computeRenderUnits(msgList);
    let prevTs = null;
    for (const unit of units) {
      const ts = unit.type === 'group' ? unit.members[0].timestamp_ms : unit.msg.timestamp_ms;
      if (prevTs === null || dayKey(ts) !== dayKey(prevTs)) {
        scroll.appendChild(makeDateSeparator(ts));
      }
      scroll.appendChild(renderBubble(unit));
      prevTs = unit.type === 'group'
        ? unit.members[unit.members.length - 1].timestamp_ms
        : unit.msg.timestamp_ms;
    }
  }

  function flashHighlightRow(row, highlightType) {
    if (!row) return;
    const bubble = row.querySelector('.msg-bubble, .msg-service-bubble');
    if (bubble) {
      const animClass = highlightType === 'gold'
        ? 'jump-highlight-gold'
        : (highlightType === 'emerald' ? 'jump-highlight-emerald' : 'search-jump-highlight');
      const duration = (highlightType === 'gold' || highlightType === 'emerald') ? 2200 : 1200;
      bubble.classList.remove('search-jump-highlight', 'jump-highlight-gold', 'jump-highlight-emerald');
      void bubble.offsetWidth;
      bubble.classList.add(animClass);
      const timer = setTimeout(() => bubble.classList.remove(animClass), duration);
      bubble.addEventListener('animationend', () => {
        clearTimeout(timer);
        bubble.classList.remove(animClass);
      }, { once: true });
    }
  }

  async function jumpToMessage(msg, highlightType) {
    const inWin = rowForMsgId(msg.msg_id);
    if (inWin) {
      const gen = ++_loadGen;
      isUserAtBottom = false;
      const loadingEl = document.getElementById('chat-loading');
      if (loadingEl) loadingEl.style.display = 'none';
      const btn = document.getElementById('scroll-to-bottom');
      if (btn) btn.classList.add('visible');
      await _pinToMsg(inWin, gen);
      flashHighlightRow(inWin, highlightType);
      return inWin;
    }
    const row = await jumpToTimestamp(msg.timestamp_ms, msg.msg_id, true, highlightType);
    return row;
  }

  function _attachMediaScrollAnchor(mediaEl) {
    mediaEl.addEventListener('load', () => {
      const scroll = document.getElementById('message-scroll');
      if (!scroll || !isUserAtBottom) return;
      suppressScroll = true;
      scroll.scrollTop = scroll.scrollHeight;
      suppressScroll = false;
    });
  }

  const gifViewportObserver = typeof IntersectionObserver !== 'undefined'
    ? new IntersectionObserver((entries) => {
        for (const entry of entries) {
          const vid = entry.target;
          const container = vid.closest('.msg-gif');
          if (!container) continue;
          if (!entry.isIntersecting) {
            if (!vid.paused) {
              vid.pause();
            }
          } else {
            if (container.classList.contains('is-playing') && vid.paused) {
              vid.play().catch(() => {});
            }
          }
        }
      }, { threshold: 0.05 })
    : null;

  let activeVideoNotePause = null;

  const videoNoteViewportObserver = typeof IntersectionObserver !== 'undefined'
    ? new IntersectionObserver((entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting && typeof entry.target._pauseVideoNote === 'function') {
            entry.target._pauseVideoNote();
          }
        }
      }, { threshold: 0.05 })
    : null;

  function renderMediaGrid(group) {
    const grid = document.createElement('div');
    grid.className = 'media-grid';

    const visible = group.slice(0, 4);
    visible.forEach((msg, idx) => {
      const cell = document.createElement('div');
      cell.className = 'media-grid-cell';

      // Build a lightweight media element (no caption, no reactions in grid)
      const wrap = document.createElement('div');
      wrap.className = 'msg-media';
      const src = MEDIA + msg.archive_path;
      const mt = msg.media_type;

      if (mt === 'image' || mt === 'sticker') {
        const cleanName = _sanitizeMediaName(msg.media_name);
        const img = document.createElement('img');
        img.src = src;
        img.loading = 'lazy';
        img.alt = cleanName || (mt === 'sticker' ? 'Sticker' : 'Photo');
        img.addEventListener('click', () => showLightboxForGroup(group, idx));
        img.addEventListener('error', () => {
          msg.is_missing = 1;
          const missingCard = renderMissingMedia(msg);
          if (wrap.isConnected) {
            wrap.replaceWith(missingCard);
          } else {
            wrap.innerHTML = '';
            wrap.className = missingCard.className;
            wrap.style.cssText = missingCard.style.cssText;
            while (missingCard.firstChild) {
              wrap.appendChild(missingCard.firstChild);
            }
          }
        }, { once: true });
        _attachMediaScrollAnchor(img);
        wrap.appendChild(img);
      } else if (mt === 'video' || mt === 'gif') {
        const vid = document.createElement('video');
        vid.controls = (mt === 'video');
        vid.preload = 'metadata';
        vid.src = src;
        const handleGridVideoError = () => {
          msg.is_missing = 1;
          const missingCard = renderMissingMedia(msg);
          if (wrap.isConnected) {
            wrap.replaceWith(missingCard);
          } else {
            wrap.innerHTML = '';
            wrap.className = missingCard.className;
            wrap.style.cssText = missingCard.style.cssText;
            while (missingCard.firstChild) {
              wrap.appendChild(missingCard.firstChild);
            }
          }
        };
        vid.addEventListener('error', handleGridVideoError, { once: true });
        if (mt === 'gif') {
          vid.muted = true;
          vid.playsInline = true;
          vid.loop = true;
          const badgeWrap = document.createElement('div');
          badgeWrap.className = 'msg-gif-badge-wrap';
          badgeWrap.setAttribute('aria-hidden', 'true');
          const badge = document.createElement('div');
          badge.className = 'msg-gif-badge';
          badge.textContent = 'GIF';
          badgeWrap.appendChild(badge);
          wrap.appendChild(badgeWrap);
        }
        vid.addEventListener('click', () => showLightboxForGroup(group, idx));
        vid.addEventListener('loadeddata', () => {
          if (vid.dataset.posterCaptured || !vid.videoWidth || !vid.videoHeight) return;
          vid.dataset.posterCaptured = '1';
          const canvas = document.createElement('canvas');
          canvas.width = vid.videoWidth;
          canvas.height = vid.videoHeight;
          canvas.getContext('2d').drawImage(vid, 0, 0);
          vid.poster = canvas.toDataURL('image/jpeg', 0.8);
          const scroll = document.getElementById('message-scroll');
          if (scroll && isUserAtBottom) {
            suppressScroll = true;
            scroll.scrollTop = scroll.scrollHeight;
            suppressScroll = false;
          }
        }, { once: true });
        wrap.appendChild(vid);
      }

      cell.appendChild(wrap);

      // Overflow badge on the last visible cell
      if (idx === 3 && group.length > 4) {
        const badge = document.createElement('div');
        badge.className = 'media-grid-overflow';
        badge.textContent = '+' + (group.length - 4);
        cell.appendChild(badge);
      }

      grid.appendChild(cell);
    });

    return grid;
  }

  function showLightboxForGroup(group, index) {
    if (group[index] && group[index].is_missing) return;
    showLightbox(group[index]);
  }
