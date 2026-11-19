function renderGalleryItem(msg) {
    const cell = document.createElement('div');
    cell.className = 'gallery-item';
    cell._msg = msg;
    const mt = msg.media_type;
    const isVn = (mt === 'audio' && isVoiceNote(msg));
    cell.dataset.type = isVn ? 'voice' : mt;
    cell.dataset.ts = String(msg.timestamp_ms || 0);
    if (msg.msg_id || msg.id) {
      cell.dataset.msgId = String(msg.msg_id || msg.id);
    }

    const isPtv = msg.is_video_note || isVideoNote(msg);
    if (isPtv) {
      cell.classList.add('is-video-note');
      const ptvBadge = document.createElement('div');
      ptvBadge.className = 'gallery-ptv-badge';
      ptvBadge.title = 'Video message';
      ptvBadge.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 14.5v-9l6 4.5-6 4.5z"/></svg>';
      cell.appendChild(ptvBadge);
    }

    const cleanName = _sanitizeMediaName(msg.media_name);

    if (!msg.archive_path && mt !== 'link' && mt !== 'vcard') {
      if (msg.has_thumbnail && msg.msg_id) {
        cell.classList.add('is-missing', 'has-thumb');
        cell.dataset.hydrated = 'true';
        const img = document.createElement('img');
        img.src = '/api/thumbnail/' + encodeURIComponent(msg.msg_id);
        img.alt = (mt === 'video' ? 'Video' : 'Photo');
        img.className = 'gallery-thumb';
        cell.appendChild(img);

        const warnBadge = document.createElement('div');
        warnBadge.className = 'gallery-missing-indicator-badge';
        warnBadge.title = 'Media file not found on disk';
        warnBadge.innerHTML = '<svg class="gallery-missing-indicator-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';
        cell.appendChild(warnBadge);

        const btn = document.createElement('button');
        btn.className = 'gallery-goto';
        btn.title = 'Go to message';
        btn.textContent = '→ in chat';
        btn.addEventListener('click', e => {
          e.stopPropagation();
          if (btn.disabled) return;
          btn.disabled = true;
          btn.textContent = '⏳ in chat';
          closeMediaGallery();
          jumpToMessage(msg, 'default');
        });
        cell.appendChild(btn);

        // Clicking the unavailable media should do nothing
        cell.addEventListener('click', e => {
          e.stopPropagation();
        });
        return cell;
      }
      buildMissingGalleryCard(cell, msg);
      return cell;
    }

    const src = '/media/' + msg.archive_path;

    if (mt === 'image' || mt === 'sticker') {
      cell.dataset.src = src;
      cell.dataset.hydrated = 'false';
      const img = document.createElement('img');
      img.src = EMPTY_GALLERY_PIX;
      img.alt = (mt === 'sticker' ? 'Sticker' : 'Photo');
      img.className = 'gallery-thumb';
      const idx = lightboxItems.length;
      lightboxItems.push(msg);
      img.addEventListener('click', () => { lightboxContext = 'gallery'; lightboxIndex = idx; openLightboxAt(idx, 0); });
      img.addEventListener('error', () => {
        buildMissingGalleryCard(cell, msg);
      }, { once: true });
      cell.appendChild(img);
    } else if (mt === 'video' || mt === 'gif') {
      cell.dataset.src = src;
      cell.dataset.archivePath = msg.archive_path;
      if (msg.has_thumbnail && msg.msg_id) {
        cell.dataset.thumbSrc = '/api/thumbnail/' + encodeURIComponent(msg.msg_id);
      } else if (!videoThumbCache.has(msg.archive_path)) {
        cell.classList.add('is-loading-thumb');
      }
      cell.dataset.hydrated = 'false';
      const img = document.createElement('img');
      img.src = EMPTY_GALLERY_PIX;
      img.alt = (mt === 'gif' ? 'GIF' : 'Video');
      img.className = 'gallery-thumb gallery-video-thumb';
      const idx = lightboxItems.length;
      lightboxItems.push(msg);
      img.addEventListener('click', () => { lightboxContext = 'gallery'; lightboxIndex = idx; openLightboxAt(idx, 0); });
      img.addEventListener('error', () => {
        cell.classList.remove('is-loading-thumb');
        img.classList.add('gallery-video-thumb--err');
      }, { once: true });
      img.addEventListener('load', () => {
        if (img.src && !img.src.startsWith('data:image/svg')) {
          cell.classList.remove('is-loading-thumb');
        }
      });
      cell.appendChild(img);
    } else if (mt === 'audio') {
      cell.classList.add('gallery-audio-item');
      if (isVn) {
        cell.classList.add('is-voice-note');
      } else {
        cell.classList.add('is-audio-file');
      }

      const hdr = document.createElement('div');
      hdr.className = 'gallery-audio-header';

      const badge = document.createElement('div');
      badge.className = 'gallery-audio-badge ' + (isVn ? 'voice' : 'music');
      if (isVn) {
        badge.title = 'Voice message';
        badge.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/></svg>';
      } else {
        badge.title = 'Audio file';
        badge.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>';
      }
      hdr.appendChild(badge);

      const durText = _formatAudioDuration(msg.media_duration);
      if (durText) {
        const durPill = document.createElement('span');
        durPill.className = 'gallery-audio-dur';
        durPill.textContent = durText;
        hdr.appendChild(durPill);
      } else if (!isVn) {
        const ext = (cleanName.match(/\.([a-z0-9]+)$/i) || [])[1];
        if (ext) {
          const extPill = document.createElement('span');
          extPill.className = 'gallery-audio-dur';
          extPill.textContent = ext.toUpperCase();
          hdr.appendChild(extPill);
        }
      }
      cell.appendChild(hdr);

      const body = document.createElement('div');
      body.className = 'gallery-audio-body';

      const title = document.createElement('div');
      title.className = 'gallery-audio-title';
      if (isVn) {
        title.textContent = 'Voice message';
      } else {
        const cleanTitle = (cleanName && cleanName !== 'audio')
          ? cleanName.replace(/\.[a-z0-9]+$/i, '')
          : 'Audio track';
        title.textContent = cleanTitle;
        title.title = cleanName || 'Audio track';
      }
      body.appendChild(title);

      const sub = document.createElement('div');
      sub.className = 'gallery-audio-sub';
      const senderText = msg.sender || (msg.from_me ? 'You' : '');
      const timeText = msg.timestamp_ms ? fmtTime(msg.timestamp_ms) : '';
      sub.textContent = [senderText, timeText].filter(Boolean).join(' · ');
      body.appendChild(sub);
      cell.appendChild(body);

      const footer = document.createElement('div');
      footer.className = 'gallery-audio-footer';

      const playBtn = document.createElement('button');
      playBtn.className = 'gallery-audio-play';
      playBtn.title = 'Play / Pause';
      playBtn.setAttribute('aria-label', isVn ? 'Play voice message' : 'Play audio');
      playBtn.innerHTML = '<svg class="play-icon" width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg><svg class="pause-icon" width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>';
      playBtn.addEventListener('click', e => {
        e.stopPropagation();
        toggleGalleryAudioPlayback(cell, src);
      });
      footer.appendChild(playBtn);

      const gotoBtn = document.createElement('button');
      gotoBtn.className = 'gallery-goto';
      gotoBtn.title = 'Go to message';
      gotoBtn.textContent = '→ in chat';
      gotoBtn.addEventListener('click', e => {
        e.stopPropagation();
        if (gotoBtn.disabled) return;
        gotoBtn.disabled = true;
        gotoBtn.textContent = '⏳ in chat';
        if (currentGalleryAudio) {
          currentGalleryAudio.pause();
          currentGalleryAudio = null;
          currentGalleryAudioCell = null;
        }
        closeMediaGallery();
        jumpToMessage(msg, 'default');
      });
      footer.appendChild(gotoBtn);
      cell.appendChild(footer);

      cell.addEventListener('click', () => {
        toggleGalleryAudioPlayback(cell, src);
      });
    } else if (mt === 'link') {
      const urlMatch = (msg.text_body || '').match(/https?:\/\/[^\s<>"]+/);
      const rawUrl = msg.link_url || (urlMatch ? urlMatch[0] : '') || msg.text_body || '';
      const url = rawUrl.replace(/[.,;:!?)'"]+$/, '');
      const domain = extractDomain(url);

      cell.classList.add('gallery-link-card', 'gallery-rich-link-card');

      const mediaBox = document.createElement('div');
      mediaBox.className = 'gallery-rich-link-media';

      if (msg.has_thumbnail && msg.msg_id) {
        cell.dataset.src = '/api/thumbnail/' + msg.msg_id;
        cell.dataset.hydrated = 'false';
        const img = document.createElement('img');
        img.className = 'gallery-thumb gallery-rich-link-thumb';
        img.src = EMPTY_GALLERY_PIX;
        img.alt = '';
        mediaBox.appendChild(img);
      } else {
        mediaBox.innerHTML = '<span class="gallery-rich-link-fallback-icon">🔗</span>';
      }
      cell.appendChild(mediaBox);

      const body = document.createElement('div');
      body.className = 'gallery-rich-link-body';

      if (domain) {
        const domEl = document.createElement('div');
        domEl.className = 'gallery-rich-link-domain';
        domEl.textContent = domain;
        body.appendChild(domEl);
      }

      const titleText = msg.link_title || domain || url || 'Link';
      const titleEl = document.createElement('a');
      titleEl.className = 'gallery-rich-link-title' + (!msg.link_desc ? ' no-desc' : '');
      titleEl.href = url || '#';
      titleEl.target = '_blank';
      titleEl.rel = 'noopener noreferrer';
      titleEl.title = titleText;
      titleEl.textContent = titleText;
      titleEl.setAttribute('aria-label', 'Open link: ' + titleText);
      body.appendChild(titleEl);

      if (msg.link_desc) {
        const descEl = document.createElement('div');
        descEl.className = 'gallery-rich-link-desc';
        descEl.title = msg.link_desc;
        descEl.textContent = msg.link_desc;
        body.appendChild(descEl);
      }

      const metaEl = document.createElement('div');
      metaEl.className = 'gallery-rich-link-meta';
      const dateStr = new Date(msg.timestamp_ms).toLocaleDateString(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric'
      });
      const senderStr = msg.sender || 'You';
      const metaText = senderStr + ' · ' + dateStr + (url ? ' · ' + url : '');
      metaEl.textContent = metaText;
      metaEl.title = metaText;
      body.appendChild(metaEl);

      cell.appendChild(body);
      cell.addEventListener('click', e => {
        if (!e.target.closest('a') && !e.target.closest('.gallery-goto')) {
          if (url && url !== '#') window.open(url, '_blank');
        }
      });
    } else if (mt === 'document') {
      cell.classList.add('gallery-link-card');
      const name = document.createElement('div');
      name.className = 'gallery-link-url';
      name.textContent = '📄 ' + (cleanName || 'file');
      const meta = document.createElement('div');
      meta.className = 'gallery-link-meta';
      meta.textContent = (msg.sender || 'You') + ' · ' + new Date(msg.timestamp_ms).toLocaleDateString(undefined, {year:'numeric', month:'short', day:'numeric'});
      cell.appendChild(name);
      cell.appendChild(meta);
      cell.addEventListener('click', () => window.open(src, '_blank'));
    } else if (mt === 'vcard') {
      const d = document.createElement('div');
      d.className = 'gallery-doc';
      d.innerHTML = '<span style="font-size:28px">📇</span><span>' + esc(cleanName || msg.text_body || 'Contact') + '</span>';
      d.addEventListener('click', () => {
        if (msg.archive_path) {
          window.open(src, '_blank');
        } else {
          closeMediaGallery();
          jumpToMessage(msg, 'default');
        }
      });
      cell.appendChild(d);
    } else {
      const d = document.createElement('div');
      d.className = 'gallery-doc';
      d.innerHTML = '<span style="font-size:28px">📄</span><span>' + esc(cleanName || 'file') + '</span>';
      d.addEventListener('click', () => window.open(src, '_blank'));
      cell.appendChild(d);
    }

    const btn = document.createElement('button');
    btn.className = 'gallery-goto';
    btn.title = 'Go to message';
    btn.textContent = '→ in chat';
    btn.addEventListener('click', e => {
      e.stopPropagation();
      if (btn.disabled) return;
      btn.disabled = true;
      btn.textContent = '⏳ in chat';
      closeMediaGallery();
      jumpToMessage(msg, 'default');
    });
    cell.appendChild(btn);
    return cell;
  }

  function _insertGalleryGridBatch(grid, items) {
    if (!grid || !items || !items.length) return;
    const observer = getGalleryMediaObserver();
    const linkObserver = getGalleryLinkObserver();
    const cellsToObserve = [];
    const linkCellsToObserve = [];

    // Fast check: pure tail append if items are strictly older than the last cell in grid
    const existingHeaders = [...grid.querySelectorAll('.gallery-month-header')];
    const oldestHeader = existingHeaders[existingHeaders.length - 1];
    const oldestHeaderMk = oldestHeader ? parseInt(oldestHeader.dataset.month, 10) : null;

    const allCells = grid.querySelectorAll('.gallery-item');
    const lastCell = allCells.length ? allCells[allCells.length - 1] : null;
    const oldestTsInGrid = lastCell ? parseInt(lastCell.dataset.ts || '0', 10) : 0;

    const isTailAppend = !lastCell || (items[0].timestamp_ms <= oldestTsInGrid);

    if (isTailAppend) {
      const fragment = document.createDocumentFragment();
      let currentMk = oldestHeader ? oldestHeaderMk : null;

      for (const msg of items) {
        const msgId = msg.msg_id || msg.id;
        if (msgId) {
          const existing = grid.querySelector(`.gallery-item[data-msg-id="${msgId}"]`);
          if (existing) {
            if (!msg._prepopulated && existing.dataset.prepopulated === 'true') {
              delete existing.dataset.prepopulated;
            }
            if (activeTypes.size > 0) {
              existing.classList.toggle('type-hidden', !activeTypes.has(existing.dataset.type));
            } else {
              existing.classList.remove('type-hidden');
            }
            continue;
          }
        }

        const mk = monthKey(msg.timestamp_ms);
        if (mk !== currentMk) {
          currentMk = mk;
          if (!grid.querySelector(`.gallery-month-header[data-month="${mk}"]`)) {
            const monthHdr = document.createElement('div');
            monthHdr.className = 'gallery-month-header';
            monthHdr.dataset.month = String(mk);
            monthHdr.textContent = fmtMonthHeader(msg.timestamp_ms);
            fragment.appendChild(monthHdr);
          }
        }

        const cell = renderGalleryItem(msg);
        if (msg._prepopulated) {
          cell.dataset.prepopulated = 'true';
        }
        if (activeTypes.size > 0 && !activeTypes.has(cell.dataset.type || msg.media_type)) {
          cell.classList.add('type-hidden');
        }
        fragment.appendChild(cell);
        if (cell.dataset.src) {
          if (cell.dataset.type === 'link') {
            linkCellsToObserve.push(cell);
          } else {
            cellsToObserve.push(cell);
          }
        }
      }

      grid.insertBefore(fragment, gallerySentinel || null);
    } else {
      // Interleaved items fallback
      for (const msg of items) {
        _insertGalleryGridItem(grid, msg);
      }
    }

    if (observer) {
      for (const c of cellsToObserve) {
        observer.observe(c);
      }
    }
    if (linkObserver) {
      for (const c of linkCellsToObserve) {
        linkObserver.observe(c);
      }
    }
  }

  function _insertGalleryGridItem(grid, msg) {
    const isPrepopulated = Boolean(msg && msg._prepopulated);
    const msgId = msg.msg_id || msg.id;
    if (msgId) {
      const existing = grid.querySelector(`.gallery-item[data-msg-id="${msgId}"]`);
      if (existing) {
        if (!isPrepopulated && existing.dataset.prepopulated === 'true') {
          delete existing.dataset.prepopulated;
        }
        if (activeTypes.size > 0) {
          existing.classList.toggle('type-hidden', !activeTypes.has(existing.dataset.type));
        } else {
          existing.classList.remove('type-hidden');
        }
        return;
      }
    }

    const mk = monthKey(msg.timestamp_ms);
    let monthHdr = grid.querySelector(`.gallery-month-header[data-month="${mk}"]`);
    if (!monthHdr) {
      monthHdr = document.createElement('div');
      monthHdr.className = 'gallery-month-header';
      monthHdr.dataset.month = String(mk);
      monthHdr.textContent = fmtMonthHeader(msg.timestamp_ms);
      const olderHdr = [...grid.querySelectorAll('.gallery-month-header')]
        .find(el => parseInt(el.dataset.month, 10) < mk);
      grid.insertBefore(monthHdr, olderHdr || gallerySentinel || null);
    }

    const cell = renderGalleryItem(msg);
    if (isPrepopulated) {
      cell.dataset.prepopulated = 'true';
    }
    if (activeTypes.size > 0 && !activeTypes.has(cell.dataset.type || msg.media_type)) {
      cell.classList.add('type-hidden');
    }

    const nextSection = [...grid.querySelectorAll('.gallery-month-header')]
      .find(el => parseInt(el.dataset.month, 10) < mk) || gallerySentinel || null;

    let insertBeforeEl = nextSection;
    let curr = monthHdr.nextElementSibling;
    while (curr && curr !== nextSection) {
      if (curr.classList.contains('gallery-item')) {
        const currTs = parseInt(curr.dataset.ts || '0', 10);
        if (currTs < msg.timestamp_ms) {
          insertBeforeEl = curr;
          break;
        }
      }
      curr = curr.nextElementSibling;
    }
    grid.insertBefore(cell, insertBeforeEl);
    if (cell.dataset.src) {
      if (cell.dataset.type === 'link') {
        getGalleryLinkObserver()?.observe(cell);
      } else {
        getGalleryMediaObserver()?.observe(cell);
      }
    }
  }
