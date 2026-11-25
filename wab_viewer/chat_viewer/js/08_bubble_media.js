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

  const VIDEO_NOTE_RING_CIRCUMFERENCE = 2 * Math.PI * 48;
