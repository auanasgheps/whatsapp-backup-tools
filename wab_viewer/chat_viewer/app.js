(function () {
  'use strict';

  const outputRoot = window.OUTPUT_ROOT;
  let allChats = [];
  let currentChat = null;
  let msgList = [];
  let domNodes = 0;
  let prefs = { theme: 'dark', date_format: 'DD/MM/YYYY', font_size: 'medium' };

  async function loadPrefs() {
    const res = await fetch('/api/preferences');
    prefs = await res.json();
    applyPrefs();
  }

  function applyPrefs() {
    document.documentElement.dataset.theme = prefs.theme;
    const scheme = prefs.theme === 'light' ? 'light' : 'dark';
    document.documentElement.style.colorScheme = scheme;
    document.getElementById('meta-color-scheme').content = scheme;
    document.getElementById('date-picker-input').style.colorScheme = scheme;
    const sizeMap = { small: '13px', medium: '15px', large: '17px' };
    document.documentElement.style.setProperty('--font-size', sizeMap[prefs.font_size] || '15px');
    document.querySelectorAll('.pref-btn').forEach(btn => {
      btn.classList.toggle('active', prefs[btn.dataset.pref] === btn.dataset.value);
    });
  }
  const MAX_DOM = 100;

  // ---- util ----------------------------------------------------------------

  function fmtTime(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    return d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  function fmtDateSep(ts) {
    const d = new Date(ts);
    const dd = String(d.getDate()).padStart(2, '0');
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const yyyy = String(d.getFullYear());
    if (prefs.date_format === 'MM/DD/YYYY') return `${mm}/${dd}/${yyyy}`;
    if (prefs.date_format === 'YYYY/MM/DD') return `${yyyy}/${mm}/${dd}`;
    return `${dd}/${mm}/${yyyy}`;
  }

  function dayKey(ts) {
    const d = new Date(ts);
    return d.getFullYear() * 10000 + (d.getMonth() + 1) * 100 + d.getDate();
  }

  function monthKey(ts) {
    const d = new Date(ts);
    return d.getFullYear() * 100 + (d.getMonth() + 1);
  }

  function fmtMonthHeader(ts) {
    const d = new Date(ts);
    if (d.getFullYear() === new Date().getFullYear()) {
      return d.toLocaleString('default', { month: 'long' });
    }
    return d.toLocaleString('default', { month: 'long', year: 'numeric' });
  }

  function activeArchiveDir() {
    // Returns the selected Sent/Received direction, or null for group chats (no tabs)
    if (!currentChat || currentChat.type !== 'contact') return null;
    const active = document.querySelector('.archive-tab.active');
    return active ? active.dataset.dir : null;
  }

  function archiveSegments(archivePath) {
    const parts = archivePath.split('/');
    // Contacts/<name>/<year>/Sent|Received/<file>  → parts[0]='Contacts'
    // Groups/<name>/<year>/<file>                  → parts[0]='Groups'
    if (parts[0] === 'Groups') return { year: parts[2], direction: null };
    return { year: parts[2], direction: parts[3] };
  }

  function buildArchiveView(items, direction) {
    const tree = document.getElementById('media-archive-tree');
    tree.innerHTML = '';

    const filtered = items.filter(m => m.archive_path &&
      (direction === null || archiveSegments(m.archive_path).direction === direction));

    if (!filtered.length) {
      tree.innerHTML = galleryAllLoaded
        ? '<div style="color:var(--text-muted);padding:16px">No media in this chat.</div>'
        : '<div style="color:var(--text-muted);padding:16px">Loading…</div>';
      return;
    }

    const byYear = {};
    for (const msg of filtered) {
      const { year } = archiveSegments(msg.archive_path);
      (byYear[year] = byYear[year] || []).push(msg);
    }

    for (const year of Object.keys(byYear).sort((a, b) => b - a)) {
      const block = _createArchiveYearBlock(year);
      const grid = block.querySelector('.archive-year-grid');
      const countEl = block.querySelector('.archive-year-count');
      countEl.textContent = byYear[year].length;

      let lastMonth = null;
      for (const msg of byYear[year]) {
        const mk = monthKey(msg.timestamp_ms);
        if (mk !== lastMonth) {
          const mhdr = document.createElement('div');
          mhdr.className = 'gallery-month-header';
          mhdr.dataset.month = mk;
          mhdr.textContent = new Date(msg.timestamp_ms).toLocaleString('default', { month: 'long' });
          grid.appendChild(mhdr);
          lastMonth = mk;
        }
        grid.appendChild(renderGalleryItem(msg));
      }

      tree.appendChild(block);
    }
  }

  function _createArchiveYearBlock(year) {
    const block = document.createElement('div');
    block.className = 'archive-year open';
    block.dataset.year = year;
    const hdr = document.createElement('div');
    hdr.className = 'archive-year-header';
    hdr.innerHTML =
      `<span class="archive-year-chevron">&#9658;</span><span>${year}</span>` +
      `<span class="archive-year-count" style="margin-left:auto;font-weight:400;color:var(--text-muted);font-size:12px">0</span>`;
    hdr.addEventListener('click', () => block.classList.toggle('open'));
    const grid = document.createElement('div');
    grid.className = 'archive-year-grid';
    block.appendChild(hdr);
    block.appendChild(grid);
    return block;
  }

  function _appendToArchiveView(items, direction) {
    const tree = document.getElementById('media-archive-tree');
    // clear "No media." placeholder if present
    if (tree.querySelector('div:not(.archive-year)')) tree.innerHTML = '';

    const filtered = items.filter(m => m.archive_path &&
      (direction === null || archiveSegments(m.archive_path).direction === direction));
    if (!filtered.length) return;

    for (const msg of filtered) {
      const { year } = archiveSegments(msg.archive_path);

      let block = tree.querySelector(`.archive-year[data-year="${year}"]`);
      if (!block) {
        block = _createArchiveYearBlock(year);
        // insert in descending year order
        const after = [...tree.querySelectorAll('.archive-year')]
          .find(el => parseInt(el.dataset.year) < parseInt(year));
        tree.insertBefore(block, after || null);
      }

      const grid = block.querySelector('.archive-year-grid');
      const countEl = block.querySelector('.archive-year-count');
      countEl.textContent = parseInt(countEl.textContent || '0') + 1;

      const mk = monthKey(msg.timestamp_ms);
      let monthHdr = grid.querySelector(`.gallery-month-header[data-month="${mk}"]`);
      if (!monthHdr) {
        monthHdr = document.createElement('div');
        monthHdr.className = 'gallery-month-header';
        monthHdr.dataset.month = mk;
        monthHdr.textContent = new Date(msg.timestamp_ms).toLocaleString('default', { month: 'long' });
        // insert before the first section with an older month key
        const olderHdr = [...grid.querySelectorAll('.gallery-month-header')]
          .find(el => parseInt(el.dataset.month) < mk);
        grid.insertBefore(monthHdr, olderHdr || null);
      }
      // insert item after its month header, before the next month header
      const nextSection = [...grid.querySelectorAll('.gallery-month-header')]
        .find(el => parseInt(el.dataset.month) < mk) || null;
      grid.insertBefore(renderGalleryItem(msg), nextSection);
    }
  }

  function esc(s) {
    if (s == null) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function linkify(text) {
    if (!text) return '';
    const urlRe = /https?:\/\/[^\s<>"]+/g;
    let result = '';
    let last = 0;
    let m;
    while ((m = urlRe.exec(text)) !== null) {
      result += esc(text.slice(last, m.index));
      let url = m[0];
      // strip trailing punctuation that is unlikely to be part of the URL
      url = url.replace(/[.,;:!?)\\]'"]+$/, '');
      result += '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(url) + '</a>';
      last = m.index + url.length;
      // advance past trailing chars we stripped so they get picked up as plain text
      urlRe.lastIndex = m.index + m[0].length;
    }
    result += esc(text.slice(last));
    return result;
  }

  function waFormat(text) {
    if (!text) return '';

    // 1. Extract ``` code blocks — protect from any inner formatting
    const codeBlocks = [];
    text = text.replace(/```([\s\S]*?)```/g, (_, content) => {
      codeBlocks.push(content);
      return '\x00CB' + (codeBlocks.length - 1) + '\x00';
    });

    // 2. Split on URLs so they bypass formatting but still become links
    const urlRe = /https?:\/\/[^\s<>"]+/g;
    const parts = [];
    let last = 0;
    let m;
    while ((m = urlRe.exec(text)) !== null) {
      if (m.index > last) parts.push({ type: 'text', val: text.slice(last, m.index) });
      let url = m[0].replace(/[.,;:!?)\\]'"]+$/, '');
      parts.push({ type: 'url', val: url });
      last = m.index + m[0].length;
      urlRe.lastIndex = last;
    }
    if (last < text.length) parts.push({ type: 'text', val: text.slice(last) });

    // 3. Format each non-URL segment
    const codespans = [];
    const formatted = parts.map(p => {
      if (p.type === 'url') {
        const u = esc(p.val);
        return '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + u + '</a>';
      }
      let s = esc(p.val);

      // Protect inline code spans from inner formatting
      s = s.replace(/`([^`]+)`/g, (_, inner) => {
        codespans.push(inner);
        return '\x00CS' + (codespans.length - 1) + '\x00';
      });

      // Inline: bold *text*, italic _text_, strikethrough ~text~
      // Markers must touch non-space on both inner sides
      s = s.replace(/\*(\S(?:[^*]*\S)?)\*/g, '<strong>$1</strong>');
      s = s.replace(/_(\S(?:[^_]*\S)?)_/g, '<em>$1</em>');
      s = s.replace(/~(\S(?:[^~]*\S)?)~/g, '<s>$1</s>');

      // Restore inline code spans
      s = s.replace(/\x00CS(\d+)\x00/g, (_, i) => '<code>' + esc(codespans[+i]) + '</code>');

      // Block-level: process line by line
      const lines = s.split('\n');
      let inUl = false, inOl = false;
      const out = [];
      for (let i = 0; i < lines.length; i++) {
        let ln = lines[i];
        const next = lines[i + 1];

        if (/^###\s/.test(ln))      { closeLists(); out.push('<h3>' + ln.slice(4) + '</h3>'); continue; }
        if (/^##\s/.test(ln))       { closeLists(); out.push('<h2>' + ln.slice(3) + '</h2>'); continue; }
        if (/^#\s/.test(ln))        { closeLists(); out.push('<h1>' + ln.slice(2) + '</h1>'); continue; }
        if (/^&gt;\s?/.test(ln))    { closeLists(); out.push('<blockquote>' + ln.replace(/^&gt;\s?/, '') + '</blockquote>'); continue; }

        if (/^[-*]\s/.test(ln)) {
          if (!inUl) { inUl = true; out.push('<ul>'); }
          out.push('<li>' + ln.slice(2) + '</li>');
          if (!next || !/^[-*]\s/.test(next)) { inUl = false; out.push('</ul>'); }
          continue;
        }
        if (/^\d+\.\s/.test(ln)) {
          if (!inOl) { inOl = true; out.push('<ol>'); }
          out.push('<li>' + ln.replace(/^\d+\.\s/, '') + '</li>');
          if (!next || !/^\d+\.\s/.test(next)) { inOl = false; out.push('</ol>'); }
          continue;
        }

        closeLists();
        out.push(ln);
      }
      closeLists();
      return out.join('<br>');

      function closeLists() {
        if (inUl) { inUl = false; out.push('</ul>'); }
        if (inOl) { inOl = false; out.push('</ol>'); }
      }
    }).join('');

    // 4. Restore code blocks
    return formatted.replace(/\x00CB(\d+)\x00/g, (_, i) =>
      '<pre><code>' + esc(codeBlocks[+i]) + '</code></pre>'
    );
  }

  function highlight(text, q) {
    if (!q || !text) return waFormat(text || '');
    const words = q.trim().split(/\s+/);
    let result = waFormat(text);
    for (const w of words) {
      const re = new RegExp(w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
      result = result.replace(/(<[^>]+>)|([^<]+)/g, (_, tag, txt) =>
        tag ? tag : txt.replace(re, mm => '<mark>' + mm + '</mark>')
      );
    }
    return result;
  }

  function makeDateSeparator(ts) {
    const el = document.createElement('div');
    el.className = 'date-separator';
    el.dataset.dayKey = dayKey(ts);
    el.innerHTML = `<span>${fmtDateSep(ts)}</span>`;
    return el;
  }

  // ---- load chats ----------------------------------------------------------

  async function loadChats() {
    document.getElementById('chat-list').innerHTML = '<div id="chat-list-notice"><div class="loading-spinner"></div>Loading Chats…</div>';
    const res = await fetch('/api/chats');
    allChats = await res.json();
    renderChatList();
  }

  let currentFilter = 'all';

  document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      currentFilter = btn.dataset.filter;
      document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      renderChatList();
    });
  });

  function resetFilterTo(filter) {
    if (currentFilter === filter) return;
    currentFilter = filter;
    document.querySelectorAll('.filter-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.filter === filter);
    });
    renderChatList();
  }

  let _indexPollGen = 0;

  function _startIndexPoll(chat) {
    const gen = ++_indexPollGen;
    const params = new URLSearchParams({chat_id: chat.id, chat_type: chat.type});
    async function poll() {
      if (gen !== _indexPollGen) return;
      try {
        const r = await fetch('/api/chat-index-status?' + params);
        const d = await r.json();
        if (gen !== _indexPollGen) return;
        if (d.status === 'done') {
          document.querySelector(`.chat-item[data-id="${chat.id}"][data-type="${chat.type}"] .index-spinner`)?.remove();
          return;
        }
        const liveEl = document.querySelector(`.chat-item[data-id="${chat.id}"][data-type="${chat.type}"]`);
        if (liveEl && !liveEl.querySelector('.index-spinner')) {
          const sp = document.createElement('span');
          sp.className = 'index-spinner';
          liveEl.querySelector('.chat-name').appendChild(sp);
        }
        setTimeout(poll, 500);
      } catch (_) {
        setTimeout(poll, 1000);
      }
    }
    poll();
  }

  const MEDIA_LABELS = {image:'📷 Photo', video:'🎥 Video', audio:'🎵 Voice message',
                        gif:'🎞 GIF', sticker:'🎭 Sticker', document:'📄 Document', link:'🔗 Link'};
  function chatPreview(chat) {
    const prefix = chat.last_msg_from_me ? 'You: ' : '';
    if (chat.last_msg_type !== 'text') {
      return prefix + (MEDIA_LABELS[chat.last_msg_type] || '📎 Media');
    }
    const text = chat.last_msg_preview || '';
    return (prefix + esc(text.slice(0, 60))) || '…';
  }

  function renderChatList() {
    const list = document.getElementById('chat-list');
    list.innerHTML = '';
    const visible = currentFilter === 'all' ? allChats : allChats.filter(c => c.type === currentFilter);
    visible.forEach(chat => {
      const el = document.createElement('div');
      el.className = 'chat-item' + (chat.type === 'group' ? ' group' : '');
      el.dataset.id = chat.id;
      el.dataset.type = chat.type;
      el.innerHTML = `<div class="chat-name">${esc(chat.display_name)}</div><div class="chat-meta">${chatPreview(chat)} · ${fmtTime(chat.newest_ts)}</div>`;
      el.addEventListener('click', () => selectChat(chat, el));
      list.appendChild(el);
    });
  }

  // ---- select chat ---------------------------------------------------------

  async function selectChat(chat, el) {
    _indexPollGen++;
    ++_loadGen;
    loading = false;
    document.querySelectorAll('.chat-item').forEach(e => e.classList.remove('active'));
    el.classList.add('active');
    el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    closeChatInfo();
    currentChat = chat;
    msgList = [];
    noMoreOlder = false;
    noMoreNewer = false;
    pendingLoad = null;

    const scroll = document.getElementById('message-scroll');
    scroll.innerHTML = '';
    domNodes = 0;
    document.getElementById('scroll-to-bottom').classList.remove('visible');

    document.getElementById('chat-header').style.display = '';
    document.getElementById('chat-title').textContent = chat.display_name;
    document.getElementById('empty-pane').style.display = 'none';
    document.getElementById('search-results').classList.remove('has-results');
    document.getElementById('search-results').innerHTML = '';

    // reset in-chat search and date picker when switching chats
    document.getElementById('chat-search-input').value = '';
    document.getElementById('chat-search-clear').style.display = 'none';
    clearChatSearch();
    document.getElementById('date-picker-input').value = '';
    document.getElementById('date-go-btn').style.display = 'none';
    document.getElementById('date-clear-btn').style.display = 'none';

    const loadingEl = document.getElementById('chat-loading');
    loadingEl.style.display = 'flex';

    await loadMessages('older');

    loadingEl.style.display = 'none';
    scroll.scrollTop = scroll.scrollHeight;

    _startIndexPoll(chat);
  }

  // ---- load messages -------------------------------------------------------

  let loading = false;
  let _loadGen = 0;
  let noMoreOlder = false;
  let noMoreNewer = false;
  let pendingLoad = null;

  function showSpinner(position) {
    const el = document.createElement('div');
    el.className = 'load-spinner';
    el.id = 'load-spinner-' + position;
    el.textContent = '⟳';
    const scroll = document.getElementById('message-scroll');
    if (position === 'top') scroll.insertBefore(el, scroll.firstChild);
    else scroll.appendChild(el);
    return el;
  }

  function removeSpinner(position) {
    const el = document.getElementById('load-spinner-' + position);
    if (el) el.remove();
  }

  async function loadMessages(direction) {
    const gen = _loadGen;
    if (loading) return;
    if (direction === 'older' && noMoreOlder) return;
    if (direction === 'newer' && noMoreNewer) return;
    loading = true;

    const params = new URLSearchParams({
      chat_id: currentChat.id,
      chat_type: currentChat.type,
      limit: 50
    });

    if (direction === 'older' && msgList.length > 0) {
      params.set('before', msgList[0].timestamp_ms);
    } else if (direction === 'newer' && msgList.length > 0) {
      params.set('after', msgList[msgList.length - 1].timestamp_ms);
    }

    const spinner = showSpinner(direction === 'older' ? 'top' : 'bottom');

    try {
      const res = await fetch('/api/messages?' + params);
      const msgs = await res.json();
      removeSpinner(direction === 'older' ? 'top' : 'bottom');
      if (gen !== _loadGen) return;

      if (!msgs.length) {
        if (direction === 'older') noMoreOlder = true;
        else noMoreNewer = true;
        return;
      }

      const scroll = document.getElementById('message-scroll');

      if (direction === 'older') {
        // API returns DESC; reverse to get chronological order for prepending
        const ordered = msgs.slice().reverse();
        // The message currently at the top of our list is the boundary for date seps
        const firstExistingTs = msgList.length > 0 ? msgList[0].timestamp_ms : null;

        // Prepend into msgList
        for (let i = ordered.length - 1; i >= 0; i--) {
          msgList.unshift(ordered[i]);
        }

        const prevHeight = scroll.scrollHeight;
        const prevTop = scroll.scrollTop;

        // Insert into DOM oldest-first (each goes before the current firstChild)
        // so final order is oldest-at-top. For date sep: compare each msg with
        // the one that comes after it in the DOM (i.e. ordered[i+1] or firstExistingTs).
        for (let i = ordered.length - 1; i >= 0; i--) {
          const m = ordered[i];
          const nextTs = i < ordered.length - 1 ? ordered[i + 1].timestamp_ms : firstExistingTs;
          const nodes = renderBubbleWithSep(m, nextTs, 'before');
          nodes.forEach(node => scroll.insertBefore(node, scroll.firstChild));
        }

        domNodes += ordered.length;
        scroll.scrollTop = prevTop + (scroll.scrollHeight - prevHeight);
        pruneDom('top');
      } else {
        const prevLastTs = msgList.length > 0 ? msgList[msgList.length - 1].timestamp_ms : null;
        msgs.forEach(m => msgList.push(m));
        msgs.forEach((m, i) => {
          const prevTs = i === 0 ? prevLastTs : msgs[i - 1].timestamp_ms;
          renderBubbleWithSep(m, prevTs, 'after').forEach(node => scroll.appendChild(node));
        });
        domNodes += msgs.length;
        pruneDom('bottom');
      }

    } finally {
      if (gen === _loadGen) {
        loading = false;
        if (pendingLoad) {
          const dir = pendingLoad;
          pendingLoad = null;
          loadMessages(dir);
        }
      }
    }
  }

  function renderBubbleWithSep(msg, prevTs, direction) {
    const nodes = [];
    const needsSep = prevTs === null || dayKey(msg.timestamp_ms) !== dayKey(prevTs);
    if (needsSep && direction === 'after') nodes.push(makeDateSeparator(msg.timestamp_ms));
    nodes.push(renderBubble(msg));
    // 'before': separator goes after the bubble so insertBefore puts it above the next row;
    // label uses prevTs (= the chronologically newer neighbour, start of that day)
    if (needsSep && direction === 'before' && prevTs !== null) nodes.push(makeDateSeparator(prevTs));
    return nodes;
  }

  // ---- append / prepend with pruning ---------------------------------------

  function pruneDom(keepEnd) {
    const scroll = document.getElementById('message-scroll');
    let prunedRows = false;
    while (domNodes > MAX_DOM && scroll.children.length > 0) {
      const child = keepEnd === 'top' ? scroll.lastChild : scroll.firstChild;
      if (!child) break;
      scroll.removeChild(child);
      if (child.classList && child.classList.contains('msg-row')) {
        domNodes--;
        prunedRows = true;
      }
    }
    if (prunedRows) {
      if (keepEnd === 'top') noMoreOlder = false;
      else noMoreNewer = false;
    }
    // Re-sync msgList boundaries from surviving DOM rows
    const rows = scroll.querySelectorAll('.msg-row[data-ts]');
    if (rows.length === 0) { msgList = []; return; }
    const minTs = parseInt(rows[0].dataset.ts);
    const maxTs = parseInt(rows[rows.length - 1].dataset.ts);
    msgList = msgList.filter(m => m.timestamp_ms >= minTs && m.timestamp_ms <= maxTs);
  }

  // ---- scroll trigger -------------------------------------------------------

  function setupScrollTrigger() {
    const scroll = document.getElementById('message-scroll');
    const btn = document.getElementById('scroll-to-bottom');
    scroll.addEventListener('scroll', function () {
      hideMsgDetails();
      const atBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 100;
      btn.classList.toggle('visible', !atBottom);
      if (scroll.scrollTop < 100) {
        if (loading) { pendingLoad = 'older'; return; }
        loadMessages('older');
      } else if (atBottom) {
        if (loading) { pendingLoad = 'newer'; return; }
        loadMessages('newer');
      }
    });
    btn.addEventListener('click', async () => {
      if (noMoreNewer) {
        scroll.scrollTop = scroll.scrollHeight;
        return;
      }
      // Newer messages exist outside the current DOM window — reload from latest
      scroll.innerHTML = '';
      msgList = [];
      domNodes = 0;
      noMoreOlder = false;
      noMoreNewer = false;
      await loadMessages('older');
      scroll.scrollTop = scroll.scrollHeight;
    });
  }
  setupScrollTrigger();

  function renderBubble(msg) {
    const row = document.createElement('div');
    row.className = 'msg-row ' + (msg.from_me ? 'sent' : 'recv');
    row.dataset.ts = msg.timestamp_ms;

    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble';

    const senderEl = document.createElement('div');
    senderEl.className = 'msg-sender';
    senderEl.textContent = msg.sender || '';
    if (msg.sender && !msg.from_me && currentChat && currentChat.type === 'group') bubble.appendChild(senderEl);

    if (msg.quoted_text) {
      const quote = document.createElement('div');
      quote.className = 'msg-quote';
      const qSender = document.createElement('div');
      qSender.className = 'msg-quote-sender';
      const qs = msg.quoted_sender;
      qSender.textContent = qs || (currentChat && currentChat.type === 'contact' ? currentChat.display_name : '');
      const qText = document.createElement('div');
      qText.className = 'msg-quote-text';
      qText.textContent = msg.quoted_text;
      quote.appendChild(qSender);
      quote.appendChild(qText);
      if (msg.quoted_ts) {
        quote.style.cursor = 'pointer';
        quote.title = 'Jump to original message';
        quote.addEventListener('click', () => jumpToTimestamp(msg.quoted_ts));
      }
      bubble.appendChild(quote);
    }

    const meta = document.createElement('div');
    meta.className = 'msg-meta';
    if (msg.from_me) {
      const badge = document.createElement('span');
      badge.className = 'direction-badge sent';
      badge.textContent = 'You';
      meta.appendChild(badge);
    }
    const showInfo = msg.from_me ? !!msg.msg_id : !!msg.archive_path;
    if (showInfo) {
      const infoBtn = document.createElement('span');
      infoBtn.className = 'msg-info-btn';
      infoBtn.title = 'Message details';
      infoBtn.textContent = 'ⓘ';
      infoBtn.addEventListener('click', e => {
        e.stopPropagation();
        showMsgDetails(msg.from_me ? msg.msg_id : null, infoBtn, msg.archive_path || null);
      });
      meta.appendChild(infoBtn);
    }
    meta.appendChild(document.createTextNode(fmtTime(msg.timestamp_ms)));
    if (msg.media_type === 'text' || !msg.archive_path) {
      if (!msg.archive_path && msg.media_type !== 'text' && msg.media_type !== 'link') {
        const txt = document.createElement('div');
        txt.className = 'msg-text msg-unavailable';
        const icon = msg.media_type === 'image' ? '🖼️' : msg.media_type === 'video' ? '🎥' :
                     msg.media_type === 'audio' ? '🎵' : msg.media_type === 'sticker' ? '🩹' :
                     msg.media_type === 'gif' ? '🎞️' : '📄';
        txt.innerHTML = esc(icon) + ' ' + highlight(msg.text_body || 'Media not available', '');
        bubble.appendChild(txt);
      } else {
        const txt = document.createElement('div');
        txt.className = 'msg-text';
        txt.innerHTML = highlight(msg.text_body, '');
        bubble.appendChild(txt);
      }
    } else {
      const mediaEl = renderMedia(msg);
      bubble.appendChild(mediaEl);
      if (msg.text_body) {
        const cap = document.createElement('div');
        cap.className = 'msg-caption';
        cap.innerHTML = highlight(msg.text_body, '');
        bubble.appendChild(cap);
      }
    }

    if (msg.reactions) {
      const rxEl = document.createElement('div');
      rxEl.className = 'msg-reactions';
      const seen = {};
      msg.reactions.split(',').forEach(e => {
        seen[e] = (seen[e] || 0) + 1;
      });
      Object.entries(seen).forEach(([emoji, count]) => {
        const badge = document.createElement('span');
        badge.className = 'rx-badge';
        badge.textContent = count > 1 ? `${emoji} ${count}` : emoji;
        rxEl.appendChild(badge);
      });
      bubble.appendChild(rxEl);
    }

    bubble.appendChild(meta);

    row.appendChild(bubble);
    return row;
  }

  function renderMedia(msg) {
    const wrap = document.createElement('div');
    wrap.className = 'msg-media';
    const path = msg.archive_path;
    const src = '/media/' + path;
    const mt = msg.media_type;

    if (mt === 'image' || mt === 'gif' || mt === 'sticker') {
      const img = document.createElement('img');
      img.src = src;
      img.loading = 'lazy';
      img.alt = msg.media_name || 'image';
      img.addEventListener('click', () => showLightbox(src, mt, msg));
      wrap.appendChild(img);
    } else if (mt === 'video') {
      const vid = document.createElement('video');
      vid.controls = true;
      vid.preload = 'none';
      vid.src = src;
      wrap.appendChild(vid);
      // extract first frame as poster so the bubble isn't blank before playback
      const probe = document.createElement('video');
      probe.src = src;
      probe.muted = true;
      probe.preload = 'metadata';
      probe.addEventListener('loadeddata', () => {
        const canvas = document.createElement('canvas');
        canvas.width = probe.videoWidth;
        canvas.height = probe.videoHeight;
        canvas.getContext('2d').drawImage(probe, 0, 0);
        vid.poster = canvas.toDataURL('image/jpeg', 0.8);
        probe.src = '';
      }, { once: true });
      probe.addEventListener('error', () => { probe.src = ''; }, { once: true });
    } else if (mt === 'audio') {
      const aud = document.createElement('audio');
      aud.controls = true;
      aud.src = src;
      wrap.appendChild(aud);
    } else {
      const a = document.createElement('a');
      a.href = src;
      a.className = 'doc-link';
      a.download = msg.media_name || 'file';
      a.innerHTML = `<span class="doc-icon">📄</span><span>${esc(msg.media_name || 'Download')}</span>`;
      wrap.appendChild(a);
    }
    return wrap;
  }

  let lightboxItems = [];
  let lightboxIndex = 0;

  function _closeLb() {
    const lb = document.getElementById('img-lightbox');
    if (!lb) return;
    lb.classList.add('is-closing');
    lb.addEventListener('animationend', () => lb.remove(), { once: true });
    setTimeout(() => lb.isConnected && lb.remove(), 300);
  }

  function openLightboxAt(index) {
    document.getElementById('img-lightbox')?.remove();

    const msg = lightboxItems[index];
    const src = '/media/' + msg.archive_path;
    const mt = msg.media_type;

    const lb = document.createElement('div');
    lb.id = 'img-lightbox';
    lb.className = 'img-lightbox';

    const closeBtn = document.createElement('button');
    closeBtn.className = 'lb-close';
    closeBtn.innerHTML = '&#10005;';
    closeBtn.title = 'Close';
    closeBtn.addEventListener('click', () => _closeLb());
    lb.appendChild(closeBtn);
    const prevBtn = document.createElement('button');
    prevBtn.className = 'lb-arrow prev';
    prevBtn.innerHTML = '&#10094;';
    prevBtn.title = 'Previous';
    prevBtn.disabled = _nextLightboxIndex(index, -1) === -1;
    prevBtn.addEventListener('click', e => {
      e.stopPropagation();
      const i = _nextLightboxIndex(lightboxIndex, -1);
      if (i !== -1) { lightboxIndex = i; openLightboxAt(lightboxIndex); }
    });
    lb.appendChild(prevBtn);

    const nextBtn = document.createElement('button');
    nextBtn.className = 'lb-arrow next';
    nextBtn.innerHTML = '&#10095;';
    nextBtn.title = 'Next';
    nextBtn.disabled = _nextLightboxIndex(index, 1) === -1 && galleryAllLoaded;
    nextBtn.addEventListener('click', async e => {
      e.stopPropagation();
      let i = _nextLightboxIndex(lightboxIndex, 1);
      if (i !== -1) {
        lightboxIndex = i; openLightboxAt(lightboxIndex);
      } else if (!galleryAllLoaded) {
        const oldest = currentGalleryItems[currentGalleryItems.length - 1]?.timestamp_ms;
        await _loadGalleryPage(oldest);
        i = _nextLightboxIndex(lightboxIndex, 1);
        if (i !== -1) { lightboxIndex = i; openLightboxAt(lightboxIndex); }
      }
    });
    lb.appendChild(nextBtn);

    const tsEl = document.createElement('div');
    tsEl.className = 'lb-timestamp';
    tsEl.textContent = fmtTime(msg.timestamp_ms);
    lb.appendChild(tsEl);

    let media;
    if (mt === 'video') {
      media = document.createElement('video');
      media.controls = true;
      media.autoplay = true;
      media.src = src;
    } else {
      media = document.createElement('img');
      media.src = src;
    }
    lb.appendChild(media);

    const pathRow = document.createElement('div');
    pathRow.className = 'lb-path';
    const pathText = document.createElement('span');
    const sep = outputRoot.includes('\\') ? '\\' : '/';
    const fullPath = outputRoot.replace(/[/\\]+$/, '') + sep + msg.archive_path.replace(/\//g, sep);
    pathText.textContent = fullPath;
    const copyBtn = document.createElement('button');
    copyBtn.className = 'lb-copy-btn';
    copyBtn.title = 'Copy path';
    copyBtn.innerHTML = '&#128203;';
    copyBtn.addEventListener('click', e => {
      e.stopPropagation();
      navigator.clipboard.writeText(fullPath).then(() => {
        copyBtn.classList.add('copied');
        setTimeout(() => copyBtn.classList.remove('copied'), 1500);
      });
    });
    pathRow.appendChild(pathText);
    pathRow.appendChild(copyBtn);
    lb.appendChild(pathRow);

    lb.addEventListener('click', e => { if (e.target === lb) _closeLb(); });
    document.body.appendChild(lb);
  }

  function showLightbox(src, mt, msg) {
    document.getElementById('img-lightbox')?.remove();

    const lb = document.createElement('div');
    lb.id = 'img-lightbox';
    lb.className = 'img-lightbox';

    const closeBtn = document.createElement('button');
    closeBtn.className = 'lb-close';
    closeBtn.innerHTML = '&#10005;';
    closeBtn.title = 'Close';
    closeBtn.addEventListener('click', () => _closeLb());
    lb.appendChild(closeBtn);

    const tsEl = document.createElement('div');
    tsEl.className = 'lb-timestamp';
    tsEl.textContent = fmtTime(msg.timestamp_ms);
    lb.appendChild(tsEl);

    let media;
    if (mt === 'video') {
      media = document.createElement('video');
      media.controls = true;
      media.autoplay = true;
      media.src = src;
    } else {
      media = document.createElement('img');
      media.src = src;
    }
    lb.appendChild(media);

    const pathRow = document.createElement('div');
    pathRow.className = 'lb-path';
    const pathText = document.createElement('span');
    const sep = outputRoot.includes('\\') ? '\\' : '/';
    const fullPath = outputRoot.replace(/[/\\]+$/, '') + sep + msg.archive_path.replace(/\//g, sep);
    pathText.textContent = fullPath;
    const copyBtn = document.createElement('button');
    copyBtn.className = 'lb-copy-btn';
    copyBtn.title = 'Copy path';
    copyBtn.innerHTML = '&#128203;';
    copyBtn.addEventListener('click', e => {
      e.stopPropagation();
      navigator.clipboard.writeText(fullPath).then(() => {
        copyBtn.classList.add('copied');
        setTimeout(() => copyBtn.classList.remove('copied'), 1500);
      });
    });
    pathRow.appendChild(pathText);
    pathRow.appendChild(copyBtn);
    lb.appendChild(pathRow);

    lb.addEventListener('click', e => { if (e.target === lb) _closeLb(); });
    document.body.appendChild(lb);
  }

  // ---- media gallery -------------------------------------------------------

  function closeMediaGallery() {
    document.getElementById('media-gallery').classList.remove('open');
    if (galleryObserver) { galleryObserver.disconnect(); galleryObserver = null; }
    if (gallerySentinel) { gallerySentinel.remove(); gallerySentinel = null; }
  }

  function renderGalleryItem(msg) {
    const cell = document.createElement('div');
    cell.className = 'gallery-item';
    const src = '/media/' + msg.archive_path;
    const mt = msg.media_type;
    cell.dataset.type = mt;

    if (mt === 'image' || mt === 'gif' || mt === 'sticker') {
      const img = document.createElement('img');
      img.src = src;
      img.loading = 'lazy';
      img.alt = msg.media_name || '';
      const idx = lightboxItems.length;
      lightboxItems.push(msg);
      img.addEventListener('click', () => { lightboxIndex = idx; openLightboxAt(idx); });
      cell.appendChild(img);
    } else if (mt === 'video') {
      const img = document.createElement('img');
      img.alt = msg.media_name || '';
      img.className = 'gallery-video-thumb';
      const idx = lightboxItems.length;
      lightboxItems.push(msg);
      img.addEventListener('click', () => { lightboxIndex = idx; openLightboxAt(idx); });
      cell.appendChild(img);

      // extract first frame into img; fall back to a muted play icon on error
      const vid = document.createElement('video');
      vid.src = src;
      vid.muted = true;
      vid.preload = 'metadata';
      vid.addEventListener('loadeddata', () => {
        const canvas = document.createElement('canvas');
        canvas.width = vid.videoWidth;
        canvas.height = vid.videoHeight;
        canvas.getContext('2d').drawImage(vid, 0, 0);
        img.src = canvas.toDataURL('image/jpeg', 0.8);
        vid.src = '';
      }, { once: true });
      vid.addEventListener('error', () => {
        cell.querySelector('.gallery-video-thumb').classList.add('gallery-video-thumb--err');
        vid.src = '';
      }, { once: true });
    } else if (mt === 'audio') {
      const d = document.createElement('div');
      d.className = 'gallery-doc';
      d.innerHTML = '<span style="font-size:28px">🎵</span><span>' + esc(msg.media_name || 'audio') + '</span>';
      d.addEventListener('click', () => window.open(src, '_blank'));
      cell.appendChild(d);
    } else if (mt === 'link') {
      const urlMatch = msg.text_body.match(/https?:\/\/[^\s<>"]+/);
      const url = urlMatch ? urlMatch[0] : msg.text_body;
      cell.classList.add('gallery-link-card');
      const a = document.createElement('a');
      a.className = 'gallery-link-url';
      a.href = url;
      a.title = url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.textContent = url;
      const meta = document.createElement('div');
      meta.className = 'gallery-link-meta';
      meta.textContent = (msg.sender || 'You') + ' · ' + new Date(msg.timestamp_ms).toLocaleDateString(undefined, {year:'numeric', month:'short', day:'numeric'});
      cell.appendChild(a);
      cell.appendChild(meta);
      cell.addEventListener('click', e => { if (!e.target.closest('a')) window.open(url, '_blank'); });
    } else if (mt === 'document') {
      cell.classList.add('gallery-link-card');
      const name = document.createElement('div');
      name.className = 'gallery-link-url';
      name.textContent = '📄 ' + (msg.media_name || 'file');
      const meta = document.createElement('div');
      meta.className = 'gallery-link-meta';
      meta.textContent = (msg.sender || 'You') + ' · ' + new Date(msg.timestamp_ms).toLocaleDateString(undefined, {year:'numeric', month:'short', day:'numeric'});
      cell.appendChild(name);
      cell.appendChild(meta);
      cell.addEventListener('click', () => window.open(src, '_blank'));
    } else {
      const d = document.createElement('div');
      d.className = 'gallery-doc';
      d.innerHTML = '<span style="font-size:28px">📄</span><span>' + esc(msg.media_name || 'file') + '</span>';
      d.addEventListener('click', () => window.open(src, '_blank'));
      cell.appendChild(d);
    }

    const btn = document.createElement('button');
    btn.className = 'gallery-goto';
    btn.title = 'Go to message';
    btn.textContent = '→ in chat';
    btn.addEventListener('click', e => {
      e.stopPropagation();
      closeMediaGallery();
      jumpToTimestamp(msg.timestamp_ms);
    });
    cell.appendChild(btn);
    return cell;
  }

  async function openMediaGallery() {
    if (!currentChat) return;
    const grid = document.getElementById('media-gallery-grid');
    const stats = document.getElementById('media-gallery-stats');
    const switcher = document.getElementById('media-view-switcher');

    // disconnect any observer/sentinel from a previous open before clearing the grid
    if (galleryObserver) { galleryObserver.disconnect(); galleryObserver = null; }
    if (gallerySentinel) { gallerySentinel = null; }

    // reset to classic view each time
    archiveViewActive = false;
    switcher.style.display = '';
    document.getElementById('media-archive-tabs').style.display =
      currentChat.type === 'contact' ? '' : 'none';
    document.querySelectorAll('.media-view-btn').forEach((b, i) => b.classList.toggle('active', i === 0));
    grid.style.display = '';
    document.getElementById('media-archive-view').classList.remove('active');

    document.getElementById('media-gallery-title').textContent =
      currentChat.display_name ? `Media — ${currentChat.display_name}` : 'Media';

    grid.innerHTML = '<div style="color:var(--text-muted);padding:16px">Loading…</div>';
    stats.innerHTML = '';
    document.getElementById('media-gallery').classList.add('open');

    // reset pagination state
    currentGalleryItems = [];
    lightboxItems = [];
    activeTypes = new Set();
    galleryAllLoaded = false;
    galleryLoadingMore = false;
    galleryLastMonthKey = null;

    // fetch counts, documents, and links in parallel, then render media
    const typeOrder = ['image', 'video', 'audio', 'gif', 'sticker', 'document', 'link'];
    const qs = 'chat_id=' + encodeURIComponent(currentChat.id) + '&chat_type=' + encodeURIComponent(currentChat.type);
    const [counts, docItems, linkItems] = await Promise.all([
      fetch('/api/media/count?' + qs).then(r => r.json()),
      fetch('/api/media/documents?' + qs).then(r => r.json()),
      fetch('/api/media/links?' + qs).then(r => r.json()),
    ]);
    stats.innerHTML = '';
    if (counts.total) {
      stats.appendChild(_makeStatPill('total', counts.total, null, counts.total - counts.archived));
      for (const t of typeOrder) {
        const d = counts.by_type[t];
        if (!d) continue;
        stats.appendChild(_makeStatPill(t, d.count, t, d.missing));
      }
      // secondary types (links, documents) are hidden by default
      const hasSecondary = typeOrder.some(t => SECONDARY_TYPES.has(t) && counts.by_type[t]);
      if (hasSecondary) {
        typeOrder.filter(t => !SECONDARY_TYPES.has(t) && counts.by_type[t]).forEach(t => activeTypes.add(t));
        defaultActiveTypes = new Set(activeTypes);
      } else {
        defaultActiveTypes = new Set();
      }
      _syncStatPills();
    }

    grid.innerHTML = '';

    await _loadGalleryPage(null);

    if (!currentGalleryItems.length && !docItems.length && !linkItems.length) {
      grid.innerHTML = '<div style="color:var(--text-muted);padding:16px">No media in this chat.</div>';
      return;
    }

    // attach IntersectionObserver sentinel at bottom of grid
    gallerySentinel = document.createElement('div');
    gallerySentinel.style.height = '1px';
    grid.appendChild(gallerySentinel);
    galleryObserver = new IntersectionObserver(entries => {
      if (entries[0].isIntersecting) {
        const oldest = currentGalleryItems[currentGalleryItems.length - 1]?.timestamp_ms;
        _loadGalleryPage(oldest);
      }
    }, { root: grid, threshold: 0.1 });
    galleryObserver.observe(gallerySentinel);

    // append document and link cards (documents first); hidden when secondary types are filtered out by default
    let secondaryLastMonth = null;
    for (const msg of [...docItems, ...linkItems]) {
      const mk = monthKey(msg.timestamp_ms);
      if (mk !== secondaryLastMonth) {
        const hdr = document.createElement('div');
        hdr.className = 'gallery-month-header';
        hdr.textContent = fmtMonthHeader(msg.timestamp_ms);
        grid.insertBefore(hdr, gallerySentinel);
        secondaryLastMonth = mk;
      }
      const cell = renderGalleryItem(msg);
      if (activeTypes.size > 0 && !activeTypes.has(msg.media_type)) {
        cell.classList.add('type-hidden');
      }
      grid.insertBefore(cell, gallerySentinel);
    }
  }

  async function _loadGalleryPage(before) {
    if (galleryAllLoaded || galleryLoadingMore) return;
    galleryLoadingMore = true;
    const grid = document.getElementById('media-gallery-grid');
    try {
      const url = '/api/media?chat_id=' + encodeURIComponent(currentChat.id) +
                  '&chat_type=' + encodeURIComponent(currentChat.type) +
                  (before != null ? '&before=' + before : '');
      const items = await fetch(url).then(r => r.json());
      if (items.length < 100) galleryAllLoaded = true;
      currentGalleryItems.push(...items);

      const archived = items;
      for (const msg of archived) {
        const mk = monthKey(msg.timestamp_ms);
        if (mk !== galleryLastMonthKey) {
          const hdr = document.createElement('div');
          hdr.className = 'gallery-month-header';
          hdr.textContent = fmtMonthHeader(msg.timestamp_ms);
          if (gallerySentinel) grid.insertBefore(hdr, gallerySentinel);
          else grid.appendChild(hdr);
          galleryLastMonthKey = mk;
        }
        const cell = renderGalleryItem(msg);
        if (activeTypes.size > 0 && !activeTypes.has(msg.media_type)) {
          cell.classList.add('type-hidden');
        }
        if (gallerySentinel) grid.insertBefore(cell, gallerySentinel);
        else grid.appendChild(cell);
      }

      if (archiveViewActive) {
        _appendToArchiveView(items, activeArchiveDir());
      }
    } finally {
      galleryLoadingMore = false;
    }
  }

  document.getElementById('media-btn').addEventListener('click', openMediaGallery);
  document.getElementById('media-gallery-close').addEventListener('click', closeMediaGallery);
  document.addEventListener('keydown', async e => {
    if (document.getElementById('img-lightbox')) {
      if (e.key === 'Escape') { _closeLb(); return; }
      if (e.key === 'ArrowLeft') {
        const i = _nextLightboxIndex(lightboxIndex, -1);
        if (i !== -1) { lightboxIndex = i; openLightboxAt(lightboxIndex); }
        return;
      }
      if (e.key === 'ArrowRight') {
        let i = _nextLightboxIndex(lightboxIndex, 1);
        if (i !== -1) {
          lightboxIndex = i; openLightboxAt(lightboxIndex);
        } else if (!galleryAllLoaded) {
          const oldest = currentGalleryItems[currentGalleryItems.length - 1]?.timestamp_ms;
          await _loadGalleryPage(oldest);
          i = _nextLightboxIndex(lightboxIndex, 1);
          if (i !== -1) { lightboxIndex = i; openLightboxAt(lightboxIndex); }
        }
        return;
      }
    }
    if (e.key === 'Escape') {
      if (document.getElementById('chat-info-panel').classList.contains('open')) {
        closeChatInfo(); return;
      }
      closeMediaGallery();
    }
  });

  let archiveViewActive = false;
  let currentGalleryItems = [];
  let activeTypes = new Set();   // empty = show all; populated = show only those types
  let defaultActiveTypes = new Set();  // restored when deselecting a secondary type
  const SECONDARY_TYPES = new Set(['link', 'document']);  // hidden by default, toggle exclusive
  let galleryAllLoaded = false;
  let galleryLoadingMore = false;
  let gallerySentinel = null;
  let galleryObserver = null;
  let galleryLastMonthKey = null;

  function _nextLightboxIndex(from, dir) {
    let i = from + dir;
    while (i >= 0 && i < lightboxItems.length) {
      if (activeTypes.size === 0 || activeTypes.has(lightboxItems[i].media_type)) return i;
      i += dir;
    }
    return -1;
  }

  function _makeStatPill(label, count, type, missing) {
    const plurals = { audio: 'audio', sticker: 'stickers', document: 'documents', link: 'links' };
    const displayLabel = count === 1 ? label : (plurals[label] || label + 's');
    const span = document.createElement('span');
    span.className = 'gallery-stat';
    span.dataset.type = type || 'total';
    const ms = missing ? ` <span class="gallery-stat-missing">(${missing} missing)</span>` : '';
    span.innerHTML = `<strong>${count}</strong> ${displayLabel}${ms}`;
    span.addEventListener('click', () => _toggleTypeFilter(type));
    return span;
  }

  function _toggleTypeFilter(type) {
    if (type === null) {
      // Total: toggle between show-all and the default media-only state
      if (activeTypes.size === 0) {
        activeTypes = new Set(defaultActiveTypes);
      } else {
        activeTypes.clear();
      }
    } else if (SECONDARY_TYPES.has(type) && defaultActiveTypes.size > 0) {
      // Secondary type (link, document): toggle exclusive ↔ default state
      if (activeTypes.has(type)) {
        activeTypes = new Set(defaultActiveTypes);
      } else {
        activeTypes = new Set([type]);
      }
    } else if (activeTypes.size === 0) {
      // No filter active → first click is exclusive
      activeTypes.add(type);
    } else if (activeTypes.has(type)) {
      // Already selected → deselect; if nothing left, restore default
      activeTypes.delete(type);
      if (activeTypes.size === 0) activeTypes = new Set(defaultActiveTypes);
    } else {
      // Different type → add to selection
      activeTypes.add(type);
    }
    _applyTypeFilter();
    _syncStatPills();
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
        cell.classList.add('is-disappearing');
        cell.addEventListener('animationend', () => {
          cell.classList.remove('is-disappearing');
          cell.classList.add('type-hidden');
          _syncMonthHeaders();
        }, { once: true });
      } else if (!shouldHide && isHidden) {
        cell.classList.remove('type-hidden');
        cell.classList.remove('is-disappearing');
        requestAnimationFrame(() => {
          cell.classList.add('is-appearing');
          cell.addEventListener('animationend', () => {
            cell.classList.remove('is-appearing');
            _syncMonthHeaders();
          }, { once: true });
        });
      }
    });
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
      const visible = block.querySelectorAll('.gallery-item:not(.type-hidden)').length;
      block.querySelector('.archive-year-count').textContent = visible;
    });
  }

  function _syncStatPills() {
    const filtered = activeTypes.size > 0;
    document.querySelectorAll('#media-gallery-stats .gallery-stat').forEach(pill => {
      const t = pill.dataset.type;
      // secondary types (links, docs) are never archived — hide their pills in archive view
      if (archiveViewActive && t !== 'total' && SECONDARY_TYPES.has(t)) {
        pill.style.display = 'none';
        return;
      }
      pill.style.display = '';
      if (t === 'total') {
        pill.classList.toggle('active', !filtered);
        pill.classList.remove('dimmed');
      } else {
        pill.classList.toggle('active', filtered && activeTypes.has(t));
        pill.classList.toggle('dimmed', filtered && !activeTypes.has(t));
      }
    });
  }

  document.querySelectorAll('.media-view-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.media-view-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      archiveViewActive = btn.dataset.view === 'archive';
      document.getElementById('media-gallery-grid').style.display = archiveViewActive ? 'none' : '';
      document.getElementById('media-archive-view').classList.toggle('active', archiveViewActive);
      // if the active filter is secondary-types-only, those items don't exist in archive view — reset to default
      if (archiveViewActive && activeTypes.size > 0 && [...activeTypes].every(t => SECONDARY_TYPES.has(t))) {
        activeTypes = new Set(defaultActiveTypes);
      }
      _syncStatPills();
      if (archiveViewActive) {
        buildArchiveView(currentGalleryItems, activeArchiveDir());
        if (activeTypes.size > 0) { _applyTypeFilter(); } else { _syncMonthHeaders(); }
      }
    });
  });

  document.querySelectorAll('.archive-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.archive-tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      buildArchiveView(currentGalleryItems, btn.dataset.dir);
      if (activeTypes.size > 0) { _applyTypeFilter(); } else { _syncMonthHeaders(); }
    });
  });

  document.getElementById('archive-expand-all').addEventListener('click', () => {
    document.querySelectorAll('.archive-year').forEach(el => el.classList.add('open'));
  });
  document.getElementById('archive-collapse-all').addEventListener('click', () => {
    document.querySelectorAll('.archive-year').forEach(el => el.classList.remove('open'));
  });

  // ---- sidebar search -------------------------------------------------------

  let searchTimer = null;

  document.getElementById('search-input').addEventListener('input', function () {
    clearTimeout(searchTimer);
    const q = this.value.trim();
    if (!q) {
      clearSearchResults();
      return;
    }
    searchTimer = setTimeout(() => doSearch(q), 300);
  });

  async function doSearch(q) {
    // Contact name matches — client-side, instant
    const ql = q.toLowerCase();
    const contactMatches = allChats.filter(c =>
      c.display_name.toLowerCase().includes(ql) || c.id.toLowerCase().includes(ql)
    );

    // Show loading state while FTS fetch is in flight
    const container = document.getElementById('search-results');
    container.classList.add('has-results');
    container.innerHTML = '<div class="sr-section-label">Searching…</div>';

    // Full-text search across indexed chats
    const res = await fetch('/api/search?' + new URLSearchParams({ q }));
    const data = await res.json();

    renderSearchResults(q, contactMatches, data.results, data.indexed_count);
  }

  function clearSearchResults() {
    document.getElementById('search-results').classList.remove('has-results');
    document.getElementById('search-results').innerHTML = '';
  }

  function makeSectionLabel(text) {
    const el = document.createElement('div');
    el.className = 'sr-section-label';
    el.textContent = text;
    return el;
  }

  function makeContactResultItem(chat, q) {
    const el = document.createElement('div');
    el.className = 'search-result-item';
    el.innerHTML = `<div class="sr-chat">${highlight(chat.display_name, q)}</div><div class="sr-text">${esc(chat.id)}</div>`;
    el.addEventListener('click', () => {
      document.getElementById('search-input').value = '';
      clearSearchResults();
      resetFilterTo('all');
      const el2 = document.querySelector(`.chat-item[data-id="${chat.id}"][data-type="${chat.type}"]`);
      if (el2) selectChat(chat, el2);
    });
    return el;
  }

  function makeTextResultItem(r, q) {
    const el = document.createElement('div');
    el.className = 'search-result-item';
    const chatName = allChats.find(c => c.id === r.chat_id && c.type === r.chat_type)?.display_name || r.chat_id;
    const snippet = r.text_body || (r.media_name ? '[📎 ' + r.media_name + ']' : '');
    el.innerHTML = `
      <div class="sr-chat">${esc(chatName)}</div>
      <div class="sr-text">${highlight(snippet, q)}</div>
      <div class="sr-time">${fmtTime(r.timestamp_ms)}</div>
    `;
    el.addEventListener('click', async () => {
      document.getElementById('search-input').value = '';
      clearSearchResults();
      const chat = allChats.find(c => c.id === r.chat_id && c.type === r.chat_type);
      if (chat) {
        resetFilterTo('all');
        const el2 = document.querySelector(`.chat-item[data-id="${r.chat_id}"][data-type="${r.chat_type}"]`);
        if (el2) {
          await selectChat(chat, el2);
          const row = await jumpToTimestamp(r.timestamp_ms);
          if (row) {
            const bubble = row.querySelector('.msg-bubble');
            if (bubble) {
              bubble.classList.add('search-jump-highlight');
              bubble.addEventListener('animationend', () => bubble.classList.remove('search-jump-highlight'), { once: true });
            }
          }
        }
      }
    });
    return el;
  }

  function renderSearchResults(q, contactMatches, textResults, indexedCount) {
    const container = document.getElementById('search-results');
    if (!contactMatches.length && !textResults.length) {
      clearSearchResults();
      return;
    }
    container.classList.add('has-results');
    container.innerHTML = '';

    if (contactMatches.length) {
      container.appendChild(makeSectionLabel('Contacts'));
      contactMatches.forEach(chat => container.appendChild(makeContactResultItem(chat, q)));
    }

    if (textResults.length) {
      container.appendChild(makeSectionLabel('Messages'));
      textResults.slice(0, 50).forEach(r => container.appendChild(makeTextResultItem(r, q)));
    }

    if (indexedCount != null && indexedCount < allChats.length) {
      const notice = document.createElement('div');
      notice.className = 'search-index-notice';
      notice.textContent = `Searched ${indexedCount} of ${allChats.length} chats. Open more chats to index them.`;
      container.appendChild(notice);
    }
  }

  // ---- in-chat search -------------------------------------------------------

  let chatSearchResults = [];
  let chatSearchIdx = -1;
  let chatSearchTimer = null;

  document.getElementById('chat-search-input').addEventListener('input', function () {
    document.getElementById('chat-search-clear').style.display = this.value ? 'block' : 'none';
    clearTimeout(chatSearchTimer);
    const q = this.value.trim();
    if (!q) {
      clearChatSearch();
      return;
    }
    chatSearchTimer = setTimeout(() => doChatSearch(q), 300);
  });

  document.getElementById('chat-search-clear').addEventListener('click', () => {
    const inp = document.getElementById('chat-search-input');
    inp.value = '';
    document.getElementById('chat-search-clear').style.display = 'none';
    clearChatSearch();
    inp.focus();
  });

  document.getElementById('chat-search-input').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      if (e.shiftKey) navigateChatSearch(-1);
      else navigateChatSearch(1);
    } else if (e.key === 'Escape') {
      clearChatSearch();
      this.value = '';
    }
  });

  document.getElementById('search-prev').addEventListener('click', () => navigateChatSearch(-1));
  document.getElementById('search-next').addEventListener('click', () => navigateChatSearch(1));

  async function doChatSearch(q) {
    if (!currentChat) return;
    const nav = document.getElementById('chat-search-nav');
    const count = document.getElementById('chat-search-count');
    const statusRes = await fetch('/api/chat-index-status?chat_id=' +
      encodeURIComponent(currentChat.id) + '&chat_type=' + encodeURIComponent(currentChat.type));
    const statusData = await statusRes.json();
    if (statusData.status !== 'done') {
      nav.style.display = 'flex';
      count.textContent = 'Search not ready yet…';
      document.getElementById('search-prev').disabled = true;
      document.getElementById('search-next').disabled = true;
      return;
    }
    nav.style.display = 'flex';
    count.textContent = 'Searching…';
    document.getElementById('search-prev').disabled = true;
    document.getElementById('search-next').disabled = true;
    const params = new URLSearchParams({ q, chat_id: currentChat.id, chat_type: currentChat.type });
    const res = await fetch('/api/search?' + params);
    const data = await res.json();
    chatSearchResults = data.results;
    chatSearchIdx = chatSearchResults.length > 0 ? 0 : -1;
    updateChatSearchNav();
    if (chatSearchIdx >= 0) jumpToChatSearchResult(chatSearchIdx);
  }

  function clearChatSearch() {
    chatSearchResults = [];
    chatSearchIdx = -1;
    document.getElementById('chat-search-nav').style.display = 'none';
    document.getElementById('chat-search-count').textContent = '';
    document.querySelectorAll('.search-highlight').forEach(el => el.classList.remove('search-highlight'));
  }

  function navigateChatSearch(delta) {
    if (!chatSearchResults.length) return;
    chatSearchIdx = (chatSearchIdx + delta + chatSearchResults.length) % chatSearchResults.length;
    updateChatSearchNav();
    jumpToChatSearchResult(chatSearchIdx);
  }

  function updateChatSearchNav() {
    const nav = document.getElementById('chat-search-nav');
    const count = document.getElementById('chat-search-count');
    nav.style.display = 'flex';
    if (!chatSearchResults.length) {
      count.textContent = 'No results';
      document.getElementById('search-prev').disabled = true;
      document.getElementById('search-next').disabled = true;
      return;
    }
    count.textContent = `${chatSearchIdx + 1}/${chatSearchResults.length}`;
    document.getElementById('search-prev').disabled = false;
    document.getElementById('search-next').disabled = false;
  }

  async function jumpToChatSearchResult(idx) {
    const result = chatSearchResults[idx];
    if (!result) return;
    document.querySelectorAll('.search-highlight').forEach(el => el.classList.remove('search-highlight'));
    const existing = document.querySelector(`.msg-row[data-ts="${result.timestamp_ms}"]`);
    if (existing) {
      highlightMessageRow(existing);
      return;
    }
    await jumpToTimestamp(result.timestamp_ms);
    const loaded = document.querySelector(`.msg-row[data-ts="${result.timestamp_ms}"]`);
    if (loaded) highlightMessageRow(loaded);
  }

  function highlightMessageRow(row) {
    const bubble = row.querySelector('.msg-bubble');
    if (bubble) bubble.classList.add('search-highlight');
    row.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  // ---- date picker ----------------------------------------------------------

  document.getElementById('date-picker-input').max = new Date().toISOString().slice(0, 10);

  document.getElementById('date-picker-input').addEventListener('change', function () {
    const hasVal = !!this.value;
    document.getElementById('date-go-btn').style.display = hasVal ? 'inline-block' : 'none';
    document.getElementById('date-clear-btn').style.display = hasVal ? 'inline-block' : 'none';
  });

  document.getElementById('date-go-btn').addEventListener('click', async () => {
    const val = document.getElementById('date-picker-input').value;
    if (!val || !currentChat) return;
    const btn = document.getElementById('date-go-btn');
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    try {
      await jumpToTimestamp(new Date(val).getTime());
    } finally {
      btn.disabled = false;
      btn.removeAttribute('aria-busy');
    }
  });

  document.getElementById('date-clear-btn').addEventListener('click', () => {
    document.getElementById('date-picker-input').value = '';
    document.getElementById('date-go-btn').style.display = 'none';
    document.getElementById('date-clear-btn').style.display = 'none';
  });

  async function jumpToTimestamp(ts) {
    // If already in DOM, just scroll to it
    const existing = document.querySelector(`.msg-row[data-ts="${ts}"]`);
    if (existing) {
      existing.scrollIntoView({ behavior: 'smooth', block: 'center' });
      return existing;
    }

    // Save current scroll position so we can restore it after patching
    const scroll = document.getElementById('message-scroll');
    const prevScrollTop = scroll.scrollTop;

    // Load context window around the target
    const params = new URLSearchParams({
      chat_id: currentChat.id,
      chat_type: currentChat.type,
      ts,
      limit: 50
    });
    const res = await fetch('/api/messages/at?' + params);
    const msgs = await res.json();
    if (!msgs.length) return null;

    // Patch new messages into existing DOM without wiping anything.
    // Find the insertion point: last existing row whose timestamp is older than
    // the oldest message in the API response.
    const oldestNewTs = msgs[0].timestamp_ms;
    let insertBeforeEl = null;
    for (const row of scroll.querySelectorAll('.msg-row[data-ts]')) {
      if (parseInt(row.getAttribute('data-ts')) < oldestNewTs) {
        insertBeforeEl = row;
        break;
      }
    }

    // Find the message closest to ts (the actual jump target)
    const target = msgs.reduce((best, m) =>
      Math.abs(m.timestamp_ms - ts) < Math.abs(best.timestamp_ms - ts) ? m : best
    );

    // Insert oldest-first, after any existing rows older than the new content
    for (let i = 0; i < msgs.length; i++) {
      const m = msgs[i];
      const prevTs = i === 0 ? null : msgs[i - 1].timestamp_ms;
      const nodes = renderBubbleWithSep(m, prevTs, 'after');
      nodes.forEach(node => {
        if (insertBeforeEl) scroll.insertBefore(node, insertBeforeEl);
        else scroll.appendChild(node);
      });
    }

    // Restore scroll position so the patch is invisible
    scroll.scrollTop = prevScrollTop;

    const targetEl = document.querySelector(`.msg-row[data-ts="${target.timestamp_ms}"]`);
    if (targetEl) targetEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
    return targetEl || null;
  }

  // ---- toolbar toggle -------------------------------------------------------

  document.getElementById('toolbar-toggle').addEventListener('click', function () {
    const exp = document.getElementById('toolbar-expanded');
    const open = exp.classList.toggle('open');
    if (open) document.getElementById('chat-search-input').focus();
    else {
      clearChatSearch();
      document.getElementById('chat-search-input').value = '';
    }
  });

  // ---- chat info panel -------------------------------------------------------

  function _formatBytes(bytes) {
    if (bytes >= 1073741824) return (bytes / 1073741824).toFixed(1) + ' GB';
    if (bytes >= 1048576)    return (bytes / 1048576).toFixed(1) + ' MB';
    if (bytes >= 1024)       return (bytes / 1024).toFixed(1) + ' KB';
    return bytes + ' B';
  }

  function _infoRow(label, valueNode) {
    const row = document.createElement('div');
    row.className = 'chat-info-row';
    const lbl = document.createElement('span');
    lbl.className = 'chat-info-label';
    lbl.textContent = label;
    row.appendChild(lbl);
    const val = document.createElement('span');
    val.className = 'chat-info-value';
    if (typeof valueNode === 'string') {
      val.textContent = valueNode;
    } else {
      val.appendChild(valueNode);
    }
    row.appendChild(val);
    return { row, val };
  }

  function _spinner() {
    const s = document.createElement('span');
    s.className = 'chat-info-spinner';
    return s;
  }

  function closeChatInfo() {
    document.getElementById('chat-info-panel').classList.remove('open');
  }

  async function openChatInfo() {
    if (!currentChat) return;
    const panel = document.getElementById('chat-info-panel');
    const title = document.getElementById('chat-info-title');
    const body  = document.getElementById('chat-info-body');

    title.textContent = currentChat.display_name || '';
    body.innerHTML = '';

    // Show loading skeleton
    const loadingRow = document.createElement('div');
    loadingRow.className = 'chat-info-row';
    loadingRow.appendChild(_spinner());
    body.appendChild(loadingRow);
    panel.classList.add('open');

    const params = new URLSearchParams({ chat_id: currentChat.id, chat_type: currentChat.type });
    const chatAtOpen = currentChat;

    // Kick off both requests in parallel
    let info, sizeData;
    try {
      const [infoResp, sizeResp] = await Promise.all([
        fetch('/api/chat-info?' + params),
        fetch('/api/chat-info/media-size?' + params),
      ]);
      info = await infoResp.json();
      sizeData = await sizeResp.json();
    } catch (_) {
      if (currentChat === chatAtOpen && panel.classList.contains('open')) {
        body.innerHTML = '<div class="chat-info-row" style="color:var(--text-muted)">Could not load chat info.</div>';
      }
      return;
    }

    // Panel may have been closed or chat switched while awaiting
    if (currentChat !== chatAtOpen || !panel.classList.contains('open')) return;

    body.innerHTML = '';

    const fmtTs = ts => ts ? new Date(ts).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : '—';
    const fmtNum = n => (n === null || n === undefined || n < 0) ? '—' : n.toLocaleString();

    if (currentChat.type === 'contact' && info.number) {
      body.appendChild(_infoRow('Phone', '+' + info.number).row);
    }

    if (currentChat.type === 'group' && info.created_ts) {
      body.appendChild(_infoRow('Group created', fmtTs(info.created_ts)).row);
      if (info.creator_number) {
        const match = (info.members || []).find(m => m.number === info.creator_number);
        const creatorName = (match && match.name) ? match.name : '+' + info.creator_number;
        body.appendChild(_infoRow('Created by', creatorName).row);
      }
    }

    body.appendChild(_infoRow('Conversation since', fmtTs(info.first_ts)).row);

    if (info.first_ts && info.last_ts && info.last_ts !== info.first_ts) {
      const days = Math.round((info.last_ts - info.first_ts) / 86400000);
      const years = Math.floor(days / 365);
      const months = Math.floor((days % 365) / 30);
      let span = '';
      if (years > 0) span += years + (years === 1 ? ' year' : ' years');
      if (months > 0) span += (span ? ', ' : '') + months + (months === 1 ? ' month' : ' months');
      if (!span) span = days + (days === 1 ? ' day' : ' days');
      body.appendChild(_infoRow('Duration', span).row);
    }

    if (currentChat.type === 'contact') {
      const sentReceived = document.createElement('span');
      sentReceived.textContent = fmtNum(info.sent) + ' sent · ' + fmtNum(info.received) + ' received';
      body.appendChild(_infoRow('Messages', sentReceived).row);
    } else {
      body.appendChild(_infoRow('Total messages', fmtNum(info.total)).row);
    }

    // Gallery link row
    const galleryLink = document.createElement('button');
    galleryLink.className = 'chat-info-link';
    galleryLink.textContent = 'Open Media Gallery →';
    galleryLink.addEventListener('click', () => { closeChatInfo(); openMediaGallery(); });
    body.appendChild(_infoRow('Media', galleryLink).row);

    // Media size row (already resolved since we awaited both)
    body.appendChild(_infoRow('Media size', _formatBytes(sizeData.bytes || 0)).row);

    // Group members
    if (currentChat.type === 'group' && info.members && info.members.length > 0) {
      const SHOW = 10;
      const membersWrap = document.createElement('div');
      membersWrap.className = 'chat-info-members';
      const renderMember = m => {
        const el = document.createElement('div');
        el.className = 'chat-info-member';
        const numSuffix = m.number && ('+' + m.number) !== m.name ? ' (+' + m.number + ')' : '';
        el.textContent = (m.name || m.number || '?') + numSuffix;
        return el;
      };
      info.members.slice(0, SHOW).forEach(m => membersWrap.appendChild(renderMember(m)));
      if (info.members.length > SHOW) {
        const more = document.createElement('button');
        more.className = 'chat-info-link';
        more.style.marginTop = '4px';
        const remaining = info.members.length - SHOW;
        more.textContent = '+ ' + remaining + ' more…';
        more.addEventListener('click', () => {
          info.members.slice(SHOW).forEach(m => membersWrap.insertBefore(renderMember(m), more));
          more.remove();
        });
        membersWrap.appendChild(more);
      }
      body.appendChild(_infoRow('Members (' + info.members.length + ')', membersWrap).row);
    }

    // Top senders
    if (currentChat.type === 'group' && info.top_senders && info.top_senders.length > 0) {
      const sendersWrap = document.createElement('div');
      info.top_senders.forEach(s => {
        const el = document.createElement('div');
        el.className = 'chat-info-member';
        el.textContent = (s.name || '?') + ': ' + s.count.toLocaleString();
        sendersWrap.appendChild(el);
      });
      body.appendChild(_infoRow('Top senders', sendersWrap).row);
    }
  }

  document.getElementById('chat-info-btn').addEventListener('click', openChatInfo);
  document.getElementById('chat-info-close').addEventListener('click', closeChatInfo);
  document.getElementById('chat-info-panel').addEventListener('click', e => {
    if (e.target === document.getElementById('chat-info-panel')) closeChatInfo();
  });

  // ---- message details popup ------------------------------------------------

  const msgDetailsPopup = document.getElementById('msg-details-popup');

  function hideMsgDetails() {
    msgDetailsPopup.classList.remove('visible');
  }

  document.addEventListener('click', e => {
    if (!msgDetailsPopup.contains(e.target)) hideMsgDetails();
  });

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') hideMsgDetails();
  });

  function fmtReceipt(ts) {
    if (!ts) return '—';
    return new Date(ts).toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
    });
  }

  async function showMsgDetails(msgId, anchorEl, archivePath) {
    let html = '';

    if (msgId !== null) {
      msgDetailsPopup.innerHTML = '<span style="color:var(--text-muted)">Loading…</span>';
      positionPopup(anchorEl);
      msgDetailsPopup.classList.add('visible');

      let data;
      try {
        const res = await fetch('/api/message_receipts/' + msgId);
        data = await res.json();
      } catch (_) {
        data = { available: false };
      }

      if (!data.available || !(data.members || []).length) {
        html = '<span class="msg-details-na">No receipt data available.</span>';
      } else if (data.members.length === 1) {
        html =
          `<div class="msg-details-row"><span class="msg-details-label">Delivered</span><span class="msg-details-value">${fmtReceipt(data.delivered_ts)}</span></div>` +
          `<div class="msg-details-row"><span class="msg-details-label">Read</span><span class="msg-details-value">${fmtReceipt(data.read_ts)}</span></div>`;
      } else {
        let rows = '';
        for (const m of data.members) {
          rows += `<tr><td>${esc(m.name || m.jid || '?')}</td><td>${fmtReceipt(m.delivered_ts)}</td><td>${fmtReceipt(m.read_ts)}</td></tr>`;
        }
        html = `<table class="msg-details-table"><thead><tr><th>Member</th><th>Delivered</th><th>Read</th></tr></thead><tbody>${rows}</tbody></table>`;
      }
    }

    if (archivePath) {
      html += `<div class="msg-details-copy-row"><button class="msg-details-copy-btn" data-path="${esc(archivePath)}">Copy path</button></div>`;
    }

    msgDetailsPopup.innerHTML = html;
    msgDetailsPopup.querySelector('.msg-details-copy-btn')?.addEventListener('click', e => {
      e.stopPropagation();
      const btn = e.currentTarget;
      const sep = outputRoot.includes('\\') ? '\\' : '/';
      const fullPath = outputRoot.replace(/[/\\]+$/, '') + sep + btn.dataset.path.replace(/\//g, sep);
      navigator.clipboard.writeText(fullPath).then(() => {
        btn.textContent = 'Copied!';
        setTimeout(() => { btn.textContent = 'Copy path'; }, 1500);
      });
    });

    positionPopup(anchorEl);
    msgDetailsPopup.classList.add('visible');
  }

  function positionPopup(anchorEl) {
    const rect = anchorEl.getBoundingClientRect();
    const pane = document.getElementById('chat-pane').getBoundingClientRect();
    const popupW = msgDetailsPopup.offsetWidth || 200;
    let left = rect.right - popupW;
    if (left < pane.left + 4) left = pane.left + 4;
    if (left + popupW > pane.right - 4) left = pane.right - popupW - 4;
    let top = rect.bottom + 4;
    if (top + msgDetailsPopup.offsetHeight > window.innerHeight - 8) {
      top = rect.top - msgDetailsPopup.offsetHeight - 4;
    }
    msgDetailsPopup.style.left = left + 'px';
    msgDetailsPopup.style.top = top + 'px';
  }

  // ---- init ----------------------------------------------------------------

  let _bulkIndexPollGen = 0;

  function _startBulkIndexPoll() {
    const gen = ++_bulkIndexPollGen;
    async function poll() {
      if (gen !== _bulkIndexPollGen) return;
      try {
        const r = await fetch('/api/index/progress');
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
    document.getElementById('settings-modal').classList.add('open');
    try {
      const r = await fetch('/api/index/progress');
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
      fetch('/api/preferences', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: btn.dataset.pref, value: btn.dataset.value })
      });
    });
  });

  document.getElementById('index-all-btn').addEventListener('click', async () => {
    let sizeBytes = 0;
    try {
      const r = await fetch('/api/index/source-size');
      const d = await r.json();
      sizeBytes = d.bytes || 0;
    } catch (_) {}

    if (sizeBytes > 150 * 1024 * 1024) {
      const mb = Math.round(sizeBytes / 1024 / 1024);
      if (!confirm('The database is large (' + mb + ' MB). Indexing may take several minutes. Continue?')) return;
    }
    if (!confirm('Index all chats for search? This will run in the background while you use the viewer.')) return;

    const res = await fetch('/api/index/all', { method: 'POST' });
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
    await fetch('/api/index/clear', { method: 'POST' });
    const orig = btn.textContent;
    btn.textContent = 'Cleared.';
    setTimeout(() => { btn.textContent = orig; }, 2000);
    document.getElementById('index-all-progress').style.display = 'none';
    document.getElementById('index-all-btn').disabled = false;
  });

  loadPrefs().then(() => loadChats());
})();
