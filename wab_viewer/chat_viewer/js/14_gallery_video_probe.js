function clearVideoProbeQueue() {
    videoProbeQueue.length = 0;
  }

  function cancelVideoThumbnail(cell) {
    const idx = videoProbeQueue.findIndex(item => item.cell === cell);
    if (idx !== -1) {
      videoProbeQueue.splice(idx, 1);
    }
  }

  function enqueueVideoThumbnail(cell, img, src, archivePath, msg) {
    if (videoThumbCache.has(archivePath)) {
      img.src = videoThumbCache.get(archivePath);
      cell.classList.remove('is-loading-thumb');
      return;
    }
    if (videoProbeQueue.some(item => item.cell === cell)) {
      return;
    }
    enqueueVideoFrameProbe({
      cell,
      img,
      src,
      archivePath,
      msg: msg || cell._msg,
      isAlive: () => cell.isConnected && cell.dataset.hydrated === 'true',
      onCached: (dataUrl) => {
        img.src = dataUrl;
        cell.classList.remove('is-loading-thumb');
      },
      onFrame: (dataUrl) => {
        if (cell.isConnected && cell.dataset.hydrated === 'true') {
          img.src = dataUrl;
          cell.classList.remove('is-loading-thumb');
          img.classList.remove('gallery-video-thumb--err');
        }
      },
      onError: () => {
        if (cell.isConnected) {
          cell.classList.remove('is-loading-thumb');
          img.classList.add('gallery-video-thumb--err');
        }
      },
    });
  }

  function hydrateGalleryItem(cell) {
    if (cell.dataset.hydrated === 'true') {
      return;
    }
    cell.dataset.hydrated = 'true';
    const mt = cell.dataset.type;
    const src = cell.dataset.src;
    const img = cell.querySelector('.gallery-thumb');
    if (!img || !src) {
      return;
    }

    if (mt === 'image' || mt === 'sticker') {
      img.src = src;
      img.addEventListener('error', () => {
        buildMissingGalleryCard(cell, cell._msg);
      }, { once: true });
    } else if (mt === 'video' || mt === 'gif') {
      const archivePath = cell.dataset.archivePath;
      if (cell.dataset.thumbSrc) {
        img.src = cell.dataset.thumbSrc;
        // Do not remove 'is-loading-thumb' here; img.onload removes it once bytes arrive,
        // or img.onerror triggers fallback video frame probe.
      } else if (videoThumbCache.has(archivePath)) {
        img.src = videoThumbCache.get(archivePath);
        cell.classList.remove('is-loading-thumb');
      } else {
        cell.classList.add('is-loading-thumb');
        scheduleVisibleVideoExtraction();
      }
    } else if (mt === 'link') {
      img.src = src;
      img.onerror = () => {
        delete cell.dataset.src;
        const box = cell.querySelector('.gallery-rich-link-media');
        if (box) box.innerHTML = '<span class="gallery-rich-link-fallback-icon">🔗</span>';
      };
    }
  }

  function dehydrateGalleryItem(cell) {
    if (cell.dataset.hydrated !== 'true') {
      return;
    }
    const mt = cell.dataset.type;
    if (mt === 'link') {
      return;
    }
    cell.dataset.hydrated = 'false';
    const img = cell.querySelector('.gallery-thumb');
    if (!img) {
      return;
    }

    if (mt === 'video' || mt === 'gif') {
      cancelVideoThumbnail(cell);
    }
    img.src = EMPTY_GALLERY_PIX;
  }

  function handleMediaIntersection(entries) {
    for (const entry of entries) {
      const cell = entry.target;
      if (entry.isIntersecting) {
        hydrateGalleryItem(cell);
      } else {
        dehydrateGalleryItem(cell);
      }
    }
  }
