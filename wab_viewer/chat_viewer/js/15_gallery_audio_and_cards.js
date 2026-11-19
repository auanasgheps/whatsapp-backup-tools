function closeMediaGallery() {
    archiveViewActive = false;
    archiveLoadingAll = false;
    galleryBackfillGen++;
    galleryOpenGen++;
    galleryLoadingMore = false;
    linksLoadingMore = false;
    if (currentGalleryAudio) {
      currentGalleryAudio.pause();
      currentGalleryAudio = null;
      currentGalleryAudioCell = null;
    }
    document.getElementById('media-gallery').classList.remove('open');
    if (galleryObserver) { galleryObserver.disconnect(); galleryObserver = null; }
    if (gallerySentinel) { gallerySentinel.remove(); gallerySentinel = null; }
    const loaders = document.querySelectorAll('.gallery-filter-loading, .gallery-scroll-loading, .gallery-end-notice, .gallery-empty-notice');
    loaders.forEach(el => el.remove());
    if (gallerySessionAbortController) {
      gallerySessionAbortController.abort();
      gallerySessionAbortController = null;
    }
    if (galleryCurrentAbortController) {
      galleryCurrentAbortController.abort();
      galleryCurrentAbortController = null;
    }
    lastGalleryScrollTop = 0;
    galleryScrollDirection = 'down';
    galleryPendingScrollCheck = false;
    _setStatPillsLoading(false);
    teardownMediaObservers();
    clearVideoProbeQueue();
    clearTimeout(galleryScrollIdleTimer);
    galleryIsScrolling = false;
  }

  function _sanitizeMediaName(name) {
    if (!name) return '';
    const clean = String(name).replace(/\\/g, '/');
    const base = clean.split('/').pop() || '';
    return base;
  }

  let currentGalleryAudio = null;
  let currentGalleryAudioCell = null;

  function _formatAudioDuration(sec) {
    const s = Math.max(0, Math.floor(Number(sec) || 0));
    if (s <= 0) return '';
    const m = Math.floor(s / 60);
    const rem = s % 60;
    return `${m}:${rem.toString().padStart(2, '0')}`;
  }

  function toggleGalleryAudioPlayback(cell, audioSrc) {
    stopActiveVoiceNote();
    if (activeVideoNotePause) {
      activeVideoNotePause();
    }
    if (currentGalleryAudioCell === cell && currentGalleryAudio) {
      if (currentGalleryAudio.paused) {
        currentGalleryAudio.play();
        cell.classList.add('is-playing');
      } else {
        currentGalleryAudio.pause();
        cell.classList.remove('is-playing');
      }
      return;
    }
    if (currentGalleryAudio) {
      currentGalleryAudio.pause();
      currentGalleryAudio.currentTime = 0;
      if (currentGalleryAudioCell) {
        currentGalleryAudioCell.classList.remove('is-playing');
      }
    }
    const aud = new Audio(audioSrc);
    currentGalleryAudio = aud;
    currentGalleryAudioCell = cell;
    cell.classList.add('is-playing');
    aud.play().catch(() => {
      cell.classList.remove('is-playing');
      if (currentGalleryAudioCell === cell) {
        currentGalleryAudio = null;
        currentGalleryAudioCell = null;
      }
    });
    aud.addEventListener('ended', () => {
      cell.classList.remove('is-playing');
      currentGalleryAudio = null;
      currentGalleryAudioCell = null;
    });
    aud.addEventListener('pause', () => {
      cell.classList.remove('is-playing');
    });
  }

  function buildMissingGalleryCard(cell, msg) {
    if (msg) {
      msg.is_missing = true;
    }
    cell.classList.add('is-missing');
    cell.innerHTML = '';
    delete cell.dataset.src;
    delete cell.dataset.archivePath;
    cell.dataset.hydrated = 'true';

    // Clicking the unavailable media card should do nothing
    cell.addEventListener('click', e => {
      e.stopPropagation();
    });

    if (msg && msg.has_thumbnail && msg.msg_id) {
      const blurImg = document.createElement('img');
      blurImg.className = 'gallery-missing-blur';
      blurImg.src = '/api/thumbnail/' + encodeURIComponent(msg.msg_id);
      blurImg.alt = '';
      blurImg.loading = 'lazy';
      blurImg.onerror = () => { blurImg.style.display = 'none'; };
      cell.appendChild(blurImg);
    }

    const overlay = document.createElement('div');
    overlay.className = 'gallery-missing-overlay';

    const videoNote = Boolean(msg && msg.media_type === 'video' && (msg.is_video_note || isVideoNote(msg)));
    const isVn = Boolean(msg && (msg.is_voice_note || isVoiceNote(msg)));
    const mt = msg ? msg.media_type : '';

    const badge = document.createElement('div');
    badge.className = 'gallery-missing-badge';
    const icon = videoNote ? '🎥' :
                 isVn ? '🎤' :
                 mt === 'video' ? '🎥' :
                 mt === 'audio' ? '🎵' :
                 mt === 'sticker' ? '🩹' :
                 mt === 'gif' ? '🎞️' :
                 mt === 'vcard' ? '📇' :
                 mt === 'document' ? '📄' : '🖼️';
    badge.textContent = icon;
    overlay.appendChild(badge);

    const info = document.createElement('div');
    info.className = 'media-missing-info';

    const label = document.createElement('div');
    label.className = 'gallery-missing-label';
    if (msg && msg.is_view_once) {
      label.textContent = (mt === 'video') ? 'Video · View Once' : 'Photo · View Once';
    } else {
      const typeStr = videoNote ? 'Video message' :
                      isVn ? 'Voice message' :
                      mt === 'video' ? 'Video' :
                      mt === 'gif' ? 'GIF' :
                      mt === 'audio' ? 'Audio' :
                      mt === 'document' ? 'Document' :
                      mt === 'sticker' ? 'Sticker' :
                      (mt === 'location' || mt === 'live_location') ? 'Location' : 'Photo';
      label.textContent = typeStr + ' unavailable';
    }
    info.appendChild(label);

    const sizeStr = (msg && msg.media_size) ? _formatBytes(msg.media_size) : '';
    const durStr = (msg && msg.media_duration && msg.media_duration > 0) ? _formatTimer(msg.media_duration) : '';

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
    cell.appendChild(overlay);

    if (msg) {
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
    }
  }
