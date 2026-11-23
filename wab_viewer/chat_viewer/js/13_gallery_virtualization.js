// ---- media gallery -------------------------------------------------------

  const EMPTY_GALLERY_PIX = 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg"/>';
  const videoThumbCache = new Map();
  const MAX_CONCURRENT_VIDEO_PROBES = 2;
  let activeVideoProbes = 0;
  const videoProbeQueue = [];
  let galleryMediaObserver = null;
  let archiveMediaObserver = null;
  let archiveViewActive = false;
  let currentGalleryItems = [];
  let currentArchiveItems = [];
  let archiveAllLoaded = false;
  let currentGalleryStats = null;
  const galleryCountsCache = new Map();
  let activeTypes = new Set();   // empty = show all; populated = show only those types
  let defaultActiveTypes = new Set();  // restored when deselecting a secondary type or sticker
  let isDefaultView = true;
  let typesAllLoaded = new Set();
  let allMediaStreamLoaded = false;
  let streamHasFilteredTail = false;
  const PRIMARY_MEDIA_TYPES = new Set(['image', 'video']);
  const SECONDARY_TYPES = new Set(['link', 'vcard']);  // non-file types, hidden by default in archive view
  const DEFAULT_DESELECTED_TYPES = new Set(['voice', 'audio', 'gif', 'sticker', 'document', 'vcard', 'link']);  // hidden by default, toggle exclusive
  const MEDIA_TYPE_PLURALS = {
    image: 'images',
    video: 'videos',
    voice: 'voice notes',
    audio: 'audio tracks',
    gif: 'GIFs',
    sticker: 'stickers',
    document: 'documents',
    link: 'links',
    vcard: 'contacts',
    total: 'media',
  };
  let galleryAllLoaded = false;
  let galleryLoadingMore = false;
  let linksAllLoaded = false;
  let linksLoadingMore = false;
  let galleryOpenGen = 0;
  let gallerySessionAbortController = null;
  let galleryBackfillGen = 0;
  let gallerySentinel = null;
  let galleryObserver = null;
  let lastGalleryScrollTop = 0;
  let galleryScrollDirection = 'down';
  let galleryCurrentAbortController = null;
  let galleryPendingScrollCheck = false;
  let galleryLinkObserver = null;

  function teardownMediaObservers() {
    if (galleryMediaObserver) {
      galleryMediaObserver.disconnect();
      galleryMediaObserver = null;
    }
    if (galleryLinkObserver) {
      galleryLinkObserver.disconnect();
      galleryLinkObserver = null;
    }
    if (archiveMediaObserver) {
      archiveMediaObserver.disconnect();
      archiveMediaObserver = null;
    }
  }

  function getGalleryMediaObserver() {
    if (!galleryMediaObserver) {
      const grid = document.getElementById('media-gallery-grid');
      if (grid) {
        galleryMediaObserver = new IntersectionObserver(handleMediaIntersection, {
          root: grid,
          rootMargin: '600px 0px 600px 0px',
        });
      }
    }
    return galleryMediaObserver;
  }
  window.getGalleryMediaObserver = getGalleryMediaObserver;

  function getGalleryLinkObserver() {
    if (!galleryLinkObserver) {
      const grid = document.getElementById('media-gallery-grid');
      if (grid) {
        galleryLinkObserver = new IntersectionObserver(handleMediaIntersection, {
          root: grid,
          rootMargin: '40px 0px 40px 0px',
        });
      }
    }
    return galleryLinkObserver;
  }
  window.getGalleryLinkObserver = getGalleryLinkObserver;

  function getArchiveMediaObserver() {
    if (!archiveMediaObserver) {
      const tree = document.getElementById('media-archive-tree');
      if (tree) {
        archiveMediaObserver = new IntersectionObserver(handleMediaIntersection, {
          root: tree,
          rootMargin: '600px 0px 600px 0px',
        });
      }
    }
    return archiveMediaObserver;
  }

  let galleryIsScrolling = false;
  let galleryScrollIdleTimer = null;

  function _findAnchorGalleryElement(grid) {
    if (!grid) return null;
    const gRect = grid.getBoundingClientRect();
    const children = [...grid.children];
    for (const el of children) {
      if ((el.classList.contains('gallery-month-header') && !el.classList.contains('month-empty')) ||
          (el.classList.contains('gallery-item') && !el.classList.contains('type-hidden'))) {
        const r = el.getBoundingClientRect();
        if (r.bottom >= gRect.top && r.top <= gRect.bottom) {
          return el;
        }
      }
    }
    return null;
  }

  function _getMinVisibleGalleryTimestamp(grid) {
    if (!grid) return Infinity;
    const gRect = grid.getBoundingClientRect();
    let minTs = Infinity;
    const children = [...grid.children];
    for (const el of children) {
      if (el.classList.contains('type-hidden') || el.classList.contains('month-empty')) continue;
      const r = el.getBoundingClientRect();
      if (r.bottom >= gRect.top && r.top <= gRect.bottom + 600) {
        if (el.dataset.ts) {
          const ts = parseInt(el.dataset.ts, 10);
          if (!isNaN(ts) && ts < minTs) minTs = ts;
        } else if (el.dataset.month) {
          const mk = parseInt(el.dataset.month, 10);
          if (!isNaN(mk)) {
            const y = Math.floor(mk / 100);
            const m = mk % 100;
            const monthStartTs = new Date(y, m - 1, 1).getTime();
            if (monthStartTs < minTs) minTs = monthStartTs;
          }
        }
      }
    }
    return minTs;
  }

  function _hydrateVisibleGalleryItems() {
    const grid = document.getElementById('media-gallery-grid');
    if (!grid) return;
    const gRect = grid.getBoundingClientRect();
    const cells = grid.querySelectorAll('.gallery-item[data-hydrated="false"]:not(.type-hidden):not([data-type="link"])');
    for (const cell of cells) {
      const r = cell.getBoundingClientRect();
      if (r.bottom >= gRect.top - 200 && r.top <= gRect.bottom + 600) {
        hydrateGalleryItem(cell);
      }
    }
  }

  async function _loadMoreLinks() {
    if (archiveViewActive || linksAllLoaded || linksLoadingMore || !currentChat) return;
    const grid = document.getElementById('media-gallery-grid');
    if (!grid) return;
    const linkCells = [...grid.querySelectorAll('.gallery-item[data-type="link"]')];
    let oldestTs = Infinity;
    if (linkCells.length > 0) {
      for (let i = linkCells.length - 1; i >= 0; i--) {
        const ts = parseInt(linkCells[i].dataset.ts || '0', 10);
        if (ts > 0 && ts < oldestTs) oldestTs = ts;
      }
    }
    const gen = galleryOpenGen;
    const requestedChatId = currentChat.id;
    linksLoadingMore = true;
    _setStatPillsLoading(true);

    let scrollLoader = null;
    if (linkCells.length > 0) {
      scrollLoader = grid.querySelector('.gallery-scroll-loading');
      if (!scrollLoader) {
        scrollLoader = document.createElement('div');
        scrollLoader.className = 'gallery-scroll-loading';
        scrollLoader.innerHTML = '<span class="gallery-filter-spinner"></span><span>Loading older links…</span>';
        const sentinel = (gallerySentinel && gallerySentinel.parentNode === grid) ? gallerySentinel : null;
        grid.insertBefore(scrollLoader, sentinel);
      }
    }

    try {
      const beforeParam = (oldestTs !== Infinity && oldestTs > 0) ? '&before=' + oldestTs : '';
      const qs = 'chat_id=' + encodeURIComponent(currentChat.id) +
                 '&chat_type=' + encodeURIComponent(currentChat.type) +
                 '&limit=30' + beforeParam;
      const signal = gallerySessionAbortController ? gallerySessionAbortController.signal : undefined;
      const res = await fetch(API + '/media/links?' + qs, { signal });
      if (!res.ok) throw new Error('Failed to fetch more links');
      const items = await res.json();
      if (gen !== galleryOpenGen || !currentChat || currentChat.id !== requestedChatId) return;
      if (!document.getElementById('media-gallery').classList.contains('open')) return;
      if (items.length < 30) linksAllLoaded = true;
      _insertGalleryGridBatch(grid, items);
      _syncMonthHeaders();
    } catch (err) {
      if (err.name === 'AbortError') return;
      if (gen !== galleryOpenGen || !currentChat || currentChat.id !== requestedChatId) return;
      console.error('Failed to load more links:', err);
    } finally {
      if (scrollLoader) scrollLoader.remove();
      const filterLoader = grid ? grid.querySelector('.gallery-filter-loading') : null;
      if (filterLoader) filterLoader.remove();
      _setStatPillsLoading(false);
      linksLoadingMore = false;
      _syncGalleryNotices();
    }
  }

  function _getOldestTimestampForActiveTypes(grid) {
    if (!grid) return null;
    const isDocOrLinkOnly = activeTypes.size === 1 && (activeTypes.has('document') || activeTypes.has('link'));
    let selector = '.gallery-item:not([data-prepopulated="true"])';
    if (isDocOrLinkOnly) {
      selector = `.gallery-item[data-type="${[...activeTypes][0]}"]`;
    } else if (activeTypes.size > 0) {
      selector = [...activeTypes].map(t => `.gallery-item[data-type="${t}"]:not([data-prepopulated="true"])`).join(', ');
    }
    const items = grid.querySelectorAll(selector);
    if (!items.length) return null;
    for (let i = items.length - 1; i >= 0; i--) {
      const ts = parseInt(items[i].dataset.ts || '0', 10);
      if (ts > 0) return ts;
    }
    return null;
  }

  async function _checkAndFetchActiveTypeItems(gen) {
    if (archiveViewActive || (gen != null && gen !== galleryBackfillGen)) return;
    const grid = document.getElementById('media-gallery-grid');
    if (!grid) return;

    if (activeTypes.size === 1 && activeTypes.has('link')) {
      return;
    }

    const typesKey = activeTypes.size > 0 ? [...activeTypes].sort().join(',') : 'all';
    if (typesAllLoaded.has(typesKey)) return;
    if (activeTypes.size === 0 && galleryAllLoaded) return;

    let visibleCount = 0;
    if (activeTypes.size === 0) {
      visibleCount = grid.querySelectorAll('.gallery-item:not(.type-hidden)').length;
    } else {
      for (const t of activeTypes) {
        visibleCount += grid.querySelectorAll(`.gallery-item[data-type="${t}"]:not(.type-hidden)`).length;
      }
    }

    let expectedCount = Infinity;
    if (currentGalleryStats && currentGalleryStats.by_type) {
      if (activeTypes.size === 1) {
        const onlyType = [...activeTypes][0];
        expectedCount = currentGalleryStats.by_type[onlyType]?.count || 0;
      } else if (activeTypes.size > 1) {
        expectedCount = [...activeTypes].reduce((acc, t) => acc + (currentGalleryStats.by_type[t]?.count || 0), 0);
      } else if (activeTypes.size === 0 && currentGalleryStats.total) {
        expectedCount = currentGalleryStats.total;
      }
    }

    // If screen view is already filled with media, don't load additional media!
    const isScreenFilled = grid.scrollHeight > grid.clientHeight + 60 && visibleCount >= 10;
    if (isScreenFilled) {
      if (visibleCount >= expectedCount && expectedCount > 0) {
        typesAllLoaded.add(typesKey);
        _syncGalleryNotices();
      }
      return;
    }

    if (visibleCount < expectedCount && !typesAllLoaded.has(typesKey)) {
      const oldestTs = _getOldestTimestampForActiveTypes(grid);
      await _loadGalleryPage(oldestTs);
    }

    visibleCount = 0;
    if (activeTypes.size === 0) {
      visibleCount = grid.querySelectorAll('.gallery-item:not(.type-hidden)').length;
    } else {
      for (const t of activeTypes) {
        visibleCount += grid.querySelectorAll(`.gallery-item[data-type="${t}"]:not(.type-hidden)`).length;
      }
    }
    if (visibleCount >= expectedCount && expectedCount > 0) {
      typesAllLoaded.add(typesKey);
      _syncGalleryNotices();
    }
  }

  async function _loadGalleryPageForType(type) {
    const grid = document.getElementById('media-gallery-grid');
    if (!grid || !currentChat) return;
    try {
      const url = API + '/media?chat_id=' + encodeURIComponent(currentChat.id) +
                  '&chat_type=' + encodeURIComponent(currentChat.type) +
                  '&types=' + encodeURIComponent(type);
      const res = await fetch(url);
      if (!res.ok) return;
      const items = await res.json();
      currentGalleryItems.push(...items);
      _insertGalleryGridBatch(grid, items);
    } catch (e) {
      console.warn('Failed to fetch gallery items for type', type, e);
    }
  }

  function _checkGalleryLoadMore() {
    if (archiveViewActive || galleryLoadingMore) return;
    const grid = document.getElementById('media-gallery-grid');
    if (!grid) return;

    if (galleryScrollDirection === 'up') return;

    if (activeTypes.size === 1 && activeTypes.has('link')) {
      if (linksLoadingMore || linksAllLoaded) return;
      const distFromBottom = grid.scrollHeight - (grid.scrollTop + grid.clientHeight);
      if (distFromBottom <= 800 || grid.scrollHeight <= grid.clientHeight) {
        _loadMoreLinks();
      }
      return;
    }

    const typesKey = activeTypes.size > 0 ? [...activeTypes].sort().join(',') : 'all';
    if (typesAllLoaded.has(typesKey)) return;
    if (activeTypes.size === 0 && galleryAllLoaded) return;

    const oldestTs = _getOldestTimestampForActiveTypes(grid);
    if (oldestTs == null) {
      _loadGalleryPage(null);
      return;
    }

    const distFromBottom = grid.scrollHeight - (grid.scrollTop + grid.clientHeight);
    if (distFromBottom <= 800 || grid.scrollHeight <= grid.clientHeight) {
      _loadGalleryPage(oldestTs);
    }
  }

  async function _ensureMediaLoadedForViewport(targetTs, anchorEl, anchorTopBefore, gen) {
    if (archiveViewActive || gen !== galleryBackfillGen) return;
    await _checkAndFetchActiveTypeItems(gen);
    _hydrateVisibleGalleryItems();
  }

  function onGalleryScroll() {
    galleryIsScrolling = true;
    clearTimeout(galleryScrollIdleTimer);
    // Pause queued tasks while user is actively scrolling
    videoProbeQueue.length = 0;

    const grid = document.getElementById('media-gallery-grid');
    if (grid && !archiveViewActive) {
      const currentScrollTop = grid.scrollTop;
      if (currentScrollTop < lastGalleryScrollTop - 6) {
        galleryScrollDirection = 'up';
        // Immediately abort any in-flight load when scrolling upwards away from bottom
        if (galleryLoadingMore && galleryCurrentAbortController) {
          galleryCurrentAbortController.abort();
          galleryCurrentAbortController = null;
        }
      } else if (currentScrollTop > lastGalleryScrollTop + 6) {
        galleryScrollDirection = 'down';
      }
      lastGalleryScrollTop = currentScrollTop;
    }

    if (galleryLoadingMore || linksLoadingMore) {
      galleryPendingScrollCheck = true;
    } else {
      _checkGalleryLoadMore();
    }

    galleryScrollIdleTimer = setTimeout(() => {
      galleryIsScrolling = false;
      scheduleVisibleVideoExtraction();
    }, 250);
  }

  function scheduleVisibleVideoExtraction() {
    if (galleryIsScrolling || galleryLoadingMore) {
      return;
    }
    const container = archiveViewActive
      ? document.getElementById('media-archive-tree')
      : document.getElementById('media-gallery-grid');
    if (!container) return;

    const cRect = container.getBoundingClientRect();
    const videoCells = [
      ...container.querySelectorAll(
        '.gallery-item[data-type="video"][data-hydrated="true"], .gallery-item[data-type="gif"][data-hydrated="true"]'
      )
    ];
    for (const cell of videoCells) {
      const archivePath = cell.dataset.archivePath;
      if (!archivePath) continue;
      const img = cell.querySelector('.gallery-thumb');
      if (!img) continue;

      if (videoThumbCache.has(archivePath)) {
        img.src = videoThumbCache.get(archivePath);
        cell.classList.remove('is-loading-thumb');
        continue;
      }

      // Check if intersecting container's actual visible viewport (with 100px buffer)
      const r = cell.getBoundingClientRect();
      const isVisibleOnScreen = (r.bottom >= cRect.top - 100) && (r.top <= cRect.bottom + 100);
      if (isVisibleOnScreen) {
        enqueueVideoThumbnail(cell, img, cell.dataset.src, archivePath, cell._msg);
      }
    }
  }
