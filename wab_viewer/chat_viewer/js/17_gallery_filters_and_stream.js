  const GALLERY_STAT_PILL_TYPES = [
    { key: 'total', type: null, label: 'items' },
    { key: 'image', type: 'image', label: 'images' },
    { key: 'video', type: 'video', label: 'videos' },
    { key: 'voice', type: 'voice', label: 'voice notes' },
    { key: 'audio', type: 'audio', label: 'audio' },
    { key: 'gif', type: 'gif', label: 'GIFs' },
    { key: 'sticker', type: 'sticker', label: 'stickers' },
    { key: 'document', type: 'document', label: 'documents' },
    { key: 'link', type: 'link', label: 'links' },
    { key: 'vcard', type: 'vcard', label: 'contacts' },
  ];

  function _renderSkeletonStatPills() {
    const stats = document.getElementById('media-gallery-stats');
    if (!stats) return;
    stats.innerHTML = '';
    for (const item of GALLERY_STAT_PILL_TYPES) {
      const pill = _makeStatPill(item.key, '--', item.type, 0);
      pill.classList.add('is-loading');
      stats.appendChild(pill);
    }
    _syncStatPills();
  }

  function _hydrateStatPills(counts) {
    if (!counts) return;
    currentGalleryStats = counts;
    const stats = document.getElementById('media-gallery-stats');
    if (!stats) return;

    const plurals = { voice: 'voice notes', audio: 'audio', sticker: 'stickers', document: 'documents', link: 'links', vcard: 'contacts', total: 'items' };
    const singulars = { voice: 'voice note', vcard: 'contact', total: 'item' };
    const typeOrder = ['image', 'video', 'voice', 'audio', 'gif', 'sticker', 'document', 'vcard', 'link'];
    const totalMissing = Object.values(counts.by_type || {}).reduce((acc, d) => acc + (d.missing || 0), 0);

    // Update 'total' pill
    let totalPill = stats.querySelector('.gallery-stat[data-type="total"]');
    if (!totalPill) {
      totalPill = _makeStatPill('total', counts.total, null, totalMissing);
      stats.prepend(totalPill);
    } else {
      const ms = totalMissing ? ` <span class="gallery-stat-missing">(${totalMissing} missing)</span>` : '';
      const displayLabel = counts.total === 1 ? 'item' : 'items';
      totalPill.innerHTML = `<strong>${counts.total}</strong> ${displayLabel}${ms}`;
      totalPill.classList.remove('is-loading');
      totalPill.style.display = '';
    }

    // Update each type pill
    for (const t of typeOrder) {
      const d = counts.by_type?.[t];
      const count = d?.count || 0;
      const missing = d?.missing || 0;
      let pill = stats.querySelector(`.gallery-stat[data-type="${t}"]`);
      if (count === 0 && !activeTypes.has(t)) {
        if (pill) pill.style.display = 'none';
        continue;
      }
      if (!pill) {
        pill = _makeStatPill(t, count, t, missing);
        stats.appendChild(pill);
      } else {
        const displayLabel = count === 1 ? (singulars[t] || t) : (plurals[t] || t + 's');
        const ms = missing ? ` <span class="gallery-stat-missing">(${missing} missing)</span>` : '';
        pill.innerHTML = `<strong>${count}</strong> ${displayLabel}${ms}`;
        pill.classList.remove('is-loading');
        pill.style.display = '';
      }
    }

    _syncStatPills();
    _syncGalleryNotices();
  }

  async function openMediaGallery() {
    if (!currentChat) return;
    closeChatInfo();
    closeChatStarred();
    const grid = document.getElementById('media-gallery-grid');
    const switcher = document.getElementById('media-view-switcher');

    const gen = ++galleryOpenGen;
    const requestedChatId = currentChat.id;
    const requestedChatType = currentChat.type;

    if (gallerySessionAbortController) {
      gallerySessionAbortController.abort();
      gallerySessionAbortController = null;
    }
    const sessionController = new AbortController();
    gallerySessionAbortController = sessionController;

    // disconnect any observer/sentinel from a previous open before clearing the grid
    if (galleryObserver) { galleryObserver.disconnect(); galleryObserver = null; }
    if (gallerySentinel) { gallerySentinel = null; }
    teardownMediaObservers();
    clearVideoProbeQueue();
    clearTimeout(galleryScrollIdleTimer);
    galleryIsScrolling = false;

    // reset to classic view each time
    archiveViewActive = false;
    archiveLoadingAll = false;
    galleryBackfillGen++;
    switcher.style.display = 'flex';
    document.getElementById('media-archive-tabs').style.display =
      currentChat.type === 'contact' ? 'flex' : 'none';
    document.querySelectorAll('.media-view-btn').forEach((b, i) => b.classList.toggle('active', i === 0));
    grid.style.display = 'grid';
    document.getElementById('media-archive-view').classList.remove('active');

    document.getElementById('media-gallery-title').textContent =
      currentChat.display_name ? `Media — ${currentChat.display_name}` : 'Media';

    grid.innerHTML = '<div class="gallery-filter-loading gallery-initial-loading"><span class="gallery-filter-spinner"></span><span>Loading media…</span></div>';
    document.getElementById('media-gallery').classList.add('open');

    // reset pagination state
    currentGalleryItems = [];
    currentArchiveItems = [];
    archiveAllLoaded = false;
    currentGalleryStats = null;
    lightboxItems = [];
    activeTypes = new Set();
    defaultActiveTypes = new Set();
    isDefaultView = true;
    allMediaStreamLoaded = false;
    streamHasFilteredTail = false;
    typesAllLoaded = new Set();
    galleryAllLoaded = false;
    galleryLoadingMore = false;
    linksAllLoaded = false;
    linksLoadingMore = false;

    // Render skeleton pills immediately or hydrate from cache
    const cacheKey = `${requestedChatId}:${requestedChatType}`;
    const cachedCounts = galleryCountsCache.get(cacheKey);
    if (cachedCounts) {
      currentGalleryStats = cachedCounts;
      _hydrateStatPills(cachedCounts);
    } else {
      _renderSkeletonStatPills();
    }

    try {
      const typeOrder = ['image', 'video', 'voice', 'audio', 'gif', 'sticker', 'document', 'vcard', 'link'];
      const qs = 'chat_id=' + encodeURIComponent(requestedChatId) + '&chat_type=' + encodeURIComponent(requestedChatType);

      // Optimistically default to images + videos (filtering out any DEFAULT_DESELECTED_TYPES)
      activeTypes = new Set(['image', 'video'].filter(t => !DEFAULT_DESELECTED_TYPES.has(t)));
      defaultActiveTypes = new Set(activeTypes);
      isDefaultView = true;
      _syncStatPills();

      grid.innerHTML = '';

      // attach IntersectionObserver sentinel at bottom of grid
      gallerySentinel = document.createElement('div');
      gallerySentinel.className = 'gallery-sentinel';
      gallerySentinel.style.height = '1px';
      grid.appendChild(gallerySentinel);
      galleryObserver = new IntersectionObserver(entries => {
        if (entries[0].isIntersecting && !galleryLoadingMore && !linksLoadingMore && galleryScrollDirection === 'down') {
          if (activeTypes.size === 1 && activeTypes.has('link')) {
            _loadMoreLinks();
          } else {
            const oldest = _getOldestTimestampForActiveTypes(grid);
            _loadGalleryPage(oldest);
          }
        }
      }, { root: grid, threshold: 0.1 });
      galleryObserver.observe(gallerySentinel);

      getGalleryMediaObserver();

      // Initiate count aggregation in background without blocking initial media render
      fetch('/api/media/count?' + qs, { signal: sessionController.signal })
        .then(r => r.ok ? r.json() : null)
        .then(counts => {
          if (!counts) return;
          if (gen !== galleryOpenGen || !currentChat || currentChat.id !== requestedChatId) return;
          if (!document.getElementById('media-gallery').classList.contains('open')) return;
          galleryCountsCache.set(cacheKey, counts);
          _hydrateStatPills(counts);

          // If chat has no images or videos and user hasn't switched filter, switch to first available type
          const hasImagesOrVideos = (counts.by_type?.['image']?.count || 0) + (counts.by_type?.['video']?.count || 0) > 0;
          if (!hasImagesOrVideos && counts.total > 0 && isDefaultView) {
            const firstAvailable = typeOrder.find(t => counts.by_type?.[t]?.count > 0);
            if (firstAvailable) {
              activeTypes = new Set([firstAvailable]);
              defaultActiveTypes = new Set([firstAvailable]);
              _syncStatPills();
              _loadGalleryPage(null);
            }
          } else if (counts.total === 0 && !currentGalleryItems.length && !sessionController.signal.aborted) {
            grid.innerHTML = '<div class="gallery-empty-notice">No media in this chat.</div>';
          }
        })
        .catch(err => {
          if (err.name !== 'AbortError') console.error('Failed to fetch media count:', err);
        });

      // Render page 1 immediately
      await _loadGalleryPage(null);

      if (gen !== galleryOpenGen || !currentChat || currentChat.id !== requestedChatId) return;
      if (!document.getElementById('media-gallery').classList.contains('open')) return;

      if (!currentGalleryItems.length && !sessionController.signal.aborted) {
        if (currentGalleryStats && currentGalleryStats.total === 0) {
          grid.innerHTML = '<div class="gallery-empty-notice">No media in this chat.</div>';
        } else if (!currentGalleryStats) {
          grid.innerHTML = '<div class="gallery-filter-loading"><span class="gallery-filter-spinner"></span><span>Checking media…</span></div>';
        }
        return;
      }
    } catch (err) {
      if (err.name === 'AbortError' || sessionController.signal.aborted) {
        return;
      }
      if (gen !== galleryOpenGen || !currentChat || currentChat.id !== requestedChatId) return;
      console.error('Failed to load media gallery:', err);
      grid.innerHTML = '<div class="gallery-empty-notice">Failed to load media gallery.</div>';
      return;
    }
    if (gen !== galleryOpenGen || !document.getElementById('media-gallery').classList.contains('open')) return;
    _syncMonthHeaders();
    _syncGalleryNotices();
  }

  function _setStatPillsLoading(isLoading) {
    const statsEl = document.getElementById('media-gallery-stats');
    if (!statsEl) return;
    const pills = statsEl.querySelectorAll('.gallery-stat');
    if (!isLoading) {
      pills.forEach(p => {
        const strong = p.querySelector('strong');
        if (!strong || strong.textContent !== '--') {
          p.classList.remove('is-loading');
        }
      });
      return;
    }
    const currentType = activeTypes.size === 1 ? [...activeTypes][0] : (activeTypes.size === 0 ? 'total' : null);
    pills.forEach(p => {
      if (currentType && p.dataset.type === currentType) {
        p.classList.add('is-loading');
      } else if (!currentType && activeTypes.has(p.dataset.type)) {
        p.classList.add('is-loading');
      } else {
        const strong = p.querySelector('strong');
        if (!strong || strong.textContent !== '--') {
          p.classList.remove('is-loading');
        }
      }
    });
  }

  function _syncGalleryNotices() {
    const grid = document.getElementById('media-gallery-grid');
    if (!grid || archiveViewActive) return;
    if (!document.getElementById('media-gallery').classList.contains('open')) return;

    grid.querySelectorAll('.gallery-end-notice, .gallery-empty-notice').forEach(n => n.remove());

    const typesKey = activeTypes.size > 0 ? [...activeTypes].sort().join(',') : 'all';
    const type = activeTypes.size === 1 ? [...activeTypes][0] : null;
    const typeLabel = type ? (MEDIA_TYPE_PLURALS[type] || type + 's') : 'media';

    let totalExpected = 0;
    if (currentGalleryStats) {
      if (type) {
        totalExpected = currentGalleryStats.by_type?.[type]?.count || 0;
      } else if (activeTypes.size === 0) {
        totalExpected = currentGalleryStats.total || 0;
      } else {
        totalExpected = [...activeTypes].reduce((acc, t) => acc + (currentGalleryStats.by_type?.[t]?.count || 0), 0);
      }
    }

    const visibleCount = grid.querySelectorAll(
      type
        ? `.gallery-item[data-type="${type}"]:not(.type-hidden), .gallery-link-card[data-type="${type}"]:not(.type-hidden)`
        : '.gallery-item:not(.type-hidden), .gallery-link-card:not(.type-hidden)'
    ).length;

    if (totalExpected > 0 && visibleCount >= totalExpected) {
      typesAllLoaded.add(typesKey);
    }

    const isAllDone = (activeTypes.size === 0 && galleryAllLoaded) ||
                      typesAllLoaded.has(typesKey) ||
                      (activeTypes.size === 1 && activeTypes.has('link') && linksAllLoaded) ||
                      (totalExpected > 0 && visibleCount >= totalExpected);

    if ((currentGalleryStats && totalExpected === 0 && type) || (isAllDone && visibleCount === 0)) {
      const notice = document.createElement('div');
      notice.className = 'gallery-empty-notice';
      notice.textContent = type ? `No ${typeLabel} in this chat.` : 'No media in this chat.';
      grid.insertBefore(notice, gallerySentinel || null);
    } else if (isAllDone && visibleCount > 0) {
      const notice = document.createElement('div');
      notice.className = 'gallery-end-notice';
      notice.textContent = `All ${visibleCount} ${typeLabel} loaded`;
      grid.insertBefore(notice, gallerySentinel || null);
    }
  }

  async function _loadGalleryPage(before) {
    if (galleryLoadingMore) return;
    const typesKey = activeTypes.size > 0 ? [...activeTypes].sort().join(',') : 'all';
    if (typesAllLoaded.has(typesKey)) return;
    if (activeTypes.size === 0 && galleryAllLoaded) return;
    const gen = galleryOpenGen;
    const requestedChatId = currentChat ? currentChat.id : null;
    galleryLoadingMore = true;
    if (!archiveViewActive) {
      _setStatPillsLoading(true);
    }
    const grid = document.getElementById('media-gallery-grid');

    const controller = new AbortController();
    galleryCurrentAbortController = controller;

    let scrollLoader = null;
    if (grid && before != null) {
      scrollLoader = grid.querySelector('.gallery-scroll-loading');
      if (!scrollLoader) {
        scrollLoader = document.createElement('div');
        scrollLoader.className = 'gallery-scroll-loading';
        const typeKey = activeTypes.size === 1 ? [...activeTypes][0] : 'total';
        const typePlural = MEDIA_TYPE_PLURALS[typeKey] || 'media';
        scrollLoader.innerHTML = `<span class="gallery-filter-spinner"></span><span>Loading older ${typePlural}…</span>`;
        grid.insertBefore(scrollLoader, gallerySentinel || null);
      }
    }

    try {
      const typesParam = activeTypes.size > 0
        ? '&types=' + encodeURIComponent([...activeTypes].join(','))
        : '&types=all';
      const url = '/api/media?chat_id=' + encodeURIComponent(currentChat.id) +
                  '&chat_type=' + encodeURIComponent(currentChat.type) +
                  typesParam +
                  (before != null ? '&before=' + before : '');
      const res = await fetch(url, { signal: controller.signal });
      if (!res.ok) throw new Error('Failed to fetch media');
      const items = await res.json();
      if (gen !== galleryOpenGen || !currentChat || currentChat.id !== requestedChatId) return;
      if (!document.getElementById('media-gallery').classList.contains('open')) return;
      if (items.length < 100) {
        typesAllLoaded.add(typesKey);
        if (activeTypes.size === 0) {
          galleryAllLoaded = true;
        }
      }
      if (before != null && activeTypes.size > 0) {
        streamHasFilteredTail = true;
      }
      currentGalleryItems.push(...items);

      if (!archiveViewActive) {
        _insertGalleryGridBatch(grid, items);
      } else {
        _appendToArchiveView(items, activeArchiveDir());
      }
      _syncMonthHeaders();
    } catch (err) {
      if (err.name === 'AbortError') {
        return;
      }
      if (gen !== galleryOpenGen || !currentChat || currentChat.id !== requestedChatId) return;
      console.error('Failed to load gallery page:', err);
    } finally {
      if (galleryCurrentAbortController === controller) {
        galleryCurrentAbortController = null;
      }
      if (scrollLoader) scrollLoader.remove();
      const filterLoader = grid ? grid.querySelector('.gallery-filter-loading') : null;
      if (filterLoader) filterLoader.remove();
      _setStatPillsLoading(false);
      galleryLoadingMore = false;
      if (gen === galleryOpenGen && document.getElementById('media-gallery').classList.contains('open')) {
        _syncGalleryNotices();
      }
      if (!archiveViewActive) {
        scheduleVisibleVideoExtraction();
      }

      // Resolve fast scrolling race condition: if still near bottom going down, continue loading
      if (grid && galleryScrollDirection === 'down' && !typesAllLoaded.has(typesKey)) {
        const distFromBottom = grid.scrollHeight - (grid.scrollTop + grid.clientHeight);
        if (distFromBottom <= 800 || galleryPendingScrollCheck) {
          galleryPendingScrollCheck = false;
          requestAnimationFrame(() => {
            _checkGalleryLoadMore();
          });
        }
      } else {
        galleryPendingScrollCheck = false;
      }
    }
  }

  document.getElementById('media-btn').addEventListener('click', openMediaGallery);
  document.getElementById('media-gallery-close').addEventListener('click', closeMediaGallery);
  document.getElementById('media-gallery-grid').addEventListener('scroll', onGalleryScroll, { passive: true });
  document.getElementById('media-archive-tree').addEventListener('scroll', onGalleryScroll, { passive: true });
  document.addEventListener('keydown', async e => {
    if (document.getElementById('img-lightbox')) {
      if (e.key === 'Escape') { _closeLb(); return; }
      if (e.key === 'ArrowLeft') {
        const i = _nextLightboxIndex(lightboxIndex, -1);
        if (i !== -1) { lightboxIndex = i; openLightboxAt(lightboxIndex, -1); }
        return;
      }
      if (e.key === 'ArrowRight') {
        let i = _nextLightboxIndex(lightboxIndex, 1);
        if (i !== -1) {
          lightboxIndex = i; openLightboxAt(lightboxIndex, 1);
        } else if (lightboxContext === 'gallery' && !galleryAllLoaded) {
          const oldest = currentGalleryItems[currentGalleryItems.length - 1]?.timestamp_ms;
          await _loadGalleryPage(oldest);
          i = _nextLightboxIndex(lightboxIndex, 1);
          if (i !== -1) { lightboxIndex = i; openLightboxAt(lightboxIndex, 1); }
        }
        return;
      }
    }
  });

  function _nextLightboxIndex(from, dir) {
    let i = from + dir;
    while (i >= 0 && i < lightboxItems.length) {
      const item = lightboxItems[i];
      if (item && item.is_missing) {
        i += dir;
        continue;
      }
      // activeTypes is a gallery-only filter; in chat context every item in
      // lightboxItems is already a renderable media message, so accept it.
      if (lightboxContext !== 'gallery' || activeTypes.size === 0 || activeTypes.has(item.media_type)) return i;
      i += dir;
    }
    return -1;
  }

  function _makeStatPill(label, count, type, missing) {
    const plurals = { voice: 'voice notes', audio: 'audio', sticker: 'stickers', document: 'documents', link: 'links', vcard: 'contacts', total: 'items' };
    const singulars = { voice: 'voice note', vcard: 'contact', total: 'item' };
    const displayLabel = count === 1 ? (singulars[label] || label) : (plurals[label] || label + 's');
    const span = document.createElement('span');
    span.className = 'gallery-stat';
    span.dataset.type = type || 'total';
    const ms = missing ? ` <span class="gallery-stat-missing">(${missing} missing)</span>` : '';
    span.innerHTML = `<strong>${count}</strong> ${displayLabel}${ms}`;
    span.addEventListener('click', () => _toggleTypeFilter(type));
    return span;
  }

  function _isDefaultActiveState() {
    return isDefaultView && activeTypes.size > 0;
  }

  function _toggleTypeFilter(type) {
    const grid = document.getElementById('media-gallery-grid');
    const targetTs = grid ? _getMinVisibleGalleryTimestamp(grid) : Infinity;
    const isTogglingPrimary = !isDefaultView && type !== null && PRIMARY_MEDIA_TYPES.has(type) &&
      activeTypes.size > 0 && [...activeTypes].every(t => PRIMARY_MEDIA_TYPES.has(t));
    const isSingleTypeSwitch = !isTogglingPrimary && type !== null && !(activeTypes.size === 1 && activeTypes.has(type));
    const anchorEl = (!isSingleTypeSwitch && grid) ? _findAnchorGalleryElement(grid) : null;
    const anchorTopBefore = anchorEl ? anchorEl.getBoundingClientRect().top : 0;

    if (type === null) {
      // Total: toggle between show-all (empty set) and default media-only state
      if (activeTypes.size === 0 && defaultActiveTypes.size > 0) {
        isDefaultView = true;
        activeTypes = new Set(defaultActiveTypes);
        _applyTypeFilter();
        if (grid && anchorEl && anchorEl.isConnected && !anchorEl.classList.contains('type-hidden')) {
          const delta = anchorEl.getBoundingClientRect().top - anchorTopBefore;
          if (Math.abs(delta) > 1) {
            grid.scrollTop += delta;
          }
        }
        _syncStatPills();
        scheduleVisibleVideoExtraction();
        _syncGalleryNotices();
        if (archiveViewActive) {
          buildArchiveView(currentArchiveItems, activeArchiveDir());
          if (!archiveAllLoaded) _loadAllArchivePages();
        }
        return;
      } else {
        isDefaultView = false;
        activeTypes.clear();
        _syncStatPills();

        if (allMediaStreamLoaded && !streamHasFilteredTail) {
          _applyTypeFilter();
          _syncMonthHeaders();
          scheduleVisibleVideoExtraction();
          _syncGalleryNotices();
          if (archiveViewActive) {
            buildArchiveView(currentArchiveItems, activeArchiveDir());
            if (!archiveAllLoaded) _loadAllArchivePages();
          }
          return;
        }

        // Clean unified stream reload from top
        if (currentGalleryAudio && !currentGalleryAudio.paused) {
          currentGalleryAudio.pause();
          currentGalleryAudio = null;
          if (currentGalleryAudioCell) {
            currentGalleryAudioCell.classList.remove('is-playing');
            currentGalleryAudioCell = null;
          }
        }
        if (galleryCurrentAbortController) {
          galleryCurrentAbortController.abort();
          galleryCurrentAbortController = null;
        }

        if (archiveViewActive) {
          const tree = document.getElementById('media-archive-tree');
          if (tree && !currentArchiveItems.length) {
            tree.innerHTML = '<div class="gallery-filter-loading"><span class="gallery-filter-spinner"></span><span>Loading all items…</span></div>';
          }
        }

        if (grid) {
          grid.scrollTop = 0;
          grid.querySelectorAll('.gallery-filter-loading, .gallery-scroll-loading, .gallery-end-notice, .gallery-empty-notice').forEach(el => el.remove());
          const loader = document.createElement('div');
          loader.className = 'gallery-filter-loading';
          loader.innerHTML = '<span class="gallery-filter-spinner"></span><span>Loading all items…</span>';
          grid.innerHTML = '';
          grid.appendChild(loader);
          if (gallerySentinel) {
            grid.appendChild(gallerySentinel);
            galleryObserver?.observe(gallerySentinel);
          }
        }

        currentGalleryItems = [];
        lightboxItems = [];
        galleryAllLoaded = false;
        typesAllLoaded.delete('all');

        const gen = ++galleryBackfillGen;
        _setStatPillsLoading(true);

        _loadGalleryPage(null).then(() => {
          if (gen !== galleryBackfillGen || !document.getElementById('media-gallery').classList.contains('open')) return;
          allMediaStreamLoaded = true;
          streamHasFilteredTail = false;
          if (archiveViewActive) {
            buildArchiveView(currentArchiveItems, activeArchiveDir());
            if (!archiveAllLoaded) _loadAllArchivePages();
          }
          _syncMonthHeaders();
          _syncGalleryNotices();
          scheduleVisibleVideoExtraction();
        });
        return;
      }
    } else if (DEFAULT_DESELECTED_TYPES.has(type)) {
      if (!isDefaultView && activeTypes.size === 1 && activeTypes.has(type)) {
        // Default-deselected type already exclusively active -> restore default
        isDefaultView = true;
        activeTypes = defaultActiveTypes.size > 0 ? new Set(defaultActiveTypes) : new Set();
      } else {
        // Switch exclusively to this deselected type!
        isDefaultView = false;
        activeTypes = new Set([type]);
      }
    } else {
      // Primary visual media type: 'image' or 'video'
      if (isDefaultView) {
        // From default view, clicking image or video isolates it exclusively
        isDefaultView = false;
        activeTypes = new Set([type]);
      } else {
        // Currently in explicit filter mode:
        const hasOnlyPrimary = activeTypes.size > 0 && [...activeTypes].every(t => PRIMARY_MEDIA_TYPES.has(t));
        if (hasOnlyPrimary) {
          if (activeTypes.has(type)) {
            // Already active: deselect it
            activeTypes.delete(type);
            if (activeTypes.size === 0) {
              // Nothing left -> restore default view
              isDefaultView = true;
              activeTypes = new Set(defaultActiveTypes);
            }
          } else {
            // Not yet active: add it to selection (allows image + video together!)
            activeTypes.add(type);
          }
        } else {
          // Was on a secondary/deselected type (e.g. document) -> switch exclusively to this primary type
          isDefaultView = false;
          activeTypes = new Set([type]);
        }
      }
    }
    _applyTypeFilter();
    if (isSingleTypeSwitch && grid) {
      grid.scrollTop = 0;
    } else if (grid && anchorEl && anchorEl.isConnected && !anchorEl.classList.contains('type-hidden')) {
      const delta = anchorEl.getBoundingClientRect().top - anchorTopBefore;
      if (Math.abs(delta) > 1) {
        grid.scrollTop += delta;
      }
    }
    _syncStatPills();
    scheduleVisibleVideoExtraction();

    if (grid && !archiveViewActive) {
      grid.querySelectorAll('.gallery-filter-loading, .gallery-scroll-loading, .gallery-end-notice, .gallery-empty-notice').forEach(el => el.remove());
      if (type !== null && activeTypes.size === 1) {
        const hasMatchingInGrid = Boolean(grid.querySelector(`.gallery-item[data-type="${type}"]:not(.type-hidden), .gallery-link-card[data-type="${type}"]:not(.type-hidden)`));
        const totalExpected = currentGalleryStats?.by_type?.[type]?.count || 0;
        if (!hasMatchingInGrid && totalExpected > 0) {
          const typeName = MEDIA_TYPE_PLURALS[type] || type + 's';
          const loader = document.createElement('div');
          loader.className = 'gallery-filter-loading';
          loader.innerHTML = `<span class="gallery-filter-spinner"></span><span>Loading ${typeName}…</span>`;
          grid.insertBefore(loader, gallerySentinel || null);
          _setStatPillsLoading(true);
        } else {
          _syncGalleryNotices();
        }
      } else {
        _syncGalleryNotices();
      }
    }

    const gen = ++galleryBackfillGen;
    if (activeTypes.size === 1 && activeTypes.has('link')) {
      _loadMoreLinks();
    } else if (!archiveViewActive) {
      _ensureMediaLoadedForViewport(targetTs, anchorEl, anchorTopBefore, gen);
      if (activeTypes.has('link')) {
        _checkGalleryLoadMore();
      }
    }
    if (archiveViewActive) {
      if (!archiveAllLoaded) {
        _loadAllArchivePages();
      }
    }
  }

  function _applyTypeFilter() {
    const items = document.querySelectorAll(
      '#media-gallery-grid .gallery-item, #media-archive-view .gallery-item'
    );
    items.forEach(cell => {
      const shouldHide = activeTypes.size > 0 && !activeTypes.has(cell.dataset.type);
      const isHidden   = cell.classList.contains('type-hidden');
      if (shouldHide && !isHidden) {
        cell.classList.remove('is-appearing');
        cell.classList.add('type-hidden');
      } else if (!shouldHide && isHidden) {
        cell.classList.remove('type-hidden');
        cell.classList.add('is-appearing');
      }
    });
    _syncMonthHeaders();
    _hydrateVisibleGalleryItems();
  }

  function _syncMonthHeaders() {
    document.querySelectorAll('.gallery-month-header').forEach(hdr => {
      let el = hdr.nextElementSibling;
      let hasVisible = false;
      while (el && !el.classList.contains('gallery-month-header')) {
        if (el.classList.contains('gallery-item') && !el.classList.contains('type-hidden')) {
          hasVisible = true;
          break;
        }
        el = el.nextElementSibling;
      }
      hdr.classList.toggle('month-empty', !hasVisible);
    });
    document.querySelectorAll('.archive-year').forEach(block => {
      let visible = 0;
      if (block._populated) {
        visible = block.querySelectorAll('.gallery-item:not(.type-hidden)').length;
      } else if (block._messages) {
        if (activeTypes.size > 0) {
          visible = block._messages.filter(m => {
            const t = (m.media_type === 'audio' && isVoiceNote(m)) ? 'voice' : m.media_type;
            return activeTypes.has(t);
          }).length;
        } else {
          visible = block._messages.length;
        }
      }
      const countEl = block.querySelector('.archive-year-count');
      if (countEl) {
        countEl.textContent = visible;
      }
    });
  }

  function _syncStatPills() {
    const isShowAll = activeTypes.size === 0;

    document.querySelectorAll('#media-gallery-stats .gallery-stat').forEach(pill => {
      const t = pill.dataset.type;
      // secondary types (links, docs, vcard) are never archived — hide their pills in archive view
      if (archiveViewActive && t !== 'total' && SECONDARY_TYPES.has(t)) {
        pill.style.display = 'none';
        return;
      }
      pill.style.display = '';
      if (t === 'total') {
        pill.classList.toggle('active', isShowAll);
        pill.classList.remove('dimmed');
      } else if (isShowAll) {
        pill.classList.remove('active');
        pill.classList.remove('dimmed');
      } else {
        // Category filtering (default image+video or explicit type selection):
        pill.classList.toggle('active', activeTypes.has(t));
        pill.classList.toggle('dimmed', !activeTypes.has(t));
      }
    });
  }

  let archiveLoadingAll = false;
