function renderMedia(msg) {
    if (msg.media_type === 'gif') {
      return renderGif(msg);
    }
    if (msg.media_type === 'video' && isVideoNote(msg)) {
      return renderVideoNote(msg);
    }
    if (msg.media_type === 'audio' && isVoiceNote(msg)) {
      return renderVoiceNoteBubble(msg);
    }

    const wrap = document.createElement('div');
    wrap.className = 'msg-media';
    const path = msg.archive_path;
    const src = MEDIA + path;
    const mt = msg.media_type;

    const ratio = _extractMsgAspectRatio(msg);

    if (mt === 'image' || mt === 'sticker') {
      if (mt === 'sticker') {
        wrap.classList.add('sticker');
        if (ratio > 0) wrap.style.aspectRatio = ratio.toFixed(3);
      } else {
        _applyMediaOrientationBox(wrap, ratio);
      }
      const cleanName = _sanitizeMediaName(msg.media_name);
      const img = document.createElement('img');
      img.src = src;
      img.loading = 'lazy';
      img.alt = cleanName || (mt === 'sticker' ? 'Sticker' : 'Photo');
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
        const scroll = document.getElementById('message-scroll');
        if (scroll && isUserAtBottom) {
          suppressScroll = true;
          scroll.scrollTop = scroll.scrollHeight;
          suppressScroll = false;
        }
      }, { once: true });
      if (mt !== 'sticker' && ratio <= 0) {
        img.addEventListener('load', () => {
          if (img.naturalWidth > 0 && img.naturalHeight > 0) {
            _applyMediaOrientationBox(wrap, img.naturalWidth / img.naturalHeight);
          }
        }, { once: true });
      }
      img.addEventListener('click', () => {
        if (!msg.is_missing) showLightbox(msg);
      });
      _attachMediaScrollAnchor(img);
      wrap.appendChild(img);

      if (msg.is_live_photo && msg.live_photo_path) {
        wrap.classList.add('msg-live-photo');
        const liveBadge = document.createElement('span');
        liveBadge.className = 'msg-live-badge';
        liveBadge.innerHTML = `<span class="live-icon">◎</span> ${esc(t('msg.live'))}`;
        liveBadge.title = t('msg.live_photo_hint');
        wrap.appendChild(liveBadge);

        let liveVid = null;

        function startLivePlay() {
          if (!liveVid) {
            liveVid = document.createElement('video');
            liveVid.className = 'live-photo-video';
            liveVid.src = MEDIA + msg.live_photo_path;
            liveVid.muted = true;
            liveVid.loop = true;
            liveVid.playsInline = true;
            wrap.appendChild(liveVid);
          }
          liveVid.currentTime = 0;
          liveVid.play().then(() => {
            wrap.classList.add('is-live-playing');
          }).catch(() => {});
        }

        function stopLivePlay() {
          if (liveVid) {
            liveVid.pause();
            liveVid.currentTime = 0;
            wrap.classList.remove('is-live-playing');
          }
        }

        wrap.addEventListener('mouseenter', () => {
          startLivePlay();
        });
        wrap.addEventListener('mouseleave', () => {
          stopLivePlay();
        });

        liveBadge.addEventListener('click', (e) => {
          e.stopPropagation();
          if (wrap.classList.contains('is-live-playing')) {
            stopLivePlay();
          } else {
            startLivePlay();
          }
        });
      }
    } else if (mt === 'video') {
      _applyMediaOrientationBox(wrap, ratio);
      const vid = document.createElement('video');
      vid.controls = true;
      vid.preload = 'metadata';
      vid.src = src.includes('#') ? src : `${src}#t=0.001`;
      wrap.appendChild(vid);
      const handleVideoError = () => {
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
      vid.addEventListener('error', handleVideoError, { once: true });
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
    } else if (mt === 'audio') {
      if (isVoiceNote(msg)) {
        return renderVoiceNoteBubble(msg);
      }
      wrap.classList.add('audio');
      const aud = document.createElement('audio');
      aud.controls = true;
      aud.src = src;
      wrap.appendChild(aud);
    } else if (mt === 'vcard') {
      wrap.classList.add('vcard');
      const card = document.createElement('div');
      card.className = 'vcard-card';

      const avatar = document.createElement('div');
      avatar.className = 'vcard-avatar';
      avatar.innerHTML = '<span class="vcard-avatar-icon">👤</span>';

      const details = document.createElement('div');
      details.className = 'vcard-details';

      const nameEl = document.createElement('div');
      nameEl.className = 'vcard-name';
      nameEl.textContent = msg.text_body || msg.media_name || t('media.contact');

      const subEl = document.createElement('div');
      subEl.className = 'vcard-sub';
      subEl.textContent = t('msg.contact_card');

      details.appendChild(nameEl);
      details.appendChild(subEl);
      card.appendChild(avatar);
      card.appendChild(details);

      const downloadLink = document.createElement('a');
      downloadLink.className = 'vcard-download';
      downloadLink.href = src;
      const downloadName = (msg.media_name && msg.media_name.toLowerCase().endsWith('.vcf'))
        ? msg.media_name
        : ((msg.text_body ? msg.text_body : 'contact') + '.vcf');
      downloadLink.download = downloadName;
      downloadLink.innerHTML = `<span class="vcard-download-icon">📇</span><span>${esc(t('msg.view_contact_vcf'))}</span>`;

      wrap.appendChild(card);
      wrap.appendChild(downloadLink);
    } else {
      wrap.classList.add('doc');
      const a = document.createElement('a');
      a.href = src;
      a.className = 'doc-link';
      a.download = msg.media_name || 'file';
      a.innerHTML = `<span class="doc-icon">📄</span><span>${esc(msg.media_name || 'Download')}</span>`;
      wrap.appendChild(a);
    }
    if (msg.is_view_once) {
      const voBadge = document.createElement('span');
      voBadge.className = 'msg-view-once-badge';
      voBadge.innerHTML = `<span class="vo-icon">①</span> ${esc(t('msg.view_once'))}`;
      wrap.appendChild(voBadge);
    }
    return wrap;
  }

  function formatCoordinates(lat, lng) {
    const latNum = Number(lat);
    const lngNum = Number(lng);
    if (isNaN(latNum) || isNaN(lngNum)) return (lat || '') + ', ' + (lng || '');
    const latDir = latNum >= 0 ? 'N' : 'S';
    const lngDir = lngNum >= 0 ? 'E' : 'W';
    return Math.abs(latNum).toFixed(4) + '° ' + latDir + ', ' + Math.abs(lngNum).toFixed(4) + '° ' + lngDir;
  }

  function renderLocation(msg) {
    const isLive = msg.media_type === 'live_location'
      || msg.raw_msg_type === 16
      || Boolean(Number(msg.live_location_share_duration) > 0);
    const wrap = document.createElement('div');
    wrap.className = 'msg-location-wrap';

    const card = document.createElement('div');
    card.className = 'msg-location-card' + (isLive ? ' is-live-location' : ' is-static-location');

    // 1. Map Header (Pre-rendered thumbnail from DB or offline vector fallback)
    const mapHeader = document.createElement('div');
    mapHeader.className = 'location-map-header';

    const typeChip = document.createElement('div');
    typeChip.className = 'location-map-type-chip' + (isLive ? ' is-live' : ' is-static');
    typeChip.innerHTML = isLive
      ? '<span class="live-pulse-dot" aria-hidden="true"></span><span>Live Location</span>'
      : '<span class="loc-pin-icon" aria-hidden="true">📍</span><span>Location</span>';
    mapHeader.appendChild(typeChip);

    const fallbackVector = () => {
      const existingHint = mapHeader.querySelector('.location-map-hover-hint');
      const existingChip = mapHeader.querySelector('.location-map-type-chip');
      mapHeader.innerHTML = '';
      if (existingChip) {
        mapHeader.appendChild(existingChip);
      }
      const svgWrap = document.createElement('div');
      svgWrap.className = 'location-vector-map' + (isLive ? ' is-live' : '');
      svgWrap.innerHTML = `
        <svg viewBox="0 0 320 180" xmlns="http://www.w3.org/2000/svg" class="location-map-svg" aria-hidden="true">
          <defs>
            <pattern id="locGrid" width="40" height="40" patternUnits="userSpaceOnUse">
              <path d="M 40 0 L 0 0 0 40" fill="none" stroke="currentColor" stroke-width="0.75" stroke-opacity="0.18" />
            </pattern>
          </defs>
          <rect width="100%" height="100%" fill="currentColor" class="map-bg" />
          <rect width="100%" height="100%" fill="url(#locGrid)" />
          <path d="M-10 60 Q 90 40 180 80 T 330 110" fill="none" stroke="currentColor" stroke-width="5" stroke-opacity="0.32" />
          <path d="M 70 -10 Q 120 70 160 190" fill="none" stroke="currentColor" stroke-width="3" stroke-opacity="0.25" />
          <path d="M 230 -10 Q 200 80 250 190" fill="none" stroke="currentColor" stroke-width="2.5" stroke-opacity="0.2" />
          <circle cx="160" cy="90" r="${isLive ? 32 : 24}" fill="none" stroke="var(--accent)" stroke-width="1.5" stroke-opacity="0.35" class="location-ping-ring" />
          ${isLive ? '<circle cx="160" cy="90" r="16" fill="var(--accent)" fill-opacity="0.2" />' : ''}
          ${isLive 
            ? '<circle cx="160" cy="90" r="6" fill="var(--accent)" />'
            : '<path d="M 160 76 C 153.4 76 148 81.4 148 88 C 148 97 160 108 160 108 C 160 108 172 97 172 88 C 172 81.4 166.6 76 160 76 Z" fill="#ea4335" /><circle cx="160" cy="87" r="3.5" fill="#fff" />'
          }
        </svg>
      `;
      mapHeader.appendChild(svgWrap);
      if (existingHint) {
        mapHeader.appendChild(existingHint);
      }
    };

    if (msg.has_thumbnail) {
      const img = document.createElement('img');
      img.className = 'location-map-thumb';
      img.loading = 'lazy';
      img.alt = msg.location_name || (isLive ? 'Live Location' : 'Location map');
      img.src = API + '/thumbnail/' + msg.msg_id;
      img.addEventListener('error', fallbackVector, { once: true });
      mapHeader.appendChild(img);
    } else {
      fallbackVector();
    }

    if (msg.latitude && msg.longitude) {
      mapHeader.classList.add('is-interactive');
      mapHeader.setAttribute('role', 'button');
      mapHeader.setAttribute('tabindex', '0');
      mapHeader.setAttribute('aria-haspopup', 'dialog');
      mapHeader.setAttribute('aria-label', (msg.location_name ? (msg.location_name + ' — ') : '') + t('msg.open_in_maps'));
      mapHeader.title = t('msg.click_open_maps');

      const hoverHint = document.createElement('div');
      hoverHint.className = 'location-map-hover-hint';
      hoverHint.innerHTML = `<span class="hover-hint-icon" aria-hidden="true">🗺️</span><span>${esc(t('msg.open_in_maps'))}</span> <span class="hover-hint-arrow" aria-hidden="true">↗</span>`;
      mapHeader.appendChild(hoverHint);

      const triggerMapChooser = (e) => {
        e.stopPropagation();
        openMapChooser({
          latitude: msg.latitude,
          longitude: msg.longitude,
          name: msg.location_name,
          address: msg.location_address,
          isLive: isLive
        }, mapHeader);
      };

      mapHeader.addEventListener('click', triggerMapChooser);
      mapHeader.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          triggerMapChooser(e);
        }
      });
    }

    card.appendChild(mapHeader);

    // 2. Details (Venue Name, Address, Status, Timestamps, Coordinates)
    const details = document.createElement('div');
    details.className = 'location-details';

    if (isLive) {
      const liveBadge = document.createElement('div');
      liveBadge.className = 'location-live-badge';
      liveBadge.innerHTML = `<span class="live-dot" aria-hidden="true"></span><span class="live-label">${esc(t('msg.live_location_ended'))}</span>`;
      details.appendChild(liveBadge);

      if (msg.live_location_share_duration || msg.timestamp_ms) {
        const timeInfo = document.createElement('div');
        timeInfo.className = 'location-time-info';
        let durStr = '';
        const dur = Number(msg.live_location_share_duration) || 0;
        if (dur >= 3600) {
          const hrs = Math.round((dur / 3600) * 10) / 10;
          durStr = hrs + (hrs === 1 ? ' hr' : ' hrs');
        } else if (dur > 0) {
          durStr = Math.round(dur / 60) + ' min';
        }

        let endStr = '';
        if (msg.timestamp_ms && dur > 0) {
          const endDate = new Date(Number(msg.timestamp_ms) + dur * 1000);
          endStr = 'Ended at ' + endDate.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
        }

        const summary = [durStr ? ('Shared for ' + durStr) : '', endStr].filter(Boolean).join(' · ');
        if (summary) {
          timeInfo.textContent = summary;
          details.appendChild(timeInfo);
        }
      }

      if (msg.latitude && msg.longitude) {
        const coordsEl = document.createElement('div');
        coordsEl.className = 'location-coords-sub';
        coordsEl.textContent = formatCoordinates(msg.latitude, msg.longitude);
        details.appendChild(coordsEl);
      }
    } else {
      if (msg.location_name) {
        const nameEl = document.createElement('div');
        nameEl.className = 'location-name';
        nameEl.textContent = msg.location_name;
        details.appendChild(nameEl);
      } else {
        const nameEl = document.createElement('div');
        nameEl.className = 'location-name';
        nameEl.textContent = t('msg.pinned_location');
        details.appendChild(nameEl);
      }
      if (msg.location_address) {
        const addrEl = document.createElement('div');
        addrEl.className = 'location-address';
        addrEl.textContent = msg.location_address;
        details.appendChild(addrEl);
      }
      if (msg.latitude && msg.longitude) {
        const coordsEl = document.createElement('div');
        coordsEl.className = 'location-coords-sub';
        coordsEl.textContent = formatCoordinates(msg.latitude, msg.longitude);
        details.appendChild(coordsEl);
      }
    }

    card.appendChild(details);
    wrap.appendChild(card);

    if (msg.text_body) {
      const cleanBody = msg.text_body.trim();
      const isDuplicate = cleanBody === msg.location_name
        || cleanBody === msg.location_address
        || cleanBody === 'Location'
        || cleanBody === 'Pinned location'
        || cleanBody === 'Live Location'
        || cleanBody.startsWith('Live Location');
      if (!isDuplicate && cleanBody) {
        const cap = document.createElement('div');
        cap.className = 'msg-caption';
        renderCollapsibleMessageText(cap, cleanBody, msg.msg_id, msg.mentions);
        wrap.appendChild(cap);
      }
    }

    return wrap;
  }

  function renderPoll(msg) {
    const poll = msg.poll || {
      question: msg.text_body || 'Poll',
      multiple_answers: false,
      total_votes: 0,
      total_voters: 0,
      options: [],
    };

    const container = document.createElement('div');
    container.className = 'msg-poll';
    container.setAttribute('role', 'group');
    container.setAttribute('aria-label', t('msg.poll') + ': ' + (poll.question || t('msg.poll')));

    const header = document.createElement('div');
    header.className = 'msg-poll-header';

    const q = document.createElement('div');
    q.className = 'msg-poll-question';
    q.innerHTML = highlight(poll.question || 'Poll', '');
    header.appendChild(q);

    const badge = document.createElement('div');
    badge.className = 'msg-poll-badge';
    badge.textContent = t(poll.multiple_answers ? 'msg.poll_multiple' : 'msg.poll_single');
    header.appendChild(badge);

    container.appendChild(header);

    const optionsList = document.createElement('div');
    optionsList.className = 'msg-poll-options';

    const totalVotes = poll.total_votes || 0;

    for (const opt of poll.options || []) {
      const optEl = document.createElement('div');
      optEl.className = 'msg-poll-option' + (opt.user_selected ? ' user-selected' : '');

      const optVotes = (opt.vote_count !== undefined ? opt.vote_count : opt.votes) || 0;
      const pct = totalVotes > 0 ? Math.round((optVotes / totalVotes) * 100) : 0;

      const fill = document.createElement('div');
      fill.className = 'msg-poll-option-fill';
      fill.style.width = pct + '%';
      fill.setAttribute('role', 'progressbar');
      fill.setAttribute('aria-valuenow', String(pct));
      fill.setAttribute('aria-valuemin', '0');
      fill.setAttribute('aria-valuemax', '100');
      optEl.appendChild(fill);

      const content = document.createElement('div');
      content.className = 'msg-poll-option-content';

      const titleEl = document.createElement('span');
      titleEl.className = 'msg-poll-option-title';
      if (opt.user_selected) {
        const checkIcon = document.createElement('span');
        checkIcon.className = 'msg-poll-check';
        checkIcon.textContent = '✓ ';
        titleEl.appendChild(checkIcon);
      }
      const nameSpan = document.createElement('span');
      nameSpan.innerHTML = highlight(opt.name || '', '');
      titleEl.appendChild(nameSpan);
      content.appendChild(titleEl);

      const countEl = document.createElement('span');
      countEl.className = 'msg-poll-option-count';
      countEl.textContent = String(optVotes);
      content.appendChild(countEl);

      optEl.appendChild(content);
      optionsList.appendChild(optEl);
    }

    container.appendChild(optionsList);

    const footer = document.createElement('div');
    footer.className = 'msg-poll-footer';

    const totalEl = document.createElement('span');
    totalEl.className = 'msg-poll-total';
    const vCount = poll.total_voters !== undefined ? poll.total_voters : totalVotes;
    totalEl.textContent = tp('msg.votes', vCount);
    footer.appendChild(totalEl);

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'msg-poll-view-votes-btn';
    btn.textContent = t('msg.view_votes');
    btn.setAttribute('aria-haspopup', 'dialog');
    btn.addEventListener('click', e => {
      e.stopPropagation();
      showPollDetails(msg.msg_id, btn);
    });
    footer.appendChild(btn);

    container.appendChild(footer);
    return container;
  }

  let lightboxItems = [];
  let lightboxIndex = 0;
  let lightboxContext = 'gallery'; // 'gallery' | 'chat' — controls pagination + type filtering
