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
            ? t('settings.index_progress', { pct, indexed: fmtN(d.indexed_msgs), total: fmtN(d.total_msgs) })
            : t('settings.index_starting');
        if (!d.running) {
          document.getElementById('index-progress-bar').style.width = '100%';
          document.getElementById('index-progress-label').textContent = t('common.done');
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
      const prefKey = btn.dataset.pref;
      const prefVal = btn.dataset.value;
      prefs[prefKey] = prefVal;
      applyPrefs();
      fetch(API + '/preferences', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: prefKey, value: prefVal })
      });
    });
  });

  const langSelect = document.getElementById('pref-language');
  if (langSelect) {
    langSelect.addEventListener('change', async () => {
      const prefVal = langSelect.value || 'auto';
      prefs.language = prefVal;
      await fetch(API + '/preferences', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: 'language', value: prefVal })
      });
      try {
        if (currentChat) {
          sessionStorage.setItem('wab_reopen_chat', JSON.stringify({ id: currentChat.id, type: currentChat.type }));
        } else {
          sessionStorage.removeItem('wab_reopen_chat');
        }
      } catch (_) {}
      window.location.reload();
    });
  }

  document.getElementById('index-all-btn').addEventListener('click', async () => {
    let sizeBytes = 0;
    try {
      const r = await fetch(API + '/index/source-size');
      const d = await r.json();
      sizeBytes = d.bytes || 0;
    } catch (_) {}

    if (sizeBytes > 150 * 1024 * 1024) {
      const mb = Math.round(sizeBytes / 1024 / 1024);
      if (!confirm(t('settings.index_large_confirm', { mb }))) return;
    }
    if (!confirm(t('settings.index_all_confirm'))) return;

    const res = await fetch(API + '/index/all', { method: 'POST' });
    const data = await res.json();
    if (data.error) { alert(data.error); return; }

    document.getElementById('index-all-btn').disabled = true;
    document.getElementById('index-all-progress').style.display = 'block';
    document.getElementById('index-progress-bar').style.width = '0%';
    document.getElementById('index-progress-label').textContent = t('settings.index_starting');
    _startBulkIndexPoll();
  });

  document.getElementById('clear-index-btn').addEventListener('click', async () => {
    if (!confirm(t('settings.clear_index_confirm'))) return;
    const btn = document.getElementById('clear-index-btn');
    await fetch(API + '/index/clear', { method: 'POST' });
    const orig = btn.textContent;
    btn.textContent = t('settings.cleared');
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
      optBtn.textContent = t('settings.optimizing');
      if (statusEl) {
        statusEl.textContent = t('settings.optimizing_status');
        statusEl.style.display = 'block';
      }
      try {
        const res = await fetch(API + '/index/optimize', { method: 'POST' });
        const data = await res.json();
        if (data.error) {
          if (statusEl) {
            statusEl.textContent = t('settings.optimize_error', { error: data.error });
            statusEl.style.display = 'block';
          }
        } else {
          const mbReclaimed = (data.reclaimed / (1024 * 1024)).toFixed(1);
          const mbCurrent = (data.size_after / (1024 * 1024)).toFixed(1);
          if (statusEl) {
            if (data.reclaimed > 0) {
              statusEl.textContent = t('settings.optimize_reclaimed', { reclaimed: mbReclaimed, current: mbCurrent });
            } else {
              statusEl.textContent = t('settings.optimize_optimal', { current: mbCurrent });
            }
            statusEl.style.display = 'block';
          }
        }
      } catch (_) {
        if (statusEl) {
          statusEl.textContent = t('settings.optimize_failed');
          statusEl.style.display = 'block';
        }
      } finally {
        optBtn.disabled = false;
        optBtn.textContent = orig;
      }
    });
  }
