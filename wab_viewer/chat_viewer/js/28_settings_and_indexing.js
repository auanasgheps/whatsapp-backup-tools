// ---- init ----------------------------------------------------------------

  let _bulkIndexPollGen = 0;

  function _startBulkIndexPoll() {
    const gen = ++_bulkIndexPollGen;
    async function poll() {
      if (gen !== _bulkIndexPollGen) return;
      try {
        const r = await fetch(API + '/index/progress');
        const d = await r.json();
        if (gen !== _bulkIndexPollGen) return;
        const pct = d.total_msgs > 0 ? Math.round(d.indexed_msgs / d.total_msgs * 100) : 0;
        document.getElementById('index-progress-bar').style.width = pct + '%';
        const fmtN = n => n.toLocaleString();
        document.getElementById('index-progress-label').textContent =
          d.total_msgs > 0
            ? pct + '% (' + fmtN(d.indexed_msgs) + ' / ' + fmtN(d.total_msgs) + ' messages)'
            : 'Starting…';
        if (!d.running) {
          document.getElementById('index-progress-bar').style.width = '100%';
          document.getElementById('index-progress-label').textContent = 'Done.';
          document.getElementById('index-all-btn').disabled = false;
          return;
        }
        setTimeout(poll, 1000);
      } catch (_) {
        setTimeout(poll, 2000);
      }
    }
    poll();
  }

  document.getElementById('settings-btn').addEventListener('click', async () => {
    const sBody = document.getElementById('settings-body');
    if (sBody) sBody.scrollTop = 0;
    document.getElementById('settings-modal').classList.add('open');
    try {
      const r = await fetch(API + '/index/progress');
      const d = await r.json();
      if (d.running) {
        document.getElementById('index-all-btn').disabled = true;
        document.getElementById('index-all-progress').style.display = 'block';
        _startBulkIndexPoll();
      }
    } catch (_) {}
  });
  document.getElementById('settings-close').addEventListener('click', () => {
    document.getElementById('settings-modal').classList.remove('open');
  });
  document.getElementById('settings-modal').addEventListener('click', e => {
    if (e.target === e.currentTarget) document.getElementById('settings-modal').classList.remove('open');
  });
  document.querySelectorAll('.pref-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      prefs[btn.dataset.pref] = btn.dataset.value;
      applyPrefs();
      fetch(API + '/preferences', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: btn.dataset.pref, value: btn.dataset.value })
      });
    });
  });

  document.getElementById('index-all-btn').addEventListener('click', async () => {
    let sizeBytes = 0;
    try {
      const r = await fetch(API + '/index/source-size');
      const d = await r.json();
      sizeBytes = d.bytes || 0;
    } catch (_) {}

    if (sizeBytes > 150 * 1024 * 1024) {
      const mb = Math.round(sizeBytes / 1024 / 1024);
      if (!confirm('The database is large (' + mb + ' MB). Indexing may take several minutes. Continue?')) return;
    }
    if (!confirm('Index all chats for search? This will run in the background while you use the viewer.')) return;

    const res = await fetch(API + '/index/all', { method: 'POST' });
    const data = await res.json();
    if (data.error) { alert(data.error); return; }

    document.getElementById('index-all-btn').disabled = true;
    document.getElementById('index-all-progress').style.display = 'block';
    document.getElementById('index-progress-bar').style.width = '0%';
    document.getElementById('index-progress-label').textContent = 'Starting…';
    _startBulkIndexPoll();
  });

  document.getElementById('clear-index-btn').addEventListener('click', async () => {
    if (!confirm('Clear the search index? Indexed data will be deleted. Chats will be re-indexed when you open them.')) return;
    const btn = document.getElementById('clear-index-btn');
    await fetch(API + '/index/clear', { method: 'POST' });
    const orig = btn.textContent;
    btn.textContent = 'Cleared.';
    setTimeout(() => { btn.textContent = orig; }, 2000);
    document.getElementById('index-all-progress').style.display = 'none';
    document.getElementById('index-all-btn').disabled = false;
  });

  const optBtn = document.getElementById('optimize-db-btn');
  if (optBtn) {
    optBtn.addEventListener('click', async () => {
      const statusEl = document.getElementById('optimize-db-status');
      optBtn.disabled = true;
      const orig = optBtn.textContent;
      optBtn.textContent = 'Optimizing…';
      if (statusEl) {
        statusEl.textContent = 'Defragmenting index and compacting database…';
        statusEl.style.display = 'block';
      }
      try {
        const res = await fetch(API + '/index/optimize', { method: 'POST' });
        const data = await res.json();
        if (data.error) {
          if (statusEl) {
            statusEl.textContent = 'Could not optimize: ' + data.error;
            statusEl.style.display = 'block';
          }
        } else {
          const mbReclaimed = (data.reclaimed / (1024 * 1024)).toFixed(1);
          const mbCurrent = (data.size_after / (1024 * 1024)).toFixed(1);
          if (statusEl) {
            if (data.reclaimed > 0) {
              statusEl.textContent = `Compacted! Reclaimed ${mbReclaimed} MB (Current size: ${mbCurrent} MB).`;
            } else {
              statusEl.textContent = `Database is already optimal (${mbCurrent} MB).`;
            }
            statusEl.style.display = 'block';
          }
        }
      } catch (_) {
        if (statusEl) {
          statusEl.textContent = 'Optimization failed.';
          statusEl.style.display = 'block';
        }
      } finally {
        optBtn.disabled = false;
        optBtn.textContent = orig;
      }
    });
  }
