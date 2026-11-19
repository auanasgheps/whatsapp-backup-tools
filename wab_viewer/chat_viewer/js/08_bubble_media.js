function renderGif(msg) {
    const wrap = document.createElement('div');
    wrap.className = 'msg-media msg-gif';
    wrap.setAttribute('tabindex', '0');
    wrap.setAttribute('aria-label', 'Animated GIF: click to play');

    const ratio = _extractMsgAspectRatio(msg);
    _applyMediaOrientationBox(wrap, ratio);

    const src = '/media/' + msg.archive_path;
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
    vid.src = src;

    const probe = document.createElement('video');
    probe.src = src;
    probe.muted = true;
    probe.preload = 'metadata';
    probe.addEventListener('loadeddata', () => {
      if (ratio <= 0 && probe.videoWidth > 0 && probe.videoHeight > 0) {
        _applyMediaOrientationBox(wrap, probe.videoWidth / probe.videoHeight);
      }
      const canvas = document.createElement('canvas');
      canvas.width = probe.videoWidth;
      canvas.height = probe.videoHeight;
      canvas.getContext('2d').drawImage(probe, 0, 0);
      vid.poster = canvas.toDataURL('image/jpeg', 0.8);
      probe.src = '';
      const scroll = document.getElementById('message-scroll');
      if (scroll && isUserAtBottom) {
        suppressScroll = true;
        scroll.scrollTop = scroll.scrollHeight;
        suppressScroll = false;
      }
    }, { once: true });
    probe.addEventListener('error', () => { probe.src = ''; }, { once: true });

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
        wrap.setAttribute('aria-label', 'Animated GIF: click to play');
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
      voBadge.innerHTML = '<span class="vo-icon">①</span> View Once';
      wrap.appendChild(voBadge);
    }

    return wrap;
  }

  const VIDEO_NOTE_RING_CIRCUMFERENCE = 2 * Math.PI * 48;
