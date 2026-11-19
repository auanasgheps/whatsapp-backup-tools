async function _loadAllArchivePages() {
    if (archiveLoadingAll) return;
    archiveLoadingAll = true;
    let consecutiveErrors = 0;
    try {
      while (archiveViewActive && !galleryAllLoaded && consecutiveErrors < 3) {
        if (galleryLoadingMore) {
          await new Promise(r => setTimeout(r, 40));
          continue;
        }
        const oldest = currentGalleryItems[currentGalleryItems.length - 1]?.timestamp_ms;
        if (oldest == null) {
          await new Promise(r => setTimeout(r, 40));
          continue;
        }
        const countBefore = currentGalleryItems.length;
        await _loadGalleryPage(oldest);
        if (currentGalleryItems.length === countBefore && !galleryAllLoaded) {
          consecutiveErrors++;
        } else {
          consecutiveErrors = 0;
        }
        await new Promise(r => setTimeout(r, 10));
      }
    } finally {
      archiveLoadingAll = false;
    }
  }

  document.querySelectorAll('.media-view-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.media-view-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      archiveViewActive = btn.dataset.view === 'archive';
      document.getElementById('media-gallery-grid').style.display = archiveViewActive ? 'none' : 'grid';
      document.getElementById('media-archive-view').classList.toggle('active', archiveViewActive);
      // if the active filter is secondary-types-only, those items don't exist in archive view — reset to default
      if (archiveViewActive && activeTypes.size > 0 && [...activeTypes].every(t => SECONDARY_TYPES.has(t))) {
        activeTypes = new Set(defaultActiveTypes);
        isDefaultView = true;
      }
      _syncStatPills();
      if (archiveViewActive) {
        buildArchiveView(currentGalleryItems, activeArchiveDir());
        if (activeTypes.size > 0) { _applyTypeFilter(); } else { _syncMonthHeaders(); }
        scheduleVisibleVideoExtraction();
        _loadAllArchivePages();
      }
    });
  });

  document.querySelectorAll('.archive-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.archive-tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      buildArchiveView(currentGalleryItems, btn.dataset.dir);
      if (activeTypes.size > 0) { _applyTypeFilter(); } else { _syncMonthHeaders(); }
      scheduleVisibleVideoExtraction();
      _loadAllArchivePages();
    });
  });

  document.getElementById('archive-expand-all').addEventListener('click', () => {
    document.querySelectorAll('.archive-year').forEach(el => el.classList.add('open'));
    scheduleVisibleVideoExtraction();
  });
  document.getElementById('archive-collapse-all').addEventListener('click', () => {
    document.querySelectorAll('.archive-year').forEach(el => el.classList.remove('open'));
  });
