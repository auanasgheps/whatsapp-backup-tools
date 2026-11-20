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
    videoProbeQueue.push({ cell, img, src, archivePath, msg: msg || cell._msg });
    drainVideoProbeQueue();
  }

  function drainVideoProbeQueue() {
    while (activeVideoProbes < MAX_CONCURRENT_VIDEO_PROBES && videoProbeQueue.length > 0) {
      const next = videoProbeQueue.shift();
      if (!next.cell.isConnected || next.cell.dataset.hydrated !== 'true') {
        continue;
      }
      if (videoThumbCache.has(next.archivePath)) {
        next.img.src = videoThumbCache.get(next.archivePath);
        next.cell.classList.remove('is-loading-thumb');
        continue;
      }
      activeVideoProbes++;
      extractVideoFirstFrame(next);
    }
  }

  function extractVideoFirstFrame(task) {
    const vid = document.createElement('video');
    vid.src = task.src;
    vid.muted = true;
    vid.preload = 'auto';
    let settled = false;

    const safetyTimeout = setTimeout(() => {
      if (task.cell && task.cell.isConnected) {
        task.cell.classList.remove('is-loading-thumb');
        if (task.img) task.img.classList.add('gallery-video-thumb--err');
      }
      finish();
    }, 6000);

    function finish() {
      if (settled) return;
      settled = true;
      clearTimeout(safetyTimeout);
      vid.src = '';
      activeVideoProbes--;
      drainVideoProbeQueue();
    }

    function captureFrame() {
      try {
        const canvas = document.createElement('canvas');
        canvas.width = vid.videoWidth || 320;
        canvas.height = vid.videoHeight || 180;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(vid, 0, 0, canvas.width, canvas.height);
        const dataUrl = canvas.toDataURL('image/jpeg', 0.8);
        videoThumbCache.set(task.archivePath, dataUrl);
        if (task.cell.isConnected && task.cell.dataset.hydrated === 'true') {
          task.img.src = dataUrl;
          task.cell.classList.remove('is-loading-thumb');
          task.img.classList.remove('gallery-video-thumb--err');
        }
      } catch {
        if (task.cell.isConnected) {
          task.cell.classList.remove('is-loading-thumb');
          task.img.classList.add('gallery-video-thumb--err');
        }
      } finally {
        finish();
      }
    }

    vid.addEventListener('loadeddata', () => {
      captureFrame();
    }, { once: true });

    vid.addEventListener('error', () => {
      try {
        if (task.cell.isConnected) {
          task.cell.classList.remove('is-loading-thumb');
          task.img.classList.add('gallery-video-thumb--err');
        }
      } finally {
        finish();
      }
    }, { once: true });
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
