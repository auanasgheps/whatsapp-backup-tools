function getSharedAudioContext() {
    if (!sharedAudioContext) {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (AudioCtx) {
        sharedAudioContext = new AudioCtx();
      }
    }
    if (sharedAudioContext && sharedAudioContext.state === 'suspended') {
      sharedAudioContext.resume().catch(() => {});
    }
    return sharedAudioContext;
  }

  function extractWaveformFromBuffer(audioBuffer, numBars = 64) {
    const pcm = audioBuffer.getChannelData(0);
    const blockSize = Math.floor(pcm.length / numBars);
    if (blockSize <= 0) {
      return new Array(numBars).fill(15);
    }
    const peaks = new Array(numBars);
    let maxPeak = 0;
    for (let i = 0; i < numBars; i++) {
      const start = i * blockSize;
      const end = i === numBars - 1 ? pcm.length : start + blockSize;
      let peak = 0;
      for (let j = start; j < end; j++) {
        const val = Math.abs(pcm[j]);
        if (val > peak) peak = val;
      }
      peaks[i] = peak;
      if (peak > maxPeak) maxPeak = peak;
    }
    const normalized = new Array(numBars);
    const factor = maxPeak > 0 ? (100 / maxPeak) : 0;
    for (let i = 0; i < numBars; i++) {
      normalized[i] = Math.max(10, Math.min(100, Math.round(peaks[i] * factor)));
    }
    return normalized;
  }

  function fetchAndDecodeWaveform(src) {
    if (waveformCache.has(src)) {
      return Promise.resolve(waveformCache.get(src));
    }
    return new Promise((resolve) => {
      decodeQueue.push({ src, resolve });
      processDecodeQueue();
    });
  }

  function processDecodeQueue() {
    if (activeDecodeCount >= MAX_CONCURRENT_DECODES || decodeQueue.length === 0) {
      return;
    }
    const task = decodeQueue.shift();
    if (waveformCache.has(task.src)) {
      task.resolve(waveformCache.get(task.src));
      processDecodeQueue();
      return;
    }
    activeDecodeCount++;
    fetch(task.src)
      .then((res) => {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.arrayBuffer();
      })
      .then((buf) => {
        const ctx = getSharedAudioContext();
        if (!ctx) throw new Error('AudioContext unavailable');
        return ctx.decodeAudioData(buf);
      })
      .then((audioBuf) => {
        const wave = extractWaveformFromBuffer(audioBuf, 64);
        waveformCache.set(task.src, wave);
        task.resolve(wave);
      })
      .catch((err) => {
        console.debug('Waveform decode fallback:', task.src, err.message);
        task.resolve(null);
      })
      .finally(() => {
        activeDecodeCount--;
        processDecodeQueue();
      });
  }

  let activeVoiceNoteStop = null;

  function stopActiveVoiceNote() {
    if (activeVoiceNoteStop) {
      activeVoiceNoteStop();
      activeVoiceNoteStop = null;
    }
  }

  let voiceNoteViewportObserver = null;
  if (typeof IntersectionObserver !== 'undefined') {
    voiceNoteViewportObserver = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          const wrap = entry.target;
          if (wrap._loadWaveform) {
            wrap._loadWaveform();
            delete wrap._loadWaveform;
          }
          voiceNoteViewportObserver.unobserve(wrap);
        }
      });
    }, { rootMargin: '250px 0px' });
  }

  function _formatAudioTime(sec) {
    const s = Math.max(0, Math.floor(Number(sec) || 0));
    const m = Math.floor(s / 60);
    const rem = s % 60;
    return `${m}:${rem.toString().padStart(2, '0')}`;
  }

  function renderVoiceNoteBubble(msg) {
    const wrap = document.createElement('div');
    wrap.className = 'msg-media audio voice-player';
    wrap.setAttribute('tabindex', '0');
    wrap.setAttribute('role', 'region');

    const hasAudioFile = Boolean(msg.archive_path && !msg.is_missing);
    let totalSec = (msg.media_duration && msg.media_duration > 0) ? Number(msg.media_duration) : 0;
    const baseAriaLabel = totalSec > 0
      ? `Voice message, ${_formatAudioTime(totalSec)}`
      : 'Voice message';
    wrap.setAttribute('aria-label', baseAriaLabel);

    if (!hasAudioFile) {
      wrap.classList.add('is-unavailable');
    }

    const audio = document.createElement('audio');
    audio.preload = 'metadata';
    if (hasAudioFile) {
      audio.src = MEDIA + msg.archive_path;
    }

    let savedRate = parseFloat(sessionStorage.getItem('wa_voice_playback_rate') || '1') || 1;
    if (![1, 1.2, 1.5, 2].includes(savedRate)) savedRate = 1;
    audio.playbackRate = savedRate;

    const mainRow = document.createElement('div');
    mainRow.className = 'voice-main-row';

    const playBtn = document.createElement('button');
    playBtn.type = 'button';
    playBtn.className = 'voice-play-btn';
    playBtn.setAttribute('aria-label', 'Play voice message');
    playBtn.innerHTML = VOICE_PLAY_SVG;
    if (!hasAudioFile) {
      playBtn.disabled = true;
    }

    const waveformContainer = document.createElement('div');
    waveformContainer.className = 'voice-waveform-container';
    waveformContainer.setAttribute('role', 'slider');
    waveformContainer.setAttribute('aria-label', 'Voice message timeline');
    waveformContainer.setAttribute('aria-valuemin', '0');
    waveformContainer.setAttribute('aria-valuemax', '100');
    waveformContainer.setAttribute('aria-valuenow', '0');
    waveformContainer.setAttribute('tabindex', '0');

    const waveformEl = document.createElement('div');
    waveformEl.className = 'voice-waveform';
    const barEls = [];
    for (let i = 0; i < 64; i++) {
      const bar = document.createElement('div');
      bar.className = 'voice-bar';
      bar.style.height = '4px'; // Honest flat baseline
      waveformEl.appendChild(bar);
      barEls.push(bar);
    }
    waveformContainer.appendChild(waveformEl);

    mainRow.appendChild(playBtn);
    mainRow.appendChild(waveformContainer);

    const subRow = document.createElement('div');
    subRow.className = 'voice-sub-row';

    const timeText = document.createElement('span');
    timeText.className = 'voice-time';
    timeText.textContent = !hasAudioFile
      ? (totalSec > 0 ? `${_formatAudioTime(totalSec)} (unavailable)` : 'Unavailable')
      : (totalSec > 0 ? _formatAudioTime(totalSec) : '0:00');

    const speedPill = document.createElement('button');
    speedPill.type = 'button';
    speedPill.className = 'voice-speed-pill';
    speedPill.textContent = `${savedRate}x`;
    speedPill.setAttribute('aria-label', `Playback speed ${savedRate}x`);

    subRow.appendChild(timeText);
    subRow.appendChild(speedPill);

    wrap.appendChild(mainRow);
    wrap.appendChild(subRow);

    function applyWaveformHeights(arr) {
      if (!Array.isArray(arr) || arr.length === 0) return;
      for (let i = 0; i < 64; i++) {
        const val = i < arr.length ? arr[i] : 15;
        const h = Math.max(4, Math.round((Math.min(100, Math.max(0, val)) / 100) * 26) + 2);
        barEls[i].style.height = `${h}px`;
      }
      wrap.classList.remove('is-loading');
    }

    const precomputedWave = msg.waveform || msg.audio_waveform;
    if (Array.isArray(precomputedWave) && precomputedWave.length > 0) {
      applyWaveformHeights(precomputedWave);
    } else if (hasAudioFile) {
      const srcUrl = MEDIA + msg.archive_path;
      if (waveformCache.has(srcUrl)) {
        applyWaveformHeights(waveformCache.get(srcUrl));
      } else {
        wrap.classList.add('is-loading');
        wrap._loadWaveform = () => {
          fetchAndDecodeWaveform(srcUrl).then((wave) => {
            if (wave) {
              applyWaveformHeights(wave);
            } else {
              wrap.classList.remove('is-loading');
            }
          }).catch(() => {
            wrap.classList.remove('is-loading');
          });
        };
        if (voiceNoteViewportObserver) {
          voiceNoteViewportObserver.observe(wrap);
        } else {
          wrap._loadWaveform();
          delete wrap._loadWaveform;
        }
      }
    }

    function updateProgress(currentTime, duration) {
      const dur = duration > 0 ? duration : (totalSec > 0 ? totalSec : 0);
      const progress = dur > 0 ? Math.min(1, Math.max(0, currentTime / dur)) : 0;
      const playedIndex = Math.floor(progress * 64);
      for (let i = 0; i < 64; i++) {
        if (i < playedIndex) {
          barEls[i].classList.add('is-played');
        } else {
          barEls[i].classList.remove('is-played');
        }
      }
      waveformContainer.setAttribute('aria-valuenow', Math.round(progress * 100).toString());

      if (wrap.classList.contains('is-playing') || currentTime > 0) {
        timeText.textContent = dur > 0
          ? `${_formatAudioTime(currentTime)} / ${_formatAudioTime(dur)}`
          : _formatAudioTime(currentTime);
      } else {
        timeText.textContent = dur > 0 ? _formatAudioTime(dur) : '0:00';
      }
    }

    function pausePlayback() {
      audio.pause();
      wrap.classList.remove('is-playing');
      playBtn.innerHTML = VOICE_PLAY_SVG;
      playBtn.setAttribute('aria-label', 'Play voice message');
      if (activeVoiceNoteStop === pausePlayback) {
        activeVoiceNoteStop = null;
      }
    }

    function startPlayback() {
      if (!hasAudioFile) return;

      if (wrap._loadWaveform) {
        wrap._loadWaveform();
        delete wrap._loadWaveform;
      }

      if (activeVideoNotePause) {
        activeVideoNotePause();
      }
      if (activeVoiceNoteStop && activeVoiceNoteStop !== pausePlayback) {
        activeVoiceNoteStop();
      }
      activeVoiceNoteStop = pausePlayback;

      if (currentGalleryAudio && !currentGalleryAudio.paused) {
        currentGalleryAudio.pause();
        if (currentGalleryAudioCell) currentGalleryAudioCell.classList.remove('is-playing');
      }

      audio.play().then(() => {
        wrap.classList.add('is-playing');
        playBtn.innerHTML = VOICE_PAUSE_SVG;
        playBtn.setAttribute('aria-label', 'Pause voice message');
      }).catch((err) => {
        console.warn('Voice playback failed:', err);
        pausePlayback();
      });
    }

    function togglePlayback() {
      if (audio.paused || audio.ended) {
        startPlayback();
      } else {
        pausePlayback();
      }
    }

    playBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      togglePlayback();
    });

    audio.addEventListener('loadedmetadata', () => {
      if (Number.isFinite(audio.duration) && audio.duration > 0) {
        totalSec = audio.duration;
        updateProgress(0, totalSec);
      }
    });

    audio.addEventListener('timeupdate', () => {
      if (!isScrubbing) {
        updateProgress(audio.currentTime, audio.duration || totalSec);
      }
    });

    audio.addEventListener('ended', () => {
      pausePlayback();
      updateProgress(0, audio.duration || totalSec);
    });

    audio.addEventListener('error', () => {
      wrap.classList.add('is-unavailable');
      playBtn.disabled = true;
      timeText.textContent = 'Unavailable';
      pausePlayback();
    });

    let isScrubbing = false;

    function seekFromEvent(e) {
      const rect = waveformContainer.getBoundingClientRect();
      if (rect.width <= 0) return;
      const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      const dur = (Number.isFinite(audio.duration) && audio.duration > 0) ? audio.duration : totalSec;
      if (dur > 0) {
        const targetTime = ratio * dur;
        audio.currentTime = targetTime;
        updateProgress(targetTime, dur);
      }
    }

    waveformContainer.addEventListener('pointerdown', (e) => {
      if (!hasAudioFile || wrap.classList.contains('is-unavailable')) return;
      e.preventDefault();
      try {
        waveformContainer.setPointerCapture(e.pointerId);
      } catch (_e) {}
      isScrubbing = true;
      seekFromEvent(e);

      const onPointerMove = (ev) => {
        if (isScrubbing) {
          seekFromEvent(ev);
        }
      };

      const onPointerUp = (ev) => {
        if (isScrubbing) {
          isScrubbing = false;
          seekFromEvent(ev);
          try {
            waveformContainer.releasePointerCapture(ev.pointerId);
          } catch (_err) {}
          waveformContainer.removeEventListener('pointermove', onPointerMove);
          waveformContainer.removeEventListener('pointerup', onPointerUp);
          waveformContainer.removeEventListener('pointercancel', onPointerUp);
        }
      };

      waveformContainer.addEventListener('pointermove', onPointerMove);
      waveformContainer.addEventListener('pointerup', onPointerUp);
      waveformContainer.addEventListener('pointercancel', onPointerUp);
    });

    waveformContainer.addEventListener('keydown', (e) => {
      if (!hasAudioFile || wrap.classList.contains('is-unavailable')) return;
      const dur = (Number.isFinite(audio.duration) && audio.duration > 0) ? audio.duration : totalSec;
      if (!dur) return;
      if (e.key === 'ArrowLeft') {
        e.preventDefault();
        audio.currentTime = Math.max(0, audio.currentTime - 5);
        updateProgress(audio.currentTime, dur);
      } else if (e.key === 'ArrowRight') {
        e.preventDefault();
        audio.currentTime = Math.min(dur, audio.currentTime + 5);
        updateProgress(audio.currentTime, dur);
      } else if (e.key === ' ' || e.key === 'Enter') {
        e.preventDefault();
        togglePlayback();
      }
    });

    speedPill.addEventListener('click', (e) => {
      e.stopPropagation();
      const current = savedRate;
      const nextRate = current === 1 ? 1.2 : (current === 1.2 ? 1.5 : (current === 1.5 ? 2 : 1));
      savedRate = nextRate;
      sessionStorage.setItem('wa_voice_playback_rate', String(nextRate));
      audio.playbackRate = nextRate;
      speedPill.textContent = `${nextRate}x`;
      speedPill.setAttribute('aria-label', `Playback speed ${nextRate}x`);
      window.dispatchEvent(new CustomEvent('wa-voice-speed-change', { detail: { rate: nextRate } }));
    });

    const onGlobalSpeedChange = (e) => {
      if (e.detail && e.detail.rate) {
        savedRate = e.detail.rate;
        audio.playbackRate = savedRate;
        speedPill.textContent = `${savedRate}x`;
        speedPill.setAttribute('aria-label', `Playback speed ${savedRate}x`);
      }
    };
    window.addEventListener('wa-voice-speed-change', onGlobalSpeedChange);

    wrap.addEventListener('keydown', (e) => {
      if (e.target === wrap && (e.key === ' ' || e.key === 'Enter')) {
        e.preventDefault();
        togglePlayback();
      }
    });

    return wrap;
  }
