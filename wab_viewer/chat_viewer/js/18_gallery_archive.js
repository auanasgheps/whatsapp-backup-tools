async function _loadAllArchivePages() {
    if (archiveLoadingAll || archiveAllLoaded) return;
    archiveLoadingAll = true;
    const gen = galleryOpenGen;
    const requestedChatId = currentChat ? currentChat.id : null;
    const tree = document.getElementById('media-archive-tree');
    if (tree && !currentArchiveItems.length) {
      tree.innerHTML = `<div class="gallery-filter-loading"><span class="gallery-filter-spinner"></span><span>${esc(t('gallery.loading_archive'))}</span></div>`;
    }
    let before = currentArchiveItems.length
      ? currentArchiveItems[currentArchiveItems.length - 1].timestamp_ms
      : null;
    let consecutiveErrors = 0;
    try {
      while (archiveViewActive && consecutiveErrors < 3 && !archiveAllLoaded) {
        if (gen !== galleryOpenGen || !currentChat || currentChat.id !== requestedChatId) return;
        const url = API + '/media?chat_id=' + encodeURIComponent(requestedChatId) +
                    '&chat_type=' + encodeURIComponent(currentChat.type) +
                    '&types=all&archived=1' +
                    (before != null ? '&before=' + before : '');
        const res = await fetch(url);
        if (!res.ok) {
          consecutiveErrors++;
          await new Promise(r => setTimeout(r, 200));
          continue;
        }
        const items = await res.json();
        if (gen !== galleryOpenGen || !currentChat || currentChat.id !== requestedChatId) return;
        if (!archiveViewActive) return;

        if (!items || !items.length) {
          archiveAllLoaded = true;
          break;
        }

        consecutiveErrors = 0;
        currentArchiveItems.push(...items);
        before = items[items.length - 1].timestamp_ms;

        // Render newly available archive items
        buildArchiveView(currentArchiveItems, activeArchiveDir());
        if (activeTypes.size > 0) { _applyTypeFilter(); } else { _syncMonthHeaders(); }

        if (items.length < 100) {
          archiveAllLoaded = true;
          break;
        }
        await new Promise(r => setTimeout(r, 10));
      }
    } catch (err) {
      console.error('Failed to load archive pages:', err);
    } finally {
      archiveLoadingAll = false;
      if (archiveViewActive && gen === galleryOpenGen && currentChat && currentChat.id === requestedChatId) {
        buildArchiveView(currentArchiveItems, activeArchiveDir());
        if (activeTypes.size > 0) { _applyTypeFilter(); } else { _syncMonthHeaders(); }
        scheduleVisibleVideoExtraction();
      }
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
        buildArchiveView(currentArchiveItems, activeArchiveDir());
        if (activeTypes.size > 0) { _applyTypeFilter(); } else { _syncMonthHeaders(); }
        scheduleVisibleVideoExtraction();
        if (!archiveAllLoaded) {
          _loadAllArchivePages();
        }
      }
    });
  });

  document.querySelectorAll('.archive-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.archive-tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      buildArchiveView(currentArchiveItems, btn.dataset.dir);
      if (activeTypes.size > 0) { _applyTypeFilter(); } else { _syncMonthHeaders(); }
      scheduleVisibleVideoExtraction();
      if (!archiveAllLoaded) {
        _loadAllArchivePages();
      }
    });
  });

  document.getElementById('archive-expand-all').addEventListener('click', () => {
    document.querySelectorAll('.archive-year').forEach(el => {
      el.classList.add('open');
      if (!el._populated) {
        _populateArchiveYear(el);
      }
    });
    scheduleVisibleVideoExtraction();
  });
  document.getElementById('archive-collapse-all').addEventListener('click', () => {
    document.querySelectorAll('.archive-year').forEach(el => el.classList.remove('open'));
  });
