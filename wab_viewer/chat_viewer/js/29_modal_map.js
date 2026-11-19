// ---- map provider chooser modal ------------------------------------------
  let activeMapTrigger = null;

  function openMapChooser(locData, triggerEl) {
    activeMapTrigger = triggerEl;
    const modal = document.getElementById('map-chooser-modal');
    if (!modal) return;

    const lat = Number(locData.latitude);
    const lng = Number(locData.longitude);
    if (isNaN(lat) || isNaN(lng)) return;

    const titleEl = document.getElementById('map-chooser-title');
    const subtitleEl = document.getElementById('map-chooser-subtitle');
    const copyTextEl = document.getElementById('map-copy-coords-text');
    const copyBtn = document.getElementById('map-copy-coords-btn');

    const formattedCoords = formatCoordinates(lat, lng);
    const placeName = locData.name ? locData.name.trim() : '';
    const address = locData.address ? locData.address.trim() : '';

    if (titleEl) {
      titleEl.textContent = placeName || (locData.isLive ? 'Live Location' : 'Shared Location');
    }
    if (subtitleEl) {
      subtitleEl.textContent = address ? (address + ' · ' + formattedCoords) : formattedCoords;
    }

    // 1. Google Maps: embedded exact coordinates
    const gmapsBtn = document.getElementById('map-provider-google');
    if (gmapsBtn) {
      gmapsBtn.href = 'https://www.google.com/maps/search/?api=1&query=' + lat + ',' + lng;
    }

    // 2. Bing Maps: centers view (cp), sets street zoom (lvl=16), drops named pushpin (sp=point.lat_lng_Name)
    const bingBtn = document.getElementById('map-provider-bing');
    if (bingBtn) {
      const pinLabel = placeName || address || 'Location';
      bingBtn.href = 'https://www.bing.com/maps?cp=' + lat + '~' + lng + '&lvl=16&sp=point.' + lat + '_' + lng + '_' + encodeURIComponent(pinLabel);
    }

    // 3. OpenStreetMap: sets marker pin (mlat/mlon) and centers map (#map=16/lat/lng)
    const osmBtn = document.getElementById('map-provider-osm');
    if (osmBtn) {
      osmBtn.href = 'https://www.openstreetmap.org/?mlat=' + lat + '&mlon=' + lng + '#map=16/' + lat + '/' + lng;
    }

    // Copy Coordinates button
    if (copyBtn && copyTextEl) {
      copyBtn.classList.remove('copied');
      copyTextEl.textContent = 'Copy Coordinates (' + lat.toFixed(4) + ', ' + lng.toFixed(4) + ')';
      copyBtn.onclick = (e) => {
        e.preventDefault();
        const rawCoords = lat + ', ' + lng;
        navigator.clipboard.writeText(rawCoords).then(() => {
          copyBtn.classList.add('copied');
          copyTextEl.textContent = 'Copied to clipboard! ✓';
          setTimeout(() => {
            copyBtn.classList.remove('copied');
            copyTextEl.textContent = 'Copy Coordinates (' + lat.toFixed(4) + ', ' + lng.toFixed(4) + ')';
          }, 2000);
        }).catch(() => {});
      };
    }

    modal.classList.add('open');
    setTimeout(() => {
      gmapsBtn?.focus();
    }, 50);
  }

  function closeMapChooser() {
    const modal = document.getElementById('map-chooser-modal');
    if (!modal || !modal.classList.contains('open')) return;
    modal.classList.remove('open');
    if (activeMapTrigger && typeof activeMapTrigger.focus === 'function') {
      activeMapTrigger.focus();
    }
    activeMapTrigger = null;
  }

  const mapModal = document.getElementById('map-chooser-modal');
  if (mapModal) {
    document.getElementById('map-chooser-close')?.addEventListener('click', closeMapChooser);
    mapModal.addEventListener('click', (e) => {
      if (e.target === mapModal) closeMapChooser();
    });
    mapModal.querySelectorAll('.map-provider-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        setTimeout(closeMapChooser, 150);
      });
    });
  }

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      // 1. Lightbox takes highest priority
      const lb = document.getElementById('img-lightbox');
      if (lb) {
        _closeLb();
        e.preventDefault();
        return;
      }

      // 2. Message details popup
      const detailsPopup = document.getElementById('msg-details-popup');
      if (detailsPopup && detailsPopup.classList.contains('visible')) {
        hideMsgDetails();
        e.preventDefault();
        return;
      }

      // 3. Slide-over drawers (close one active drawer at a time)
      const starredDrawer = document.getElementById('chat-starred-drawer');
      if (starredDrawer && starredDrawer.classList.contains('open')) {
        closeChatStarred();
        e.preventDefault();
        return;
      }
      const infoPanel = document.getElementById('chat-info-panel');
      if (infoPanel && infoPanel.classList.contains('open')) {
        closeChatInfo();
        e.preventDefault();
        return;
      }
      const mediaGallery = document.getElementById('media-gallery');
      if (mediaGallery && mediaGallery.classList.contains('open')) {
        closeMediaGallery();
        e.preventDefault();
        return;
      }

      // 4. Modals (Map chooser, Settings)
      const mapModal = document.getElementById('map-chooser-modal');
      if (mapModal && mapModal.classList.contains('open')) {
        closeMapChooser();
        e.preventDefault();
        return;
      }
      const settingsModal = document.getElementById('settings-modal');
      if (settingsModal && settingsModal.style.display !== 'none' && settingsModal.style.display !== '') {
        closeSettings();
        e.preventDefault();
        return;
      }

      // 5. In-chat search input or bar
      const chatSearchInput = document.getElementById('chat-search-input');
      if (chatSearchInput && document.activeElement === chatSearchInput) {
        chatSearchInput.value = '';
        chatSearchInput.blur();
        e.preventDefault();
        return;
      }

      // 6. Global sidebar search input
      const searchInput = document.getElementById('search-input');
      if (searchInput && (document.activeElement === searchInput || searchInput.value)) {
        clearActiveSidebarSearch();
        searchInput.blur();
        e.preventDefault();
        return;
      }
      return;
    }

    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      const searchInput = document.getElementById('search-input');
      if (searchInput) {
        searchInput.focus();
        searchInput.select();
      }
      return;
    }

    if (e.key === '/') {
      const tag = document.activeElement ? document.activeElement.tagName.toLowerCase() : '';
      const isEditable = document.activeElement && (document.activeElement.isContentEditable || tag === 'input' || tag === 'textarea');
      if (!isEditable) {
        e.preventDefault();
        const searchInput = document.getElementById('search-input');
        if (searchInput) {
          searchInput.focus();
          searchInput.select();
        }
      }
    }
  });
