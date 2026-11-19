function isLightboxMedia(m) {
    return m.archive_path && !m.is_missing && (m.media_type === 'image' || m.media_type === 'video' ||
      m.media_type === 'gif' || m.media_type === 'sticker');
  }

  function _closeLb() {
    const lb = document.getElementById('img-lightbox');
    if (!lb) return;
    lbZoomController?.destroy();
    lbZoomController = null;
    lb.classList.add('is-closing');
    lb.addEventListener('animationend', () => lb.remove(), { once: true });
    setTimeout(() => lb.isConnected && lb.remove(), 300);
  }

  let lightboxNavSeq = 0;
  let lbZoomController = null;

  function _setupLightboxZoom(lb) {
    let scale = 1;
    let panX = 0;
    let panY = 0;
    let isDragging = false;
    let hasDragged = false;
    let startX = 0;
    let startY = 0;
    let currentMedia = null;

    let zoomBadge = lb.querySelector('.lb-zoom-badge');
    if (!zoomBadge) {
      zoomBadge = document.createElement('div');
      zoomBadge.className = 'lb-zoom-badge';
      zoomBadge.style.display = 'none';
      zoomBadge.title = 'Click to reset zoom';
      lb.appendChild(zoomBadge);
    }

    function applyTransform(animated) {
      if (!currentMedia) return;
      currentMedia.style.transition = animated ? 'transform 0.12s ease-out' : 'none';
      currentMedia.style.transform = `translate(${panX}px, ${panY}px) scale(${scale})`;
      if (scale > 1) {
        currentMedia.style.cursor = isDragging ? 'grabbing' : 'grab';
        zoomBadge.textContent = `${Math.round(scale * 100)}% \u00d7 Reset`;
        zoomBadge.style.display = 'block';
      } else {
        currentMedia.style.cursor = 'zoom-in';
        zoomBadge.style.display = 'none';
      }
    }

    function clampPan() {
      if (!currentMedia || scale <= 1) {
        panX = 0;
        panY = 0;
        return;
      }
      const stage = lb.querySelector('.lb-stage');
      const stageW = (stage && stage.clientWidth) ? stage.clientWidth : window.innerWidth;
      const stageH = (stage && stage.clientHeight) ? stage.clientHeight : window.innerHeight;
      const scaledW = currentMedia.offsetWidth * scale;
      const scaledH = currentMedia.offsetHeight * scale;
      const maxPanX = Math.max(40, (scaledW - stageW) / 2 + 40);
      const maxPanY = Math.max(40, (scaledH - stageH) / 2 + 40);
      panX = Math.max(-maxPanX, Math.min(maxPanX, panX));
      panY = Math.max(-maxPanY, Math.min(maxPanY, panY));
    }

    function reset() {
      scale = 1;
      panX = 0;
      panY = 0;
      isDragging = false;
      hasDragged = false;
      if (currentMedia) {
        currentMedia.style.transition = 'none';
        currentMedia.style.transform = '';
        currentMedia.style.cursor = '';
      }
      if (zoomBadge) zoomBadge.style.display = 'none';
    }

    const onWheel = e => {
      if (!currentMedia || currentMedia.tagName !== 'IMG') return;
      e.preventDefault();
      const zoomFactor = e.deltaY < 0 ? 1.25 : (1 / 1.25);
      const prevScale = scale;
      let nextScale = scale * zoomFactor;
      if (nextScale < 1.05) {
        scale = 1;
        panX = 0;
        panY = 0;
        applyTransform(true);
        return;
      }
      if (nextScale > 6) {
        nextScale = 6;
      }
      const stage = lb.querySelector('.lb-stage');
      const stageRect = stage ? stage.getBoundingClientRect() : null;
      const cx = (stageRect ? (stageRect.left + stageRect.width / 2) : (window.innerWidth / 2)) + panX;
      const cy = (stageRect ? (stageRect.top + stageRect.height / 2) : (window.innerHeight / 2)) + panY;
      const dx = e.clientX - cx;
      const dy = e.clientY - cy;
      const ratio = nextScale / prevScale;
      panX -= dx * (ratio - 1);
      panY -= dy * (ratio - 1);
      scale = nextScale;
      clampPan();
      applyTransform(true);
    };

    lb.addEventListener('wheel', onWheel, { passive: false });

    const onMouseMove = e => {
      if (!isDragging || !currentMedia) return;
      hasDragged = true;
      panX = e.clientX - startX;
      panY = e.clientY - startY;
      clampPan();
      applyTransform(false);
    };

    const onMouseUp = () => {
      if (isDragging) {
        isDragging = false;
        window.removeEventListener('mousemove', onMouseMove);
        window.removeEventListener('mouseup', onMouseUp);
        applyTransform(false);
      }
    };

    const onMouseDown = e => {
      if (e.button !== 0 || !currentMedia) return;
      if (scale > 1) {
        isDragging = true;
        hasDragged = false;
        startX = e.clientX - panX;
        startY = e.clientY - panY;
        currentMedia.style.cursor = 'grabbing';
        window.addEventListener('mousemove', onMouseMove);
        window.addEventListener('mouseup', onMouseUp);
        e.preventDefault();
      }
    };

    const onClick = e => {
      if (hasDragged || !currentMedia) return;
      e.stopPropagation();
      if (scale === 1) {
        scale = 2.5;
        const stage = lb.querySelector('.lb-stage');
        const stageRect = stage ? stage.getBoundingClientRect() : null;
        const cx = stageRect ? (stageRect.left + stageRect.width / 2) : (window.innerWidth / 2);
        const cy = stageRect ? (stageRect.top + stageRect.height / 2) : (window.innerHeight / 2);
        panX = -(e.clientX - cx) * 1.5;
        panY = -(e.clientY - cy) * 1.5;
        clampPan();
        applyTransform(true);
      } else {
        scale = 1;
        panX = 0;
        panY = 0;
        applyTransform(true);
      }
    };

    zoomBadge.addEventListener('click', e => {
      e.stopPropagation();
      scale = 1;
      panX = 0;
      panY = 0;
      applyTransform(true);
    });

    return {
      attach: (mediaEl) => {
        reset();
        currentMedia = mediaEl;
        if (mediaEl && mediaEl.tagName === 'IMG') {
          mediaEl.draggable = false;
          mediaEl.addEventListener('mousedown', onMouseDown);
          mediaEl.addEventListener('click', onClick);
        }
      },
      reset: reset,
      destroy: () => {
        reset();
        window.removeEventListener('mousemove', onMouseMove);
        window.removeEventListener('mouseup', onMouseUp);
      }
    };
  }

  function _createLightboxMediaElement(msg) {
    const src = MEDIA + msg.archive_path;
    const mt = msg.media_type;
    let media;
    if ((mt === 'video' || mt === 'gif') && !src.toLowerCase().endsWith('.gif')) {
      media = document.createElement('video');
      media.controls = (mt === 'video');
      media.autoplay = true;
      media.loop = true;
      media.muted = (mt === 'gif');
      media.playsInline = true;
      media.src = src;
      if (msg.is_video_note || isVideoNote(msg)) {
        media.classList.add('lb-video-note');
      }
    } else {
      media = document.createElement('img');
      media.src = src;
    }
    return media;
  }

  function _preloadAdjacentLightboxMedia(index) {
    const nextIdx = _nextLightboxIndex(index, 1);
    if (nextIdx !== -1) {
      const nextMsg = lightboxItems[nextIdx];
      if (nextMsg && !nextMsg.is_missing && (nextMsg.media_type === 'image' || nextMsg.media_type === 'sticker')) {
        const img = new Image();
        img.src = MEDIA + nextMsg.archive_path;
      }
    }
    const prevIdx = _nextLightboxIndex(index, -1);
    if (prevIdx !== -1) {
      const prevMsg = lightboxItems[prevIdx];
      if (prevMsg && !prevMsg.is_missing && (prevMsg.media_type === 'image' || prevMsg.media_type === 'sticker')) {
        const img = new Image();
        img.src = MEDIA + prevMsg.archive_path;
      }
    }
  }

  function _setupLightboxLivePhoto(lb, stage, msg, media) {
    const oldBtn = lb.querySelector('.lb-live-btn');
    if (oldBtn) oldBtn.remove();
    const oldLiveVid = stage.querySelector('.lb-live-video');
    if (oldLiveVid) {
      oldLiveVid.pause();
      oldLiveVid.remove();
    }

    if (!msg.is_live_photo || !msg.live_photo_path) return;

    const liveBtn = document.createElement('button');
    liveBtn.className = 'lb-live-btn';
    liveBtn.innerHTML = '<span class="live-icon">◎</span> LIVE';
    liveBtn.title = 'Toggle Live Photo playback';
    let isPlaying = false;
    let lbVid = null;
    liveBtn.addEventListener('click', e => {
      e.stopPropagation();
      isPlaying = !isPlaying;
      if (isPlaying) {
        liveBtn.classList.add('active');
        if (!lbVid) {
          lbVid = document.createElement('video');
          lbVid.className = 'lb-live-video';
          lbVid.src = MEDIA + msg.live_photo_path;
          lbVid.autoplay = true;
          lbVid.loop = true;
          lbVid.muted = true;
          lbVid.playsInline = true;
        }
        media.style.display = 'none';
        stage.appendChild(lbVid);
      } else {
        liveBtn.classList.remove('active');
        if (lbVid) {
          lbVid.pause();
          lbVid.remove();
          lbVid = null;
        }
        media.style.display = '';
      }
    });
    lb.appendChild(liveBtn);
  }

  async function _transitionLightboxMedia(lb, stage, msg, dir, seq) {
    const incomingMedia = _createLightboxMediaElement(msg);

    if (dir === 0) {
      stage.innerHTML = '';
      stage.appendChild(incomingMedia);
      _setupLightboxLivePhoto(lb, stage, msg, incomingMedia);
      lbZoomController?.attach(incomingMedia);
      return;
    }

    if (incomingMedia instanceof HTMLImageElement && incomingMedia.decode) {
      try {
        await Promise.race([
          incomingMedia.decode(),
          new Promise(resolve => setTimeout(resolve, 80))
        ]);
      } catch (_) {
        // Fall back without blocking if decoding fails or aborts
      }
    }

    if (seq !== lightboxNavSeq) return;

    stage.querySelectorAll('.lb-media-exiting').forEach(el => el.remove());

    const outgoing = stage.querySelector('img:not(.lb-media-exiting), video:not(.lb-media-exiting)');
    if (outgoing) {
      if (outgoing.tagName === 'VIDEO') {
        outgoing.pause();
      }
      outgoing.classList.add('lb-media-exiting');
      outgoing.style.transform = dir > 0 ? 'translateX(-40px)' : 'translateX(40px)';
      outgoing.style.opacity = '0';
      let cleanedOutgoing = false;
      let cleanTimer = null;
      const cleanupOutgoing = () => {
        if (cleanedOutgoing) return;
        cleanedOutgoing = true;
        if (cleanTimer !== null) {
          clearTimeout(cleanTimer);
          cleanTimer = null;
        }
        if (outgoing.isConnected) outgoing.remove();
      };
      outgoing.addEventListener('transitionend', cleanupOutgoing, { once: true });
      cleanTimer = setTimeout(cleanupOutgoing, 250);
    }

    incomingMedia.classList.add('lb-media-entering');
    incomingMedia.style.transform = dir > 0 ? 'translateX(40px)' : 'translateX(-40px)';
    incomingMedia.style.opacity = '0';
    stage.appendChild(incomingMedia);

    _setupLightboxLivePhoto(lb, stage, msg, incomingMedia);

    requestAnimationFrame(() => {
      if (seq !== lightboxNavSeq) return;
      incomingMedia.style.transform = 'translateX(0)';
      incomingMedia.style.opacity = '1';
    });

    let finalized = false;
    let finalizeTimer = null;
    const finalizeIncoming = () => {
      if (finalized) return;
      finalized = true;
      if (finalizeTimer !== null) {
        clearTimeout(finalizeTimer);
        finalizeTimer = null;
      }
      if (seq !== lightboxNavSeq) return;
      incomingMedia.classList.remove('lb-media-entering');
      incomingMedia.style.transform = '';
      incomingMedia.style.opacity = '';
      incomingMedia.style.transition = '';
      lbZoomController?.attach(incomingMedia);
    };
    incomingMedia.addEventListener('transitionend', finalizeIncoming, { once: true });
    finalizeTimer = setTimeout(finalizeIncoming, 250);
  }

  function openLightboxAt(index, dir) {
    const msg = lightboxItems[index];
    if (!msg) return;

    const sep = outputRoot.includes('\\') ? '\\' : '/';
    const fullPath = outputRoot.replace(/[/\\]+$/, '') + sep + msg.archive_path.replace(/\//g, sep);

    let lb = document.getElementById('img-lightbox');
    const isNewLb = !lb;

    if (isNewLb) {
      lb = document.createElement('div');
      lb.id = 'img-lightbox';
      lb.className = 'img-lightbox';

      const closeBtn = document.createElement('button');
      closeBtn.className = 'lb-close';
      closeBtn.innerHTML = '&#10005;';
      closeBtn.title = 'Close';
      closeBtn.addEventListener('click', () => _closeLb());
      lb.appendChild(closeBtn);

      const prevBtn = document.createElement('button');
      prevBtn.className = 'lb-arrow prev';
      prevBtn.innerHTML = '&#10094;';
      prevBtn.title = 'Previous';
      prevBtn.addEventListener('click', e => {
        e.stopPropagation();
        const i = _nextLightboxIndex(lightboxIndex, -1);
        if (i !== -1) { lightboxIndex = i; openLightboxAt(lightboxIndex, -1); }
      });
      lb.appendChild(prevBtn);

      const nextBtn = document.createElement('button');
      nextBtn.className = 'lb-arrow next';
      nextBtn.innerHTML = '&#10095;';
      nextBtn.title = 'Next';
      nextBtn.addEventListener('click', async e => {
        e.stopPropagation();
        let i = _nextLightboxIndex(lightboxIndex, 1);
        if (i !== -1) {
          lightboxIndex = i; openLightboxAt(lightboxIndex, 1);
        } else if (lightboxContext === 'gallery' && !galleryAllLoaded) {
          const oldest = currentGalleryItems[currentGalleryItems.length - 1]?.timestamp_ms;
          await _loadGalleryPage(oldest);
          i = _nextLightboxIndex(lightboxIndex, 1);
          if (i !== -1) { lightboxIndex = i; openLightboxAt(lightboxIndex, 1); }
        }
      });
      lb.appendChild(nextBtn);

      const tsEl = document.createElement('div');
      tsEl.className = 'lb-timestamp';
      lb.appendChild(tsEl);

      const stage = document.createElement('div');
      stage.className = 'lb-stage';
      lb.appendChild(stage);

      const pathRow = document.createElement('div');
      pathRow.className = 'lb-path';
      const pathText = document.createElement('span');
      const copyBtn = document.createElement('button');
      copyBtn.className = 'lb-copy-btn';
      copyBtn.title = 'Copy path';
      copyBtn.innerHTML = '&#128203;';
      copyBtn.addEventListener('click', e => {
        e.stopPropagation();
        const curMsg = lightboxItems[lightboxIndex];
        if (!curMsg) return;
        const curFullPath = outputRoot.replace(/[/\\]+$/, '') + sep + curMsg.archive_path.replace(/\//g, sep);
        navigator.clipboard.writeText(curFullPath).then(() => {
          copyBtn.classList.add('copied');
          setTimeout(() => copyBtn.classList.remove('copied'), 1500);
        });
      });
      pathRow.appendChild(pathText);
      pathRow.appendChild(copyBtn);
      lb.appendChild(pathRow);

      lb.addEventListener('click', e => { if (e.target === lb || e.target === stage) _closeLb(); });
      document.body.appendChild(lb);

      lbZoomController = _setupLightboxZoom(lb);
    } else {
      lbZoomController?.reset();
    }

    const tsEl = lb.querySelector('.lb-timestamp');
    if (tsEl) tsEl.textContent = fmtTime(msg.timestamp_ms);

    const pathText = lb.querySelector('.lb-path span');
    if (pathText) pathText.textContent = fullPath;

    const prevBtn = lb.querySelector('.lb-arrow.prev');
    if (prevBtn) prevBtn.disabled = _nextLightboxIndex(index, -1) === -1;

    const nextBtn = lb.querySelector('.lb-arrow.next');
    if (nextBtn) {
      nextBtn.disabled = _nextLightboxIndex(index, 1) === -1 &&
        (lightboxContext !== 'gallery' || galleryAllLoaded);
    }

    const stage = lb.querySelector('.lb-stage');
    const seq = ++lightboxNavSeq;
    _transitionLightboxMedia(lb, stage, msg, isNewLb ? 0 : dir, seq);
    _preloadAdjacentLightboxMedia(index);
  }

  function showLightbox(msg) {
    if (!msg || msg.is_missing) return;
    // Navigate the chat's currently-loaded media (chronological), not the stale
    // gallery list. msg is the same object reference held in msgList, so identity
    // lookup gives its position. Falls back to a single-item view if not found.
    lightboxContext = 'chat';
    lightboxItems = msgList.filter(isLightboxMedia);
    lightboxIndex = lightboxItems.indexOf(msg);
    if (lightboxIndex === -1) {
      lightboxItems = [msg];
      lightboxIndex = 0;
    }
    openLightboxAt(lightboxIndex, 0);
  }
