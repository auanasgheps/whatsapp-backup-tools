function renderGif(msg) {
    const wrap = document.createElement('div');
    wrap.className = 'msg-media msg-gif';
    wrap.setAttribute('tabindex', '0');
    wrap.setAttribute('aria-label', t('msg.gif_play'));

    const ratio = _extractMsgAspectRatio(msg);
    _applyMediaOrientationBox(wrap, ratio);

    const src = MEDIA + msg.archive_path;
    const isImageGif = (msg.archive_path || '').toLowerCase().endsWith('.gif');
    if (isImageGif) {
      const img = document.createElement('img');
      img.src = src;
      img.loading = 'lazy';
      if (ratio <= 0) {
        img.addEventListener('load', () => {
          if (img.naturalWidth > 0 && img.naturalHeight > 0) {
            _applyMediaOrientationBox(wrap, img.naturalWidth / img.naturalHeight);
          }
        }, { once: true });
      }
      img.addEventListener('click', () => showLightbox(msg));
      wrap.appendChild(img);
      return wrap;
    }

    const vid = document.createElement('video');
    vid.className = 'msg-gif-video';
    vid.preload = 'metadata';
    vid.muted = true;
    vid.playsInline = true;
    vid.loop = true;
    vid.src = src.includes('#') ? src : `${src}#t=0.001`;

    const handleGifError = () => {
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
      const scroll = document.getElementById('message-scroll');
      if (scroll && isUserAtBottom) {
        suppressScroll = true;
        scroll.scrollTop = scroll.scrollHeight;
        suppressScroll = false;
      }
    };
    vid.addEventListener('error', handleGifError, { once: true });

    vid.addEventListener('loadedmetadata', () => {
      if (ratio <= 0 && vid.videoWidth > 0 && vid.videoHeight > 0) {
        _applyMediaOrientationBox(wrap, vid.videoWidth / vid.videoHeight);
      }
      if (vid.currentTime === 0) {
        vid.currentTime = 0.001;
      }
    }, { once: true });

    vid.addEventListener('loadeddata', () => {
      const scroll = document.getElementById('message-scroll');
      if (scroll && isUserAtBottom) {
        suppressScroll = true;
        scroll.scrollTop = scroll.scrollHeight;
        suppressScroll = false;
      }
    }, { once: true });

    const badgeWrap = document.createElement('div');
    badgeWrap.className = 'msg-gif-badge-wrap';
    badgeWrap.setAttribute('aria-hidden', 'true');
    const badge = document.createElement('div');
    badge.className = 'msg-gif-badge';
    badge.textContent = 'GIF';
    badgeWrap.appendChild(badge);

    function toggleGif() {
      if (vid.paused) {
        vid.play().then(() => {
          wrap.classList.add('is-playing');
          wrap.setAttribute('aria-label', 'Animated GIF: click to pause');
        }).catch(() => {});
      } else {
        vid.pause();
        wrap.classList.remove('is-playing');
        wrap.setAttribute('aria-label', t('msg.gif_play'));
      }
    }

    wrap.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleGif();
    });

    wrap.addEventListener('keydown', (e) => {
      if (e.key === ' ' || e.key === 'Enter') {
        e.preventDefault();
        e.stopPropagation();
        toggleGif();
      }
    });

    if (gifViewportObserver) {
      gifViewportObserver.observe(vid);
    }

    wrap.appendChild(vid);
    wrap.appendChild(badgeWrap);

    if (msg.is_view_once) {
      const voBadge = document.createElement('span');
      voBadge.className = 'msg-view-once-badge';
      voBadge.innerHTML = `<span class="vo-icon">①</span> ${esc(t('msg.view_once'))}`;
      wrap.appendChild(voBadge);
    }

    return wrap;
  }

  function enqueueVideoFrameProbe(probeItem) {
    videoProbeQueue.push(probeItem);
    drainVideoProbeQueue();
  }

  function drainVideoProbeQueue() {
    while (activeVideoProbes < MAX_CONCURRENT_VIDEO_PROBES && videoProbeQueue.length > 0) {
      const next = videoProbeQueue.shift();
      if (!next.isAlive()) {
        continue;
      }
      if (videoThumbCache.has(next.archivePath)) {
        next.onCached(videoThumbCache.get(next.archivePath));
        continue;
      }
      activeVideoProbes++;
      _runVideoFrameProbe(next);
    }
  }

  function _runVideoFrameProbe(task) {
    const vid = document.createElement('video');
    vid.src = task.src;
    vid.muted = true;
    vid.playsInline = true;
    vid.preload = 'auto';
    let settled = false;

    const safetyTimeout = setTimeout(() => {
      task.onError();
      finish();
    }, 6000);

    function finish() {
      if (settled) return;
      settled = true;
      clearTimeout(safetyTimeout);
      vid.src = '';
      activeVideoProbes--;
      drainVideoProbeQueue();
    }

    function captureFrame() {
      try {
        if (vid.videoWidth <= 0 || vid.videoHeight <= 0) {
          task.onError();
          return;
        }
        const canvas = document.createElement('canvas');
        canvas.width = vid.videoWidth;
        canvas.height = vid.videoHeight;
        canvas.getContext('2d').drawImage(vid, 0, 0, canvas.width, canvas.height);
        const dataUrl = canvas.toDataURL('image/jpeg', 0.8);
        videoThumbCache.set(task.archivePath, dataUrl);
        task.onFrame(dataUrl);
      } catch (_e) {
        task.onError();
      } finally {
        finish();
      }
    }

    vid.addEventListener('loadeddata', () => {
      captureFrame();
    }, { once: true });

    vid.addEventListener('error', () => {
      try {
        task.onError();
      } finally {
        finish();
      }
    }, { once: true });
  }

  const chatVideoPosterObserver = typeof IntersectionObserver !== 'undefined'
    ? new IntersectionObserver((entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const el = entry.target;
          chatVideoPosterObserver.unobserve(el);
          const pending = el._pendingVideoPreview;
          if (!pending) continue;
          delete el._pendingVideoPreview;
          _startVideoPreviewProbe(pending);
        }
      }, {
        root: document.getElementById('message-scroll'),
        rootMargin: '400px 0px',
      })
    : null;

  function _startVideoPreviewProbe(task) {
    if (!task.isAlive()) return;
    if (videoThumbCache.has(task.archivePath)) {
      task.onPreview(videoThumbCache.get(task.archivePath));
      return;
    }

    const queueFirstFrameProbe = () => {
      if (!task.isAlive()) return;
      if (videoProbeQueue.some(item => item.targetEl === task.targetEl)) return;
      enqueueVideoFrameProbe({
        targetEl: task.targetEl,
        src: task.src,
        archivePath: task.archivePath,
        isAlive: task.isAlive,
        onCached: (dataUrl) => {
          if (task.isAlive()) task.onPreview(dataUrl);
        },
        onFrame: (dataUrl) => {
          if (task.isAlive()) task.onPreview(dataUrl);
        },
        onError: () => {},
      });
    };

    if (task.thumbUrl) {
      const probeImg = new Image();
      probeImg.onload = () => {
        if (task.isAlive()) {
          task.onPreview(task.thumbUrl);
        }
      };
      probeImg.onerror = () => {
        queueFirstFrameProbe();
      };
      probeImg.src = task.thumbUrl;
      return;
    }

    queueFirstFrameProbe();
  }

  function resolveVideoPreview(targetEl, src, archivePath, thumbUrl, isAlive, onPreview) {
    if (!targetEl || !src || !archivePath) return;
    if (videoThumbCache.has(archivePath)) {
      onPreview(videoThumbCache.get(archivePath));
      return;
    }
    const task = { targetEl, src, archivePath, thumbUrl, isAlive, onPreview };
    if (chatVideoPosterObserver) {
      targetEl._pendingVideoPreview = task;
      chatVideoPosterObserver.observe(targetEl);
    } else {
      _startVideoPreviewProbe(task);
    }
  }

  function attachVideoPoster(vid, src, archivePath, thumbUrl) {
    if (!vid || !src || !archivePath) return;

    // If the player itself decodes a frame first (e.g., user hits play), capture and cache it.
    vid.addEventListener('loadeddata', () => {
      if (!vid.poster && vid.videoWidth > 0 && vid.videoHeight > 0) {
        try {
          const canvas = document.createElement('canvas');
          canvas.width = vid.videoWidth;
          canvas.height = vid.videoHeight;
          canvas.getContext('2d').drawImage(vid, 0, 0, canvas.width, canvas.height);
          const dataUrl = canvas.toDataURL('image/jpeg', 0.8);
          videoThumbCache.set(archivePath, dataUrl);
          vid.poster = dataUrl;
        } catch (_e) {
          // Ignore canvas errors if frame capture fails
        }
      }
    }, { once: true });

    resolveVideoPreview(
      vid,
      src,
      archivePath,
      thumbUrl,
      () => vid.isConnected && !vid.poster,
      (previewUrl) => {
        if (vid.isConnected && !vid.poster) {
          vid.poster = previewUrl;
        }
      },
    );
  }

  const VIDEO_NOTE_RING_CIRCUMFERENCE = 2 * Math.PI * 48;
