function renderVideoNote(msg) {
    const wrap = document.createElement('div');
    wrap.className = 'msg-media msg-video-note';
    wrap.setAttribute('tabindex', '0');

    const totalSec = msg.media_duration && msg.media_duration > 0 ? msg.media_duration : 0;
    const baseAriaLabel = totalSec > 0
      ? `Video message, ${_formatTimer(totalSec)}`
      : 'Video message';
    wrap.setAttribute('aria-label', `${baseAriaLabel}, click to play`);

    const src = '/media/' + msg.archive_path;

    const vid = document.createElement('video');
    vid.className = 'video-note-video';
    vid.playsInline = true;
    vid.preload = 'metadata';
    vid.src = src;

    vid.addEventListener('loadeddata', () => {
      if (!vid.dataset.posterCaptured && vid.videoWidth > 0 && vid.videoHeight > 0) {
        vid.dataset.posterCaptured = '1';
        try {
          const canvas = document.createElement('canvas');
          canvas.width = vid.videoWidth;
          canvas.height = vid.videoHeight;
          canvas.getContext('2d').drawImage(vid, 0, 0);
          vid.poster = canvas.toDataURL('image/jpeg', 0.8);
        } catch (_e) {
          // Ignore canvas errors if frame capture fails
        }
      }
      if (!totalSec && Number.isFinite(vid.duration) && vid.duration > 0) {
        durText.textContent = _formatTimer(Math.round(vid.duration));
      }
      const scroll = document.getElementById('message-scroll');
      if (scroll && isUserAtBottom) {
        suppressScroll = true;
        scroll.scrollTop = scroll.scrollHeight;
        suppressScroll = false;
      }
    }, { once: true });

    const svgNS = 'http://www.w3.org/2000/svg';
    const ringSvg = document.createElementNS(svgNS, 'svg');
    ringSvg.setAttribute('class', 'video-note-ring');
    ringSvg.setAttribute('viewBox', '0 0 100 100');
    ringSvg.setAttribute('aria-hidden', 'true');

    const trackCircle = document.createElementNS(svgNS, 'circle');
    trackCircle.setAttribute('class', 'video-note-ring-track');
    trackCircle.setAttribute('cx', '50');
    trackCircle.setAttribute('cy', '50');
    trackCircle.setAttribute('r', '48');

    const progCircle = document.createElementNS(svgNS, 'circle');
    progCircle.setAttribute('class', 'video-note-ring-progress');
    progCircle.setAttribute('cx', '50');
    progCircle.setAttribute('cy', '50');
    progCircle.setAttribute('r', '48');
    progCircle.style.strokeDasharray = VIDEO_NOTE_RING_CIRCUMFERENCE.toFixed(2);
    progCircle.style.strokeDashoffset = VIDEO_NOTE_RING_CIRCUMFERENCE.toFixed(2);

    ringSvg.appendChild(trackCircle);
    ringSvg.appendChild(progCircle);

    const playWrap = document.createElement('div');
    playWrap.className = 'video-note-play-wrap';
    playWrap.setAttribute('aria-hidden', 'true');
    const playBtn = document.createElement('div');
    playBtn.className = 'video-note-play-btn';
    playBtn.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><polygon points="7,4 20,12 7,20"/></svg>';
    playWrap.appendChild(playBtn);

    const durBadge = document.createElement('div');
    durBadge.className = 'video-note-duration';
    durBadge.setAttribute('aria-hidden', 'true');
    const durIcon = document.createElement('span');
    durIcon.className = 'video-note-cam-icon';
    durIcon.innerHTML = '<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><path d="M17 10.5V7a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-3.5l4 4v-11l-4 4z"/></svg>';
    const durText = document.createElement('span');
    durText.textContent = totalSec > 0 ? _formatTimer(totalSec) : '0:00';
    durBadge.appendChild(durIcon);
    durBadge.appendChild(durText);

    const actions = document.createElement('div');
    actions.className = 'video-note-actions';

    const muteBtn = document.createElement('button');
    muteBtn.type = 'button';
    muteBtn.className = 'video-note-action-btn';
    muteBtn.title = 'Mute';
    muteBtn.setAttribute('aria-label', 'Mute video message');
    function syncMuteIcon() {
      if (vid.muted) {
        muteBtn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>';
        muteBtn.title = 'Unmute';
        muteBtn.setAttribute('aria-label', 'Unmute video message');
      } else {
        muteBtn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>';
        muteBtn.title = 'Mute';
        muteBtn.setAttribute('aria-label', 'Mute video message');
      }
    }
    syncMuteIcon();
    muteBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      vid.muted = !vid.muted;
      syncMuteIcon();
    });

    const enlargeBtn = document.createElement('button');
    enlargeBtn.type = 'button';
    enlargeBtn.className = 'video-note-action-btn';

    function syncEnlargeIcon() {
      const isEnlarged = wrap.classList.contains('is-enlarged');
      if (isEnlarged) {
        enlargeBtn.title = 'Shrink';
        enlargeBtn.setAttribute('aria-label', 'Shrink video message');
        enlargeBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/><line x1="14" y1="10" x2="21" y2="3"/><line x1="3" y1="21" x2="10" y2="14"/></svg>';
      } else {
        enlargeBtn.title = 'Enlarge';
        enlargeBtn.setAttribute('aria-label', 'Enlarge video message');
        enlargeBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>';
      }
    }
    syncEnlargeIcon();

    enlargeBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const isEnlarged = wrap.classList.toggle('is-enlarged');
      const bubble = wrap.closest('.msg-bubble');
      if (bubble) {
        bubble.classList.toggle('has-video-note-enlarged', isEnlarged);
      }
      syncEnlargeIcon();
      const scroll = document.getElementById('message-scroll');
      if (scroll && isUserAtBottom) {
        requestAnimationFrame(() => {
          suppressScroll = true;
          scroll.scrollTop = scroll.scrollHeight;
          suppressScroll = false;
        });
      }
    });

    actions.appendChild(muteBtn);
    actions.appendChild(enlargeBtn);

    let rafId = null;

    function updateRingAndTimer() {
      const dur = Number.isFinite(vid.duration) && vid.duration > 0 ? vid.duration : totalSec;
      if (dur > 0) {
        const progress = Math.min(1, Math.max(0, vid.currentTime / dur));
        progCircle.style.strokeDashoffset = (VIDEO_NOTE_RING_CIRCUMFERENCE * (1 - progress)).toFixed(2);
        const remaining = Math.max(0, Math.ceil(dur - vid.currentTime));
        durText.textContent = _formatTimer(remaining);
      }
      if (!vid.paused && !vid.ended) {
        rafId = requestAnimationFrame(updateRingAndTimer);
      }
    }

    function pauseVideoNote() {
      if (rafId !== null) {
        cancelAnimationFrame(rafId);
        rafId = null;
      }
      if (!vid.paused) {
        vid.pause();
      }
      wrap.classList.remove('is-playing');
      wrap.setAttribute('aria-label', `${baseAriaLabel}, click to play`);
      if (activeVideoNotePause === pauseVideoNote) {
        activeVideoNotePause = null;
      }
    }

    function playVideoNote() {
      stopActiveVoiceNote();
      if (activeVideoNotePause && activeVideoNotePause !== pauseVideoNote) {
        activeVideoNotePause();
      }
      activeVideoNotePause = pauseVideoNote;
      vid.play().then(() => {
        wrap.classList.add('is-playing');
        wrap.setAttribute('aria-label', `${baseAriaLabel}, click to pause`);
        rafId = requestAnimationFrame(updateRingAndTimer);
      }).catch(() => {
        if (activeVideoNotePause === pauseVideoNote) {
          activeVideoNotePause = null;
        }
      });
    }

    function toggleVideoNote() {
      if (vid.paused || vid.ended) {
        playVideoNote();
      } else {
        pauseVideoNote();
      }
    }

    vid.addEventListener('ended', () => {
      pauseVideoNote();
      vid.currentTime = 0;
      progCircle.style.strokeDashoffset = VIDEO_NOTE_RING_CIRCUMFERENCE.toFixed(2);
      const dur = Number.isFinite(vid.duration) && vid.duration > 0 ? Math.round(vid.duration) : totalSec;
      durText.textContent = dur > 0 ? _formatTimer(dur) : '0:00';
    });

    wrap._pauseVideoNote = pauseVideoNote;
    if (videoNoteViewportObserver) {
      videoNoteViewportObserver.observe(wrap);
    }

    wrap.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleVideoNote();
    });

    wrap.addEventListener('keydown', (e) => {
      if (e.key === ' ' || e.key === 'Enter') {
        e.preventDefault();
        e.stopPropagation();
        toggleVideoNote();
      }
    });

    wrap.appendChild(vid);
    wrap.appendChild(ringSvg);
    wrap.appendChild(playWrap);
    wrap.appendChild(durBadge);
    wrap.appendChild(actions);

    if (msg.is_view_once) {
      const voBadge = document.createElement('span');
      voBadge.className = 'msg-view-once-badge';
      voBadge.innerHTML = '<span class="vo-icon">①</span> View Once';
      wrap.appendChild(voBadge);
    }

    return wrap;
  }

  const VOICE_PLAY_SVG = '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" style="transform: translateX(1px); display: block;"><polygon points="6,4 20,12 6,20"/></svg>';
  const VOICE_PAUSE_SVG = '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="4" width="4" height="16" rx="1"/><rect x="15" y="4" width="4" height="16" rx="1"/></svg>';

  const waveformCache = new Map(); // src -> number[]
  const decodeQueue = [];
  let activeDecodeCount = 0;
  const MAX_CONCURRENT_DECODES = 3;
  let sharedAudioContext = null;
