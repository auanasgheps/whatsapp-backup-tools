(function () {
  'use strict';

  const outputRoot = window.OUTPUT_ROOT;
  let allChats = [];
  let currentChat = null;
  let msgList = [];
  let suppressScroll = false;
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
    const landMap = { small: '280px', medium: '320px', large: '360px' };
    const portMap = { small: '230px', medium: '260px', large: '295px' };
    const landSize = landMap[prefs.font_size] || '320px';
    document.documentElement.style.setProperty('--media-w-land', landSize);
    document.documentElement.style.setProperty('--media-w-port', portMap[prefs.font_size] || '260px');
    document.documentElement.style.setProperty('--media-grid-size', landSize);
    document.documentElement.dataset.fontSize = prefs.font_size || 'medium';
    document.querySelectorAll('.pref-btn').forEach(btn => {
      btn.classList.toggle('active', prefs[btn.dataset.pref] === btn.dataset.value);
    });
  }
  const MAX_WINDOW = 150;

  // ---- util ----------------------------------------------------------------

  const AVATAR_PALETTE = [
    '#00a884',
    '#0284c7',
    '#2563eb',
    '#7c3aed',
    '#c026d3',
    '#db2777',
    '#ea580c',
    '#16a34a',
    '#0d9488',
    '#4f46e5',
  ];

  function getAvatarColor(key) {
    if (!key) return AVATAR_PALETTE[0];
    const str = String(key);
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
      hash = (hash * 31 + str.charCodeAt(i)) >>> 0;
    }
    return AVATAR_PALETTE[hash % AVATAR_PALETTE.length];
  }

  function getInitials(name) {
    if (!name) return '?';
    const cleaned = String(name).trim();
    if (!cleaned) return '?';
    const digitsOnly = cleaned.replace(/[\s\-\(\)\+]/g, '');
    if (/^\d+$/.test(digitsOnly)) {
      return '👤';
    }
    const parts = cleaned.split(/\s+/).filter(Boolean);
    if (parts.length >= 2) {
      const first = Array.from(parts[0])[0] || '';
      const last = Array.from(parts[parts.length - 1])[0] || '';
      return (first + last).toUpperCase();
    }
    return Array.from(cleaned).slice(0, 2).join('').toUpperCase();
  }

  function formatPhoneNumber(num) {
    if (!num) return '';
    const clean = String(num).trim();
    if (!clean) return '';
    if (clean === '0') return 'WhatsApp';
    const digits = clean.replace(/\D/g, '');
    if (!digits) return clean;
    if (digits.length === 11 && digits.startsWith('1')) {
      return `+1 (${digits.slice(1, 4)}) ${digits.slice(4, 7)}-${digits.slice(7)}`;
    }
    if (digits.length === 10) {
      return `+1 (${digits.slice(0, 3)}) ${digits.slice(3, 6)}-${digits.slice(6)}`;
    }
    return clean.startsWith('+') ? clean : `+${clean}`;
  }

  function fmtTime(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    return d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  function _formatTimer(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    const mins = Math.floor(s / 60);
    const secs = s % 60;
    return mins + ':' + (secs < 10 ? '0' : '') + secs;
  }

  function fmtDuration(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    const m = Math.floor(s / 60);
    const remSec = s % 60;
    if (m === 0) {
      return `${remSec}s`;
    }
    const h = Math.floor(m / 60);
    const remMin = m % 60;
    if (h === 0) {
      return `${remMin}m ${remSec}s`;
    }
    return `${h}h ${remMin}m ${remSec}s`;
  }

  function fmtDurationLong(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    const m = Math.floor(s / 60);
    const remSec = s % 60;
    if (m === 0) {
      return `${remSec} ${remSec === 1 ? 'second' : 'seconds'}`;
    }
    const h = Math.floor(m / 60);
    const remMin = m % 60;
    if (h === 0) {
      const secPart = remSec > 0 ? `, ${remSec} ${remSec === 1 ? 'second' : 'seconds'}` : '';
      return `${remMin} ${remMin === 1 ? 'minute' : 'minutes'}${secPart}`;
    }
    const minPart = remMin > 0 ? `, ${remMin} ${remMin === 1 ? 'minute' : 'minutes'}` : '';
    const secPart = remSec > 0 ? `, ${remSec} ${remSec === 1 ? 'second' : 'seconds'}` : '';
    return `${h} ${h === 1 ? 'hour' : 'hours'}${minPart}${secPart}`;
  }

  function fmtTimeGap(diffMs, isPrior) {
    const diffSec = Math.floor(diffMs / 1000);
    const diffMin = Math.floor(diffSec / 60);
    const diffHrs = Math.floor(diffMin / 60);
    const diffDays = Math.floor(diffHrs / 24);

    let gapStr = '';
    if (diffDays >= 365) {
      const years = Math.floor(diffDays / 365);
      gapStr = `${years} ${years === 1 ? 'year' : 'years'}`;
    } else if (diffDays >= 30) {
      const months = Math.floor(diffDays / 30);
      gapStr = `${months} ${months === 1 ? 'month' : 'months'}`;
    } else if (diffDays >= 1) {
      gapStr = `${diffDays} ${diffDays === 1 ? 'day' : 'days'}`;
    } else if (diffHrs >= 1) {
      gapStr = `${diffHrs} ${diffHrs === 1 ? 'hour' : 'hours'}`;
    }
    if (!gapStr) return '';
    return ` • Nearest message: ${gapStr} ${isPrior ? 'prior' : 'later'}`;
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

  function _populateArchiveYear(block) {
    if (!block || block._populated) return;
    const grid = block.querySelector('.archive-year-grid');
    const observer = getArchiveMediaObserver();
    const messages = block._messages || [];
    let lastMonth = null;
    for (const msg of messages) {
      const mk = monthKey(msg.timestamp_ms);
      if (mk !== lastMonth) {
        const mhdr = document.createElement('div');
        mhdr.className = 'gallery-month-header';
        mhdr.dataset.month = mk;
        mhdr.textContent = new Date(msg.timestamp_ms).toLocaleString('default', { month: 'long' });
        grid.appendChild(mhdr);
        lastMonth = mk;
      }
      const cell = renderGalleryItem(msg);
      if (activeTypes.size > 0 && !activeTypes.has(cell.dataset.type || msg.media_type)) {
        cell.classList.add('type-hidden');
      }
      grid.appendChild(cell);
      if (cell.dataset.src && observer) {
        observer.observe(cell);
      }
    }
    block._populated = true;
  }

  function buildArchiveView(items, direction) {
    const tree = document.getElementById('media-archive-tree');
    tree.innerHTML = '';
    if (archiveMediaObserver) {
      archiveMediaObserver.disconnect();
      archiveMediaObserver = null;
    }

    const filtered = items.filter(m => m.archive_path &&
      (direction === null || archiveSegments(m.archive_path).direction === direction));

    if (!filtered.length) {
      tree.innerHTML = galleryAllLoaded
        ? '<div class="archive-empty-notice" style="color:var(--text-muted);padding:16px">No media in this chat.</div>'
        : '<div class="archive-empty-notice" style="color:var(--text-muted);padding:16px">Loading…</div>';
      return;
    }

    const byYear = {};
    for (const msg of filtered) {
      const { year } = archiveSegments(msg.archive_path);
      (byYear[year] = byYear[year] || []).push(msg);
    }

    const sortedYears = Object.keys(byYear).sort((a, b) => b - a);
    const latestYear = sortedYears[0];
    for (const year of sortedYears) {
      const isLatest = (year === latestYear);
      const block = _createArchiveYearBlock(year, isLatest);
      block._messages = byYear[year];
      const countEl = block.querySelector('.archive-year-count');
      countEl.textContent = byYear[year].length;

      if (isLatest) {
        _populateArchiveYear(block);
      }

      tree.appendChild(block);
    }
  }

  function _createArchiveYearBlock(year, isOpen = false) {
    const block = document.createElement('div');
    block.className = isOpen ? 'archive-year open' : 'archive-year';
    block.dataset.year = year;
    block._messages = [];
    block._populated = false;
    const hdr = document.createElement('div');
    hdr.className = 'archive-year-header';
    hdr.innerHTML =
      `<span class="archive-year-chevron">&#9658;</span><span>${year}</span>` +
      `<span class="archive-year-count" style="margin-left:auto;font-weight:400;color:var(--text-muted);font-size:12px">0</span>`;
    hdr.addEventListener('click', () => {
      block.classList.toggle('open');
      if (block.classList.contains('open') && !block._populated) {
        _populateArchiveYear(block);
      }
    });
    const grid = document.createElement('div');
    grid.className = 'archive-year-grid';
    block.appendChild(hdr);
    block.appendChild(grid);
    return block;
  }

  function _appendToArchiveView(items, direction) {
    const tree = document.getElementById('media-archive-tree');
    // clear empty notice placeholder if present
    tree.querySelectorAll('.archive-empty-notice').forEach(el => el.remove());

    const filtered = items.filter(m => m.archive_path &&
      (direction === null || archiveSegments(m.archive_path).direction === direction));
    if (!filtered.length) return;

    for (const msg of filtered) {
      const { year } = archiveSegments(msg.archive_path);

      let block = tree.querySelector(`.archive-year[data-year="${year}"]`);
      if (!block) {
        const isLatest = !tree.querySelector('.archive-year');
        block = _createArchiveYearBlock(year, isLatest);
        // insert in descending year order
        const after = [...tree.querySelectorAll('.archive-year')]
          .find(el => parseInt(el.dataset.year) < parseInt(year));
        tree.insertBefore(block, after || null);
      }

      block._messages = block._messages || [];
      block._messages.push(msg);

      const countEl = block.querySelector('.archive-year-count');
      countEl.textContent = block._messages.length;

      if (block.classList.contains('open') && block._populated) {
        const grid = block.querySelector('.archive-year-grid');
        const mk = monthKey(msg.timestamp_ms);
        let monthHdr = grid.querySelector(`.gallery-month-header[data-month="${mk}"]`);
        if (!monthHdr) {
          monthHdr = document.createElement('div');
          monthHdr.className = 'gallery-month-header';
          monthHdr.dataset.month = mk;
          monthHdr.textContent = new Date(msg.timestamp_ms).toLocaleString('default', { month: 'long' });
          const olderHdr = [...grid.querySelectorAll('.gallery-month-header')]
            .find(el => parseInt(el.dataset.month) < mk);
          grid.insertBefore(monthHdr, olderHdr || null);
        }
        const nextSection = [...grid.querySelectorAll('.gallery-month-header')]
          .find(el => parseInt(el.dataset.month) < mk) || null;
        const cell = renderGalleryItem(msg);
        if (activeTypes.size > 0 && !activeTypes.has(cell.dataset.type || msg.media_type)) {
          cell.classList.add('type-hidden');
        }
        grid.insertBefore(cell, nextSection);
        if (cell.dataset.src) {
          getArchiveMediaObserver()?.observe(cell);
        }
      }
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

  function _buildMentionRegex(mentions) {
    if (!mentions || typeof mentions !== 'object') return null;
    const keys = Object.keys(mentions).filter(Boolean).sort((a, b) => b.length - a.length);
    if (!keys.length) return null;
    const escaped = keys.map(k => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
    return new RegExp('(^|[^A-Za-z0-9_])@(' + escaped + ')(?!\\d)', 'g');
  }

  function resolveMentionsPlainText(text, mentions) {
    if (!text) return '';
    const re = _buildMentionRegex(mentions);
    if (!re) return text;
    return text.replace(re, (full, prefix, tok) => {
      const m = mentions[tok];
      return m && m.name ? prefix + '@' + m.name : full;
    });
  }

  function waFormat(text, mentions) {
    if (!text) return '';

    const mentionRe = _buildMentionRegex(mentions);
    const mentionPlaceholders = [];

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

      // Protect @mentions from markdown formatting and XSS
      if (mentionRe) {
        mentionRe.lastIndex = 0;
        s = s.replace(mentionRe, (full, prefix, tok) => {
          const m = mentions[tok];
          if (!m || !m.name) return full;
          mentionPlaceholders.push(m);
          return prefix + '\x00MN' + (mentionPlaceholders.length - 1) + '\x00';
        });
      }

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
      let seg = out.join('<br>');
      if (mentionPlaceholders.length) {
        seg = seg.replace(/\x00MN(\d+)\x00/g, (_, i) => {
          const m = mentionPlaceholders[+i];
          if (!m || !m.name) return '';
          const label = '@' + esc(m.name);
          if (m.chat_id) {
            const cid = esc(String(m.chat_id));
            const ctype = esc(String(m.chat_type || 'contact'));
            return `<a href="#chat-${cid}" class="wa-mention wa-mention-link" data-chat-id="${cid}" data-chat-type="${ctype}" title="Open chat with ${esc(m.name)}">${label}</a>`;
          }
          return `<span class="wa-mention">${label}</span>`;
        });
      }
      return seg;

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

  function highlight(text, q, mentions) {
    if (!q || !text) return waFormat(text || '', mentions);
    const words = q.trim().split(/\s+/);
    let result = waFormat(text, mentions);
    for (const w of words) {
      const re = new RegExp(w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
      result = result.replace(/(<[^>]+>)|([^<]+)/g, (_, tag, txt) =>
        tag ? tag : txt.replace(re, mm => '<mark>' + mm + '</mark>')
      );
    }
    return result;
  }

  const COLLAPSE_CHAR_LIMIT = 750;
  const COLLAPSE_LINE_LIMIT = 15;
  const COLLAPSE_MIN_REMAINDER = 60;
  const COLLAPSE_MIN_LINE_REMAINDER = 2;
  const COLLAPSE_WORD_SNAP_WINDOW = 40;
  const COLLAPSE_LINE_BLOCK_TAGS = new Set(['PRE', 'LI', 'BLOCKQUOTE', 'H1', 'H2', 'H3']);
  const COLLAPSE_TAIL_BLOCK_TAGS = new Set(['PRE', 'UL', 'OL', 'BLOCKQUOTE', 'H1', 'H2', 'H3']);
  const expandedMsgIds = new Set();
  const expandedFallbackContainers = new WeakSet();
  const _graphemeSegmenter = typeof Intl !== 'undefined' && typeof Intl.Segmenter === 'function'
    ? new Intl.Segmenter('en', { granularity: 'grapheme' })
    : null;

  function splitGraphemes(str) {
    if (!str) return [];
    if (_graphemeSegmenter) {
      const out = [];
      for (const part of _graphemeSegmenter.segment(str)) {
        out.push(part.segment);
      }
      return out;
    }
    return Array.from(str);
  }

  function _isIgnoredListBr(parentNode, childEl) {
    return childEl.tagName === 'BR' && (parentNode.tagName === 'UL' || parentNode.tagName === 'OL');
  }

  function _endsWithCollapseBlock(containerEl) {
    const lastNode = containerEl.lastChild;
    return Boolean(
      lastNode && lastNode.nodeType === Node.ELEMENT_NODE && COLLAPSE_TAIL_BLOCK_TAGS.has(lastNode.tagName)
    );
  }

  function _scrollRowIntoViewIfClipped(containerEl) {
    const row = containerEl.closest('.msg-row');
    const scroll = document.getElementById('message-scroll');
    if (!row || !scroll) return;
    const rowRect = row.getBoundingClientRect();
    const scrollRect = scroll.getBoundingClientRect();
    if (rowRect.top < scrollRect.top || rowRect.bottom > scrollRect.bottom) {
      suppressScroll = true;
      try {
        row.scrollIntoView({ behavior: 'instant', block: 'nearest' });
      } finally {
        if (typeof requestAnimationFrame === 'function') {
          requestAnimationFrame(() => { suppressScroll = false; });
        } else {
          suppressScroll = false;
        }
      }
    }
  }

  function _focusToggleIfKeyboard(containerEl, clickEvent) {
    if (clickEvent.detail !== 0) return;
    const nextBtn = containerEl.querySelector('.msg-read-more');
    if (nextBtn) nextBtn.focus();
  }

  function measureRenderedContent(rootEl) {
    const state = { chars: 0, lineBreaks: 0, hasContent: false, justHadBreak: false };
    function walk(node) {
      for (const child of node.childNodes) {
        if (child.nodeType === Node.TEXT_NODE) {
          const val = child.nodeValue || '';
          if (!val) continue;
          const graphemes = splitGraphemes(val);
          state.chars += graphemes.length;
          for (const g of graphemes) {
            if (g === '\n' || g === '\r\n') {
              state.lineBreaks++;
              state.justHadBreak = true;
            } else {
              state.justHadBreak = false;
            }
          }
          state.hasContent = true;
          continue;
        }
        if (child.nodeType !== Node.ELEMENT_NODE) continue;
        if (child.tagName === 'BR') {
          if (_isIgnoredListBr(node, child)) continue;
          state.lineBreaks++;
          state.justHadBreak = true;
          state.hasContent = true;
          continue;
        }
        if (COLLAPSE_LINE_BLOCK_TAGS.has(child.tagName)) {
          if (state.hasContent && !state.justHadBreak) {
            state.lineBreaks++;
          }
          state.justHadBreak = false;
          state.hasContent = true;
          walk(child);
          state.justHadBreak = false;
          continue;
        }
        walk(child);
      }
    }
    walk(rootEl);
    const lineCount = (state.chars > 0 || state.lineBreaks > 0) ? state.lineBreaks + 1 : 0;
    return { charCount: state.chars, lineCount };
  }

  function _trimTrailingCollapsedNodes(fragment) {
    while (fragment.lastChild) {
      const last = fragment.lastChild;
      if (last.nodeType === Node.ELEMENT_NODE && last.tagName === 'BR') {
        fragment.removeChild(last);
        continue;
      }
      if (last.nodeType === Node.TEXT_NODE) {
        const trimmed = last.nodeValue.replace(/\s+$/, '');
        if (!trimmed) {
          fragment.removeChild(last);
          continue;
        }
        last.nodeValue = trimmed;
        break;
      }
      if (last.nodeType === Node.ELEMENT_NODE) {
        _trimTrailingCollapsedNodes(last);
        if (!last.hasChildNodes() && last.tagName !== 'BR') {
          fragment.removeChild(last);
          continue;
        }
      }
      break;
    }
  }

  function truncateFormattedTree(rootEl, charLimit, lineLimit) {
    const fragment = document.createDocumentFragment();
    const maxBreaks = lineLimit - 1;
    const state = { chars: 0, lineBreaks: 0, hasContent: false, justHadBreak: false, stopped: false };

    function visit(srcNode, destParent) {
      for (const child of srcNode.childNodes) {
        if (state.stopped) return;

        if (child.nodeType === Node.TEXT_NODE) {
          const val = child.nodeValue || '';
          if (!val) continue;
          const graphemes = splitGraphemes(val);
          let take = 0;
          let hitCharLimit = false;
          for (let i = 0; i < graphemes.length; i++) {
            const g = graphemes[i];
            if (g === '\n' || g === '\r\n') {
              if (state.lineBreaks + 1 > maxBreaks) {
                state.stopped = true;
                break;
              }
              state.lineBreaks++;
              state.justHadBreak = true;
            } else {
              state.justHadBreak = false;
            }
            if (state.chars + 1 > charLimit) {
              hitCharLimit = true;
              state.stopped = true;
              break;
            }
            state.chars++;
            state.hasContent = true;
            take = i + 1;
          }

          if (hitCharLimit && take > 0) {
            const minSnap = Math.max(0, take - COLLAPSE_WORD_SNAP_WINDOW);
            for (let j = take - 1; j >= minSnap; j--) {
              if (/\s/.test(graphemes[j])) {
                if (j > 0 || state.chars > take) {
                  take = j;
                }
                break;
              }
            }
          }

          if (take > 0) {
            destParent.appendChild(document.createTextNode(graphemes.slice(0, take).join('')));
          }
          continue;
        }

        if (child.nodeType !== Node.ELEMENT_NODE) continue;

        if (child.tagName === 'BR') {
          if (_isIgnoredListBr(srcNode, child)) continue;
          if (state.lineBreaks + 1 > maxBreaks) {
            state.stopped = true;
            return;
          }
          state.lineBreaks++;
          state.justHadBreak = true;
          state.hasContent = true;
          destParent.appendChild(document.createElement('br'));
          continue;
        }

        if (child.tagName === 'A' && state.chars > 0) {
          const linkLen = splitGraphemes(child.textContent || '').length;
          if (state.chars + linkLen > charLimit) {
            state.stopped = true;
            return;
          }
        }

        if (COLLAPSE_LINE_BLOCK_TAGS.has(child.tagName)) {
          if (state.hasContent && !state.justHadBreak) {
            if (state.lineBreaks + 1 > maxBreaks) {
              state.stopped = true;
              return;
            }
            state.lineBreaks++;
          }
          state.justHadBreak = false;
          state.hasContent = true;
        }

        const clone = child.cloneNode(false);
        visit(child, clone);
        if (clone.hasChildNodes() || !child.hasChildNodes()) {
          destParent.appendChild(clone);
        }
        if (COLLAPSE_LINE_BLOCK_TAGS.has(child.tagName)) {
          state.justHadBreak = false;
        }
      }
    }

    visit(rootEl, fragment);
    _trimTrailingCollapsedNodes(fragment);
    return fragment;
  }

  function createTextToggleButton(isExpanded, isBlockTail, onClick) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = isBlockTail ? 'msg-read-more is-block-tail' : 'msg-read-more';
    btn.setAttribute('aria-expanded', isExpanded ? 'true' : 'false');
    btn.setAttribute('aria-label', isExpanded ? 'Collapse message' : 'Read full message');
    btn.textContent = isExpanded ? 'Show Less' : 'Read More';
    btn.addEventListener('click', onClick);
    return btn;
  }

  function renderCollapsibleMessageText(containerEl, text, msgId, mentions) {
    const raw = text || '';
    const key = (msgId !== undefined && msgId !== null) ? String(msgId) : '';
    const canTrack = key !== '';
    const hasMentions = Boolean(mentions && typeof mentions === 'object' && Object.keys(mentions).length > 0);

    if (!hasMentions && raw.length <= COLLAPSE_CHAR_LIMIT) {
      let nlCount = 0;
      for (let i = 0; i < raw.length; i++) {
        if (raw.charCodeAt(i) === 10) {
          nlCount++;
          if (nlCount >= COLLAPSE_LINE_LIMIT) break;
        }
      }
      if (nlCount < COLLAPSE_LINE_LIMIT) {
        containerEl.innerHTML = highlight(raw, '', mentions);
        return;
      }
    }

    const fullHtml = highlight(raw, '', mentions);
    const parsed = document.createElement('div');
    parsed.innerHTML = fullHtml;
    const metrics = measureRenderedContent(parsed);
    const exceedsChars = metrics.charCount > (COLLAPSE_CHAR_LIMIT + COLLAPSE_MIN_REMAINDER);
    const exceedsLines = metrics.lineCount > (COLLAPSE_LINE_LIMIT + COLLAPSE_MIN_LINE_REMAINDER);

    if (!exceedsChars && !exceedsLines) {
      containerEl.innerHTML = fullHtml;
      return;
    }

    const isExpanded = canTrack ? expandedMsgIds.has(key) : expandedFallbackContainers.has(containerEl);
    if (isExpanded) {
      containerEl.innerHTML = fullHtml;
      const endsWithBlock = _endsWithCollapseBlock(containerEl);
      if (!endsWithBlock) {
        containerEl.appendChild(document.createTextNode(' '));
      }
      const btn = createTextToggleButton(true, endsWithBlock, e => {
        e.stopPropagation();
        if (canTrack) expandedMsgIds.delete(key);
        else expandedFallbackContainers.delete(containerEl);
        renderCollapsibleMessageText(containerEl, raw, msgId, mentions);
        _scrollRowIntoViewIfClipped(containerEl);
        _focusToggleIfKeyboard(containerEl, e);
      });
      containerEl.appendChild(btn);
      return;
    }

    containerEl.innerHTML = '';
    const truncatedFrag = truncateFormattedTree(parsed, COLLAPSE_CHAR_LIMIT, COLLAPSE_LINE_LIMIT);
    containerEl.appendChild(truncatedFrag);
    const endsWithBlock = _endsWithCollapseBlock(containerEl);
    const ellipsis = document.createElement('span');
    ellipsis.className = endsWithBlock ? 'msg-read-more-ellipsis is-block-tail' : 'msg-read-more-ellipsis';
    ellipsis.textContent = '... ';
    const btn = createTextToggleButton(false, endsWithBlock, e => {
      e.stopPropagation();
      if (canTrack) expandedMsgIds.add(key);
      else expandedFallbackContainers.add(containerEl);
      renderCollapsibleMessageText(containerEl, raw, msgId, mentions);
      _focusToggleIfKeyboard(containerEl, e);
    });
    containerEl.appendChild(ellipsis);
    containerEl.appendChild(btn);
  }

  function expandMessageRowById(msgId, row) {
    if (msgId !== undefined && msgId !== null) {
      expandedMsgIds.add(String(msgId));
    }
    if (!row) return;
    const targetId = (msgId !== undefined && msgId !== null) ? String(msgId) : (row.dataset.msgId || '');
    if (!targetId) return;
    expandedMsgIds.add(targetId);
    if (row.dataset.msgId) expandedMsgIds.add(row.dataset.msgId);
    const txtEl = row.querySelector('.msg-text, .msg-caption');
    let matchedMsg = msgList.find(m => String(m.msg_id) === targetId);
    if (txtEl && txtEl.classList.contains('msg-caption') && (!matchedMsg || !matchedMsg.text_body) && row.dataset.groupIds) {
      const gIds = row.dataset.groupIds.split(' ');
      const capMsg = msgList.find(m => gIds.includes(String(m.msg_id)) && m.text_body && m.text_body.trim());
      if (capMsg) {
        matchedMsg = capMsg;
        expandedMsgIds.add(String(capMsg.msg_id));
      }
    }
    if (txtEl && matchedMsg) {
      renderCollapsibleMessageText(txtEl, matchedMsg.text_body, matchedMsg.msg_id, matchedMsg.mentions);
    }
  }

  function makeDateSeparator(ts) {
    const el = document.createElement('div');
    el.className = 'date-separator';
    el.dataset.dayKey = dayKey(ts);
    el.innerHTML = `<span>${fmtDateSep(ts)}</span>`;
    return el;
  }

  function setTabEnabled(btnId, enabled, titleEnabled, titleDisabled) {
    const btn = document.getElementById(btnId);
    if (!btn) return;
    if (!enabled) {
      btn.classList.add('disabled');
      btn.setAttribute('aria-disabled', 'true');
      btn.setAttribute('tabindex', '-1');
      btn.title = titleDisabled;
    } else {
      btn.classList.remove('disabled');
      btn.removeAttribute('aria-disabled');
      btn.setAttribute('tabindex', '0');
      btn.title = titleEnabled;
    }
  }

  async function checkCallsAvailability() {
    const callsBtn = document.getElementById('tab-btn-calls');
    if (!callsBtn) return;
    try {
      const res = await fetch('/api/calls?limit=1');
      if (!res.ok) {
        throw new Error(`Server returned HTTP ${res.status} when checking calls availability`);
      }
      const data = await res.json();
      const calls = data.calls || [];
      setTabEnabled('tab-btn-calls', calls.length > 0, 'Calls', 'No call history in this backup');
    } catch (err) {
      console.warn('Failed to check calls availability:', err);
    }
  }

  // ---- load chats ----------------------------------------------------------

  async function loadChats() {
    if (currentNavTab === 'chats') {
      document.getElementById('chat-list').innerHTML = '<div id="chat-list-notice"><div class="loading-spinner"></div>Loading Chats…</div>';
    }
    const res = await fetch('/api/chats');
    allChats = await res.json();
    const hasArchived = allChats.some(c => c.is_archived);
    setTabEnabled('tab-btn-archived', hasArchived, 'Archived', 'No archived chats in this backup');
    await checkCallsAvailability();
    if (currentNavTab === 'chats') {
      renderChatList();
    } else if (currentNavTab === 'archived') {
      const q = document.getElementById('search-input')?.value || '';
      renderArchivedList(q);
    }
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
    if (currentNavTab === 'chats') {
      renderChatList();
    }
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
          _findChatListItem(chat.id, chat.type)?.querySelector('.index-spinner')?.remove();
          return;
        }
        const liveEl = _findChatListItem(chat.id, chat.type);
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
                        gif:'🎞 GIF', sticker:'🎭 Sticker', document:'📄 Document', link:'🔗 Link',
                        location:'📍 Location', live_location:'📍 Live location', vcard:'👤 Contact'};
  function chatPreview(chat) {
    const prefix = chat.last_msg_from_me ? 'You: ' : '';
    if (chat.last_msg_type === 'service') {
      return esc(chat.last_msg_preview || '…');
    }
    if (chat.last_msg_type === 'deleted') {
      return '<span class="deleted-preview">🚫 ' + esc(chat.last_msg_preview || 'This message was deleted') + '</span>';
    }
    if (chat.last_msg_type === 'poll') {
      return prefix + '📊 ' + esc(chat.last_msg_preview || 'Poll');
    }
    if (chat.is_view_once) {
      const voLabel = chat.last_msg_type === 'video' ? 'Video' : (chat.last_msg_type === 'audio' ? 'Voice message' : 'Photo');
      return prefix + '<span class="vo-preview">① ' + voLabel + '</span>';
    }
    if (chat.last_msg_type !== 'text') {
      return prefix + (MEDIA_LABELS[chat.last_msg_type] || '📎 Media');
    }
    const text = chat.last_msg_preview || '';
    return (prefix + esc(text.slice(0, 60))) || '…';
  }

  function _findChatListItem(chatId, chatType) {
    const idSel = CSS.escape(String(chatId));
    const typeSel = CSS.escape(String(chatType));
    return document.querySelector(`.chat-item[data-id="${idSel}"][data-type="${typeSel}"]`);
  }

  function _createChatListItem(chat) {
    const el = document.createElement('div');
    const isActive = currentChat && String(currentChat.id) === String(chat.id) && currentChat.type === chat.type;
    el.className = 'chat-item' + (chat.type === 'group' ? ' group' : '') + (isActive ? ' active' : '');
    el.dataset.id = chat.id;
    el.dataset.type = chat.type;

    const avatar = document.createElement('div');
    avatar.className = 'chat-item-avatar';
    const avatarKey = chat.phone_number || chat.id || chat.display_name;
    avatar.style.backgroundColor = getAvatarColor(avatarKey);
    avatar.textContent = getInitials(chat.display_name);
    avatar.setAttribute('aria-hidden', 'true');
    el.appendChild(avatar);

    const body = document.createElement('div');
    body.className = 'chat-item-body';
    const tagHtml = chat.is_preserved_only
      ? '<span class="chat-preserved-tag" title="Preserved from backup • Chat deleted on device">Preserved</span>'
      : '';
    body.innerHTML = `<div class="chat-name">${esc(chat.display_name)}${tagHtml}</div><div class="chat-meta">${chatPreview(chat)} · ${fmtTime(chat.newest_ts)}</div>`;
    el.appendChild(body);

    el.addEventListener('click', () => selectChat(chat, el));
    return el;
  }

  function renderChatList() {
    const list = document.getElementById('chat-list');
    list.innerHTML = '';
    const activeChats = allChats.filter(c => !c.is_archived);
    const visible = currentFilter === 'all' ? activeChats : activeChats.filter(c => c.type === currentFilter);
    visible.forEach(chat => list.appendChild(_createChatListItem(chat)));
  }

  function renderArchivedList(searchQuery) {
    const list = document.getElementById('chat-list');
    if (!list) return;
    list.innerHTML = '';

    const archivedChats = allChats.filter(c => Boolean(c.is_archived));
    if (archivedChats.length === 0) {
      list.innerHTML = '<div class="sidebar-tab-placeholder"><span class="placeholder-icon">📦</span><strong>Archived Chats</strong><span>Chats tucked away will appear here</span></div>';
      return;
    }

    const q = (searchQuery || '').trim().toLowerCase();
    const matches = q
      ? archivedChats.filter(c => (c.display_name || '').toLowerCase().includes(q) || (c.last_msg_preview || '').toLowerCase().includes(q) || String(c.id || '').toLowerCase().includes(q))
      : archivedChats;

    if (matches.length === 0) {
      list.innerHTML = `<div class="sidebar-tab-placeholder"><span class="placeholder-icon">🔍</span><strong>No Matches</strong><span>No archived chats matching "${esc(searchQuery)}"</span></div>`;
      return;
    }

    matches.forEach(chat => list.appendChild(_createChatListItem(chat)));
  }

  // ---- bottom navigation bar -----------------------------------------------

  let currentNavTab = 'chats';

  function switchNavTab(tabName) {
    if (currentNavTab === tabName) return;
    currentNavTab = tabName;

    const tabs = ['chats', 'calls', 'starred', 'archived'];
    tabs.forEach(t => {
      const btn = document.getElementById('tab-btn-' + t);
      if (btn) {
        const isActive = t === tabName;
        btn.classList.toggle('active', isActive);
        btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
        btn.setAttribute('tabindex', isActive ? '0' : '-1');
      }
    });

    const filtersEl = document.getElementById('sidebar-filters');
    const searchInput = document.getElementById('search-input');
    const chatListEl = document.getElementById('chat-list');
    const searchResults = document.getElementById('search-results');
    if (searchResults) {
      searchResults.classList.remove('has-results');
      searchResults.innerHTML = '';
    }
    if (searchInput) {
      searchInput.value = '';
    }
    updateSidebarSearchClearBtn();

    if (tabName === 'chats') {
      if (searchInput) searchInput.placeholder = 'Search messages & chats…';
      if (filtersEl) filtersEl.style.display = 'flex';
      const callInspector = document.getElementById('pane-call-inspector');
      if (callInspector) callInspector.style.display = 'none';
      if (currentChat) {
        const chatHeader = document.getElementById('chat-header');
        const msgScroll = document.getElementById('message-scroll');
        const emptyPane = document.getElementById('empty-pane');
        if (chatHeader) chatHeader.style.display = 'flex';
        if (msgScroll) msgScroll.style.display = 'flex';
        if (emptyPane) emptyPane.style.display = 'none';
      } else {
        const emptyPane = document.getElementById('empty-pane');
        if (emptyPane) {
          emptyPane.textContent = 'Select a chat to browse messages';
          emptyPane.style.display = 'flex';
        }
      }
      renderChatList();
    } else {
      if (filtersEl) filtersEl.style.display = 'none';
      const callInspector = document.getElementById('pane-call-inspector');
      if (callInspector) callInspector.style.display = 'none';

      if (tabName === 'calls') {
        if (searchInput) searchInput.placeholder = 'Search call history…';
        const emptyPane = document.getElementById('empty-pane');
        const chatHeader = document.getElementById('chat-header');
        const msgScroll = document.getElementById('message-scroll');
        const pinnedBanner = document.getElementById('pinned-banner');
        const contextBanner = document.getElementById('context-jump-banner');

        if (chatHeader) chatHeader.style.display = 'none';
        if (msgScroll) msgScroll.style.display = 'none';
        if (pinnedBanner) pinnedBanner.style.display = 'none';
        if (contextBanner) contextBanner.style.display = 'none';
        closeChatInfo();
        closeChatStarred();
        closeMediaGallery();

        if (emptyPane) {
          emptyPane.textContent = 'Select a call from the history to view connection metrics and duration.';
          emptyPane.style.display = 'flex';
        }

        loadCallsList(true);
      } else if (tabName === 'starred') {
        _activeStarredMsgId = null;
        if (searchInput) searchInput.placeholder = 'Search starred bookmarks…';
        const emptyPane = document.getElementById('empty-pane');
        const chatHeader = document.getElementById('chat-header');
        const msgScroll = document.getElementById('message-scroll');
        const pinnedBanner = document.getElementById('pinned-banner');
        const contextBanner = document.getElementById('context-jump-banner');

        if (chatHeader) chatHeader.style.display = 'none';
        if (msgScroll) msgScroll.style.display = 'none';
        if (pinnedBanner) pinnedBanner.style.display = 'none';
        if (contextBanner) contextBanner.style.display = 'none';
        closeChatInfo();
        closeChatStarred();
        closeMediaGallery();

        if (emptyPane) {
          emptyPane.textContent = 'Select a starred message to jump to its conversational context.';
          emptyPane.style.display = 'flex';
        }

        loadStarredList(true);
      } else if (tabName === 'archived') {
        if (searchInput) searchInput.placeholder = 'Search archived chats…';
        const emptyPane = document.getElementById('empty-pane');
        const chatHeader = document.getElementById('chat-header');
        const msgScroll = document.getElementById('message-scroll');
        const pinnedBanner = document.getElementById('pinned-banner');
        const contextBanner = document.getElementById('context-jump-banner');

        if (chatHeader) chatHeader.style.display = 'none';
        if (msgScroll) msgScroll.style.display = 'none';
        if (pinnedBanner) pinnedBanner.style.display = 'none';
        if (contextBanner) contextBanner.style.display = 'none';
        closeChatInfo();
        closeChatStarred();
        closeMediaGallery();

        if (emptyPane) {
          emptyPane.textContent = 'Select an archived chat to browse messages.';
          emptyPane.style.display = 'flex';
        }

        renderArchivedList('');
      }
    }
  }

  const bottomBar = document.getElementById('sidebar-bottom-bar');
  if (bottomBar) {
    const tabBtns = Array.from(bottomBar.querySelectorAll('.tab-btn'));
    tabBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        if (btn.classList.contains('disabled') || btn.disabled) return;
        const tab = btn.dataset.tab;
        if (tab) switchNavTab(tab);
      });
      btn.addEventListener('keydown', e => {
        const enabledBtns = tabBtns.filter(b => !b.classList.contains('disabled') && !b.disabled);
        const currentIdx = enabledBtns.indexOf(btn);
        if (currentIdx === -1) return;
        let targetIdx = -1;
        if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
          targetIdx = (currentIdx + 1) % enabledBtns.length;
        } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
          targetIdx = (currentIdx - 1 + enabledBtns.length) % enabledBtns.length;
        } else if (e.key === 'Home') {
          targetIdx = 0;
        } else if (e.key === 'End') {
          targetIdx = enabledBtns.length - 1;
        }
        if (targetIdx !== -1) {
          e.preventDefault();
          const targetBtn = enabledBtns[targetIdx];
          targetBtn.focus();
          const tab = targetBtn.dataset.tab;
          if (tab) switchNavTab(tab);
        }
      });
    });
  }

  // ---- select chat ---------------------------------------------------------

  function _prepareChatPane(chat, isJumping) {
    closeChatInfo();
    closeChatStarred();
    closeMediaGallery();
    currentChat = chat;
    msgList = [];
    expandedMsgIds.clear();
    noMoreOlder = false;
    noMoreNewer = !isJumping;
    isUserAtBottom = !isJumping;

    const callInspector = document.getElementById('pane-call-inspector');
    if (callInspector) callInspector.style.display = 'none';

    const scroll = document.getElementById('message-scroll');
    scroll.innerHTML = '';
    scroll.style.display = 'flex';
    scroll.classList.toggle('is-navigating', isJumping);
    document.getElementById('scroll-to-bottom').classList.remove('visible');

    document.getElementById('chat-header').style.display = 'flex';
    document.getElementById('chat-title').textContent = chat.display_name;

    const avatarEl = document.getElementById('chat-header-avatar');
    if (avatarEl) {
      const avatarKey = chat.phone_number || chat.id || chat.display_name;
      avatarEl.style.backgroundColor = getAvatarColor(avatarKey);
      avatarEl.textContent = getInitials(chat.display_name);
    }

    const subtitleEl = document.getElementById('chat-subtitle');
    if (subtitleEl) {
      if (chat.type === 'group') {
        let subHtml = 'Group chat';
        if (chat.is_preserved_only) {
          subHtml += ' <span class="chat-preserved-tag" title="Preserved from backup • Chat deleted on device">Preserved</span>';
        }
        subtitleEl.innerHTML = subHtml;
      } else {
        const formattedPhone = formatPhoneNumber(chat.phone_number || chat.id);
        let subHtml = formattedPhone ? esc(formattedPhone) : '';
        if (chat.is_preserved_only) {
          subHtml += (subHtml ? ' ' : '') + '<span class="chat-preserved-tag" title="Preserved from backup • Chat deleted on device">Preserved</span>';
        }
        subtitleEl.innerHTML = subHtml;
      }
    }

    document.getElementById('empty-pane').style.display = 'none';
    const sr = document.getElementById('search-results');
    if (sr) {
      sr.classList.remove('has-results');
      sr.innerHTML = '';
    }

    if (_jumpAbortController && !isJumping) {
      _jumpAbortController.abort();
    }
    if (!isJumping) {
      hideJumpHud();
    }
    document.getElementById('chat-search-input').value = '';
    document.getElementById('chat-search-clear').style.display = 'none';
    clearChatSearch();
    document.getElementById('date-picker-input').value = '';
    document.getElementById('date-go-btn').style.display = 'none';
    document.getElementById('date-clear-btn').style.display = 'none';

    if (!isJumping) {
      const contextBanner = document.getElementById('context-jump-banner');
      if (contextBanner) contextBanner.style.display = 'none';
    }

    updateChatStarredBadge(chat.id);
    const banner = document.getElementById('pinned-banner');
    const cachedPins = _chatPinsCache.get(String(chat.id));
    if (cachedPins && cachedPins.length > 0) {
      _chatPins = cachedPins.filter(p => !_dismissedPins.has(p.id));
      _currentPinIndex = 0;
      renderPinnedBanner();
    } else if (banner) {
      _chatPins = [];
      banner.style.display = 'none';
    }
    loadChatPinnedBanner(chat.id, chat.type);
    return scroll;
  }

  async function selectChat(chat, el) {
    _indexPollGen++;
    _activeStarredMsgId = null;
    const gen = ++_loadGen;
    loading = false;
    document.querySelectorAll('.chat-item').forEach(e => e.classList.remove('active'));
    if (el) {
      el.classList.add('active');
      el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    const scroll = _prepareChatPane(chat, false);

    const loadingEl = document.getElementById('chat-loading');
    loadingEl.style.display = 'flex';

    try {
      await loadMessages('older');
      if (gen !== _loadGen) return;
      suppressScroll = true;
      scroll.scrollTop = scroll.scrollHeight;
      suppressScroll = false;
    } catch (err) {
      console.error('Failed to load chat messages:', err);
    } finally {
      if (gen === _loadGen) {
        loadingEl.style.display = 'none';
        suppressScroll = false;
      }
    }
    if (gen === _loadGen) {
      await _pinToBottom(gen);
    }

    _startIndexPoll(chat);
  }

  // ---- load messages -------------------------------------------------------

  let loading = false;
  let _loadGen = 0;
  let noMoreOlder = false;
  let noMoreNewer = false;
  let isUserAtBottom = true;

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
      params.set('inclusive', '1');
    } else if (direction === 'newer' && msgList.length > 0) {
      params.set('after', msgList[msgList.length - 1].timestamp_ms);
      params.set('inclusive', '1');
    }

    showSpinner(direction === 'older' ? 'top' : 'bottom');

    try {
      let res = await fetch('/api/messages?' + params);
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}: ${res.statusText}`);
      }
      let msgs = await res.json();
      if (gen !== _loadGen) {
        removeSpinner(direction === 'older' ? 'top' : 'bottom');
        return;
      }

      let addedCount = mergePage(msgs);
      if (addedCount === 0 && msgs.length >= 50 && params.has('inclusive')) {
        // More than one page shares the boundary timestamp; fall back to strict < / >.
        params.delete('inclusive');
        res = await fetch('/api/messages?' + params);
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}: ${res.statusText}`);
        }
        msgs = await res.json();
        if (gen !== _loadGen) {
          removeSpinner(direction === 'older' ? 'top' : 'bottom');
          return;
        }
        addedCount = mergePage(msgs);
      }

      removeSpinner(direction === 'older' ? 'top' : 'bottom');

      if (!msgs.length || addedCount === 0) {
        if (direction === 'older') noMoreOlder = true;
        else noMoreNewer = true;
        return;
      }
      if (direction === 'older' && msgs.length < 50 && !params.has('before')) {
        noMoreOlder = true;
      }

      suppressScroll = true;
      try {
        const anchor = captureAnchor(direction);
        pruneList(direction);
        renderWindow();
        restoreAnchor(anchor);
      } finally {
        suppressScroll = false;
      }

    } catch (err) {
      removeSpinner(direction === 'older' ? 'top' : 'bottom');
      console.error('loadMessages error:', err);
    } finally {
      if (gen === _loadGen) {
        loading = false;
      }
    }
  }

  // ---- media grouping -------------------------------------------------------
  //
  // Groups consecutive image/video messages from the same sender (within 2 min gaps)
  // into a 2x2 grid bubble. A run must have >= 4 items to qualify.

  const MEDIA_GROUP_GAP_MS = 2 * 60 * 1000;
  const MEDIA_GROUP_MIN = 4;

  function isVideoNote(msg) {
    if (!msg) return false;
    if (Boolean(msg.is_video_note) || msg.raw_msg_type === 28 || msg.raw_msg_type === 81 || msg.raw_msg_type === 54) return true;
    const p = (msg.archive_path || msg.original_path || '').toLowerCase().replace(/\\/g, '/');
    return p.includes('/whatsapp video notes/') || p.startsWith('whatsapp video notes/') || /(^|\/)ptv-\d+/i.test(p);
  }

  function isVoiceNote(msg) {
    if (!msg) return false;
    if (msg.is_voice_note === 1 || msg.is_voice_note === true) return true;
    if (msg.waveform || msg.audio_waveform) return true;
    const p = String(msg.archive_path || msg.original_path || msg.media_name || '').toLowerCase();
    return p.includes('voice notes') || p.includes('ptt-') || /(^|\/)audi_|\.opus($|\?)/i.test(p);
  }

  function isGrouableMedia(msg) {
    return !msg.is_view_once && msg.archive_path && !isVideoNote(msg) &&
      (msg.media_type === 'image' || msg.media_type === 'video' || msg.media_type === 'gif');
  }

  function sameSender(a, b) {
    if (a.from_me !== b.from_me) return false;
    // For 1:1 chats, from_me alone is sufficient.
    // For groups, check display name; unknown sender (empty string) never matches.
    if (currentChat && currentChat.type === 'group') {
      if (!a.sender || !b.sender) return false;
      return a.sender === b.sender;
    }
    return true;
  }

  function rowForMsgId(id) {
    const key = String(id);
    const escKey = (typeof CSS !== 'undefined' && typeof CSS.escape === 'function')
      ? CSS.escape(key)
      : key.replace(/(["\\:])/g, '\\$1');
    const direct = document.querySelector(`.msg-row[data-msg-id="${escKey}"]`);
    if (direct) return direct;
    return document.querySelector(`.msg-row[data-group-ids~="${escKey}"]`) || null;
  }

  function nearestMsgToTs(ts) {
    if (!msgList.length) return null;
    return msgList.reduce((best, m) =>
      Math.abs(m.timestamp_ms - ts) < Math.abs(best.timestamp_ms - ts) ? m : best
    );
  }

  function captureAnchor(direction) {
    const scroll = document.getElementById('message-scroll');
    const top = scroll.getBoundingClientRect().top;
    const bottom = scroll.getBoundingClientRect().bottom;
    const rows = scroll.querySelectorAll('.msg-row[data-msg-id]');
    if (!rows.length) return null;

    if (direction === 'newer') {
      for (let i = rows.length - 1; i >= 0; i--) {
        const row = rows[i];
        const rect = row.getBoundingClientRect();
        if (rect.top < bottom && rect.bottom > top) {
          return { id: row.dataset.msgId, offset: rect.top - top };
        }
      }
    } else {
      for (let i = 0; i < rows.length; i++) {
        const row = rows[i];
        const rect = row.getBoundingClientRect();
        if (rect.bottom > top) {
          return { id: row.dataset.msgId, offset: rect.top - top };
        }
      }
    }
    return null;
  }

  function restoreAnchor(anchor) {
    if (!anchor) return;
    const row = rowForMsgId(anchor.id);
    if (!row) return;
    const scroll = document.getElementById('message-scroll');
    const top = scroll.getBoundingClientRect().top;
    scroll.scrollTop += (row.getBoundingClientRect().top - top) - anchor.offset;
  }

  function mergePage(newMsgs) {
    const seenIds = new Map(msgList.map(m => [String(m.msg_id), m]));
    let addedCount = 0;
    for (const m of newMsgs) {
      if (!seenIds.has(String(m.msg_id))) {
        seenIds.set(String(m.msg_id), m);
        msgList.push(m);
        addedCount++;
      }
    }
    // Discard any preserved duplicates when matching non-deleted live messages are present
    const liveStanzas = new Set();
    const liveKeys = new Set();
    for (const m of msgList) {
      if (!m.is_preserved && m.media_type !== 'deleted') {
        if (m.stanza_id) liveStanzas.add(m.stanza_id);
        liveKeys.add(`${m.timestamp_ms}_${m.from_me ? 1 : 0}`);
      }
    }
    if (liveStanzas.size > 0 || liveKeys.size > 0) {
      msgList = msgList.filter(m => {
        if (m.is_preserved) {
          if (m.stanza_id && liveStanzas.has(m.stanza_id)) return false;
          if (liveKeys.has(`${m.timestamp_ms}_${m.from_me ? 1 : 0}`)) return false;
        }
        return true;
      });
    }
    msgList.sort((a, b) => {
      const tsDiff = (a.timestamp_ms || 0) - (b.timestamp_ms || 0);
      if (tsDiff !== 0) return tsDiff;
      const sa = Number(a.sort_id ?? a.msg_id);
      const sb = Number(b.sort_id ?? b.msg_id);
      if (!Number.isNaN(sa) && !Number.isNaN(sb) && sa !== sb) return sa - sb;
      return String(a.msg_id ?? '').localeCompare(String(b.msg_id ?? ''));
    });
    return addedCount;
  }

  function pruneList(direction) {
    if (msgList.length <= MAX_WINDOW) return;
    const drop = msgList.length - MAX_WINDOW;
    if (direction === 'older') {
      msgList.splice(msgList.length - drop, drop);
      noMoreNewer = false;
    } else {
      msgList.splice(0, drop);
      noMoreOlder = false;
    }
  }

  function computeRenderUnits(list) {
    const units = [];
    for (let i = 0; i < list.length;) {
      const first = list[i];
      if (first.is_live_photo_video) {
        i++;
        continue;
      }
      if (isGrouableMedia(first)) {
        const run = [first];
        let j = i + 1;
        while (j < list.length) {
          const prev = run[run.length - 1];
          const curr = list[j];
          if (curr.is_live_photo_video) {
            j++;
            continue;
          }
          if (!isGrouableMedia(curr)) break;
          if (!sameSender(first, curr)) break;
          if (curr.timestamp_ms - prev.timestamp_ms > MEDIA_GROUP_GAP_MS) break;
          run.push(curr);
          j++;
        }
        if (run.length >= MEDIA_GROUP_MIN) {
          units.push({ type: 'group', members: run });
          i = j;
          continue;
        }
      }
      units.push({ type: 'single', msg: first });
      i++;
    }
    return units;
  }

  function renderWindow() {
    const scroll = document.getElementById('message-scroll');
    scroll.innerHTML = '';
    const units = computeRenderUnits(msgList);
    let prevTs = null;
    for (const unit of units) {
      const ts = unit.type === 'group' ? unit.members[0].timestamp_ms : unit.msg.timestamp_ms;
      if (prevTs === null || dayKey(ts) !== dayKey(prevTs)) {
        scroll.appendChild(makeDateSeparator(ts));
      }
      scroll.appendChild(renderBubble(unit));
      prevTs = unit.type === 'group'
        ? unit.members[unit.members.length - 1].timestamp_ms
        : unit.msg.timestamp_ms;
    }
  }

  function flashHighlightRow(row, highlightType) {
    if (!row) return;
    const bubble = row.querySelector('.msg-bubble, .msg-service-bubble');
    if (bubble) {
      const animClass = highlightType === 'gold'
        ? 'jump-highlight-gold'
        : (highlightType === 'emerald' ? 'jump-highlight-emerald' : 'search-jump-highlight');
      const duration = (highlightType === 'gold' || highlightType === 'emerald') ? 2200 : 1200;
      bubble.classList.remove('search-jump-highlight', 'jump-highlight-gold', 'jump-highlight-emerald');
      void bubble.offsetWidth;
      bubble.classList.add(animClass);
      const timer = setTimeout(() => bubble.classList.remove(animClass), duration);
      bubble.addEventListener('animationend', () => {
        clearTimeout(timer);
        bubble.classList.remove(animClass);
      }, { once: true });
    }
  }

  async function jumpToMessage(msg, highlightType) {
    const inWin = rowForMsgId(msg.msg_id);
    if (inWin) {
      const gen = ++_loadGen;
      isUserAtBottom = false;
      const loadingEl = document.getElementById('chat-loading');
      if (loadingEl) loadingEl.style.display = 'none';
      const btn = document.getElementById('scroll-to-bottom');
      if (btn) btn.classList.add('visible');
      await _pinToMsg(inWin, gen);
      flashHighlightRow(inWin, highlightType);
      return inWin;
    }
    const row = await jumpToTimestamp(msg.timestamp_ms, msg.msg_id, true, highlightType);
    return row;
  }

  function _attachMediaScrollAnchor(mediaEl) {
    mediaEl.addEventListener('load', () => {
      const scroll = document.getElementById('message-scroll');
      if (!scroll || !isUserAtBottom) return;
      suppressScroll = true;
      scroll.scrollTop = scroll.scrollHeight;
      suppressScroll = false;
    });
  }

  const gifViewportObserver = typeof IntersectionObserver !== 'undefined'
    ? new IntersectionObserver((entries) => {
        for (const entry of entries) {
          const vid = entry.target;
          const container = vid.closest('.msg-gif');
          if (!container) continue;
          if (!entry.isIntersecting) {
            if (!vid.paused) {
              vid.pause();
            }
          } else {
            if (container.classList.contains('is-playing') && vid.paused) {
              vid.play().catch(() => {});
            }
          }
        }
      }, { threshold: 0.05 })
    : null;

  let activeVideoNotePause = null;

  const videoNoteViewportObserver = typeof IntersectionObserver !== 'undefined'
    ? new IntersectionObserver((entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting && typeof entry.target._pauseVideoNote === 'function') {
            entry.target._pauseVideoNote();
          }
        }
      }, { threshold: 0.05 })
    : null;

  function renderMediaGrid(group) {
    const grid = document.createElement('div');
    grid.className = 'media-grid';

    const visible = group.slice(0, 4);
    visible.forEach((msg, idx) => {
      const cell = document.createElement('div');
      cell.className = 'media-grid-cell';

      // Build a lightweight media element (no caption, no reactions in grid)
      const wrap = document.createElement('div');
      wrap.className = 'msg-media';
      const src = '/media/' + msg.archive_path;
      const mt = msg.media_type;

      if (mt === 'image' || mt === 'sticker') {
        const cleanName = _sanitizeMediaName(msg.media_name);
        const img = document.createElement('img');
        img.src = src;
        img.loading = 'lazy';
        img.alt = cleanName || (mt === 'sticker' ? 'Sticker' : 'Photo');
        img.addEventListener('click', () => showLightboxForGroup(group, idx));
        img.addEventListener('error', () => {
          msg.is_missing = 1;
          const missingCard = renderMissingMedia(msg);
          if (wrap.isConnected) {
            wrap.replaceWith(missingCard);
          } else {
            wrap.innerHTML = '';
            wrap.className = missingCard.className;
            wrap.style.cssText = missingCard.style.cssText;
            while (missingCard.firstChild) {
              wrap.appendChild(missingCard.firstChild);
            }
          }
        }, { once: true });
        _attachMediaScrollAnchor(img);
        wrap.appendChild(img);
      } else if (mt === 'video' || mt === 'gif') {
        const vid = document.createElement('video');
        vid.controls = (mt === 'video');
        vid.preload = 'metadata';
        vid.src = src;
        const handleGridVideoError = () => {
          msg.is_missing = 1;
          const missingCard = renderMissingMedia(msg);
          if (wrap.isConnected) {
            wrap.replaceWith(missingCard);
          } else {
            wrap.innerHTML = '';
            wrap.className = missingCard.className;
            wrap.style.cssText = missingCard.style.cssText;
            while (missingCard.firstChild) {
              wrap.appendChild(missingCard.firstChild);
            }
          }
        };
        vid.addEventListener('error', handleGridVideoError, { once: true });
        if (mt === 'gif') {
          vid.muted = true;
          vid.playsInline = true;
          vid.loop = true;
          const badgeWrap = document.createElement('div');
          badgeWrap.className = 'msg-gif-badge-wrap';
          badgeWrap.setAttribute('aria-hidden', 'true');
          const badge = document.createElement('div');
          badge.className = 'msg-gif-badge';
          badge.textContent = 'GIF';
          badgeWrap.appendChild(badge);
          wrap.appendChild(badgeWrap);
        }
        vid.addEventListener('click', () => showLightboxForGroup(group, idx));
        vid.addEventListener('loadeddata', () => {
          if (vid.dataset.posterCaptured || !vid.videoWidth || !vid.videoHeight) return;
          vid.dataset.posterCaptured = '1';
          const canvas = document.createElement('canvas');
          canvas.width = vid.videoWidth;
          canvas.height = vid.videoHeight;
          canvas.getContext('2d').drawImage(vid, 0, 0);
          vid.poster = canvas.toDataURL('image/jpeg', 0.8);
          const scroll = document.getElementById('message-scroll');
          if (scroll && isUserAtBottom) {
            suppressScroll = true;
            scroll.scrollTop = scroll.scrollHeight;
            suppressScroll = false;
          }
        }, { once: true });
        wrap.appendChild(vid);
      }

      cell.appendChild(wrap);

      // Overflow badge on the last visible cell
      if (idx === 3 && group.length > 4) {
        const badge = document.createElement('div');
        badge.className = 'media-grid-overflow';
        badge.textContent = '+' + (group.length - 4);
        cell.appendChild(badge);
      }

      grid.appendChild(cell);
    });

    return grid;
  }

  function showLightboxForGroup(group, index) {
    if (group[index] && group[index].is_missing) return;
    showLightbox(group[index]);
  }


  // ---- scroll trigger -------------------------------------------------------

  function setupScrollTrigger() {
    const scroll = document.getElementById('message-scroll');
    const btn = document.getElementById('scroll-to-bottom');
    scroll.addEventListener('scroll', function () {
      if (suppressScroll) return;
      hideMsgDetails();
      const atBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 100;
      isUserAtBottom = atBottom;
      btn.classList.toggle('visible', !atBottom);
      if (scroll.scrollTop < 100) {
        if (loading) return;
        loadMessages('older');
      } else if (atBottom) {
        if (loading) return;
        loadMessages('newer');
      }
    });
    scroll.addEventListener('wheel', function (e) {
      if (e.deltaY < 0 && scroll.scrollTop < 100) {
        if (loading || noMoreOlder) return;
        loadMessages('older');
      }
    }, { passive: true });
    btn.addEventListener('click', async () => {
      isUserAtBottom = true;
      if (noMoreNewer) {
        await _pinToBottom(_loadGen);
        return;
      }
      const gen = ++_loadGen;
      msgList = [];
      noMoreOlder = false;
      noMoreNewer = true;
      await loadMessages('older');
      if (gen !== _loadGen) return;
      suppressScroll = true;
      scroll.scrollTop = scroll.scrollHeight;
      suppressScroll = false;
      if (gen !== _loadGen) return;
      await _pinToBottom(gen);
    });
  }
  setupScrollTrigger();

  function createMetaPinIcon() {
    const pinIcon = document.createElement('span');
    pinIcon.className = 'msg-meta-icon msg-pinned-icon';
    pinIcon.title = 'Pinned message';
    pinIcon.setAttribute('aria-label', 'Pinned message');
    pinIcon.innerHTML = '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="17" x2="12" y2="22"></line><path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z"></path></svg>';
    return pinIcon;
  }

  function createMetaStarIcon() {
    const starIcon = document.createElement('span');
    starIcon.className = 'msg-meta-icon msg-starred-icon';
    starIcon.title = 'Starred message';
    starIcon.setAttribute('aria-label', 'Starred message');
    starIcon.innerHTML = '<svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"></polygon></svg>';
    return starIcon;
  }

  function getSenderColorClass(senderName) {
    const key = (senderName || '').trim();
    if (!key || key === 'You') return '';
    let h = 2166136261;
    for (let i = 0; i < key.length; i++) {
      h ^= key.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return 'sender-color-' + ((h >>> 0) % 10);
  }

  function createSenderEl(senderName) {
    const senderEl = document.createElement('div');
    const colorCls = (currentChat && currentChat.type === 'group') ? getSenderColorClass(senderName) : '';
    senderEl.className = colorCls ? ('msg-sender ' + colorCls) : 'msg-sender';
    senderEl.textContent = senderName || '';
    return senderEl;
  }

  function renderBubble(unit) {
    if (unit.type === 'group') {
      const group = unit.members;
      const msg = group[0];
      const row = document.createElement('div');
      row.className = 'msg-row ' + (msg.from_me ? 'sent' : 'recv');
      row.dataset.msgId = String(msg.msg_id);
      row.dataset.ts = String(msg.timestamp_ms || 0);
      row.dataset.groupIds = group.map(m => String(m.msg_id)).join(' ');

      const bubble = document.createElement('div');
      bubble.className = 'msg-bubble has-media';

      if (!msg.from_me && msg.sender) {
        bubble.appendChild(createSenderEl(msg.sender));
      }

      bubble.appendChild(renderMediaGrid(group));

      const captionMsg = group.find(m => m.text_body && m.text_body.trim());
      if (captionMsg) {
        const cap = document.createElement('div');
        cap.className = 'msg-caption';
        renderCollapsibleMessageText(cap, captionMsg.text_body, captionMsg.msg_id, captionMsg.mentions);
        bubble.appendChild(cap);
      }

      const meta = document.createElement('div');
      meta.className = 'msg-meta';
      if (msg.from_me) {
        const badge = document.createElement('span');
        badge.className = 'direction-badge sent';
        badge.textContent = 'You';
        meta.appendChild(badge);
      }
      const isGroupEdited = unit.members.some(m => m.is_edited);
      if (isGroupEdited) {
        const editedSpan = document.createElement('span');
        editedSpan.className = 'msg-edited';
        editedSpan.textContent = 'Edited';
        const editedMember = unit.members.find(m => m.edited_ts || m.original_text_body);
        if (editedMember && editedMember.edited_ts) {
          editedSpan.title = 'Edited: ' + fmtTime(editedMember.edited_ts);
        }
        editedSpan.addEventListener('click', e => {
          e.stopPropagation();
          showMsgDetails(
            null,
            editedSpan,
            null,
            null,
            editedMember ? { original_text: editedMember.original_text_body || null, edited_ts: editedMember.edited_ts || null, mentions: editedMember.mentions || null } : null
          );
        });
        meta.appendChild(editedSpan);
      }
      const isGroupPinned = unit.members.some(m => m.is_pinned) || Boolean(
        msg.is_pinned || (_chatPins && _chatPins.some(p => String(p.id) === String(msg.msg_id)))
      );
      if (isGroupPinned) {
        meta.appendChild(createMetaPinIcon());
      }
      const isGroupStarred = unit.members.some(m => m.is_starred) || Boolean(msg.is_starred);
      if (isGroupStarred) {
        meta.appendChild(createMetaStarIcon());
      }
      meta.appendChild(document.createTextNode(fmtTime(msg.timestamp_ms)));
      bubble.appendChild(meta);

      row.appendChild(bubble);
      return row;
    }

    const msg = unit.msg;
    if (currentChat && currentChat.type === 'contact' && msg.media_type === 'service') {
      if (
        !msg.text_body ||
        !msg.text_body.trim() ||
        msg.text_body.includes('subject') ||
        msg.text_body.includes('description') ||
        msg.text_body.includes('deleted')
      ) {
        msg.media_type = 'deleted';
        msg.text_body = msg.from_me ? 'You deleted this message' : 'This message was deleted';
      }
    }
    if (msg.media_type === 'service') {
      if (!msg.text_body || !msg.text_body.trim()) {
        return document.createDocumentFragment();
      }
      const row = document.createElement('div');
      row.className = 'msg-row service';
      row.dataset.msgId = String(msg.msg_id);
      row.dataset.ts = String(msg.timestamp_ms || 0);

      const bubble = document.createElement('div');
      bubble.className = 'msg-service-bubble';
      bubble.textContent = msg.text_body;
      if (msg.timestamp_ms) {
        bubble.title = fmtTime(msg.timestamp_ms);
      }

      row.appendChild(bubble);
      return row;
    }

    const row = document.createElement('div');
    row.className = 'msg-row ' + (msg.from_me ? 'sent' : 'recv');
    row.dataset.msgId = String(msg.msg_id);
    row.dataset.ts = String(msg.timestamp_ms || 0);

    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble';

    if (msg.sender && !msg.from_me && currentChat && currentChat.type === 'group') {
      bubble.appendChild(createSenderEl(msg.sender));
    }

    if ((msg.quoted_text || msg.quoted_stanza_id || msg.quoted_msg_id) && msg.media_type !== 'deleted') {
      const quote = document.createElement('div');
      const qs = msg.quoted_sender;
      const qColorCls = (currentChat && currentChat.type === 'group') ? getSenderColorClass(qs) : '';
      quote.className = qColorCls ? ('msg-quote ' + qColorCls) : 'msg-quote';
      const qContent = document.createElement('div');
      qContent.className = 'msg-quote-content';
      const qSender = document.createElement('div');
      qSender.className = 'msg-quote-sender';
      qSender.textContent = qs || (currentChat && currentChat.type === 'contact' ? currentChat.display_name : '');
      const qText = document.createElement('div');
      qText.className = 'msg-quote-text';
      qText.textContent = resolveMentionsPlainText(msg.quoted_text, msg.mentions) || (msg.quoted_msg_id ? 'Quoted message' : '');
      qContent.appendChild(qSender);
      qContent.appendChild(qText);
      quote.appendChild(qContent);

      if (msg.quoted_has_thumb && (msg.quoted_msg_id || msg.msg_id)) {
        const qThumb = document.createElement('img');
        qThumb.className = 'msg-quote-thumb';
        qThumb.alt = '';
        qThumb.loading = 'lazy';
        qThumb.decoding = 'async';
        const primaryId = msg.quoted_msg_id || msg.msg_id;
        const fallbackId = (msg.quoted_msg_id && msg.msg_id && msg.quoted_msg_id !== msg.msg_id) ? msg.msg_id : null;
        qThumb.src = '/api/thumbnail/' + primaryId;
        qThumb.onerror = () => {
          if (fallbackId && qThumb.src.indexOf('/' + fallbackId) === -1) {
            qThumb.src = '/api/thumbnail/' + fallbackId;
          } else {
            qThumb.remove();
          }
        };
        quote.appendChild(qThumb);
      }

      if (msg.quoted_msg_id || msg.quoted_ts) {
        quote.style.cursor = 'pointer';
        quote.title = 'Jump to original message';
        quote.addEventListener('click', async (e) => {
          e.stopPropagation();
          if (msg.quoted_msg_id) {
            expandMessageRowById(msg.quoted_msg_id, rowForMsgId(msg.quoted_msg_id));
            await jumpToMessage({ msg_id: msg.quoted_msg_id, timestamp_ms: msg.quoted_ts }, 'default');
          } else {
            const targetNear = nearestMsgToTs(msg.quoted_ts);
            if (targetNear) {
              expandMessageRowById(targetNear.msg_id, rowForMsgId(targetNear.msg_id));
            }
            const row = await jumpToTimestamp(msg.quoted_ts, null, true, 'default');
            if (row && row.dataset.msgId) {
              expandMessageRowById(row.dataset.msgId, row);
            }
          }
        });
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
    const isCallEvent = msg.media_type === 'call' && !!msg.call;
    const showInfo = isCallEvent ||
                     (msg.from_me && !msg.is_preserved && !!msg.msg_id && msg.media_type !== 'deleted') ||
                     (!!msg.archive_path && msg.media_type !== 'deleted') ||
                     !!msg.is_preserved ||
                     (msg.is_edited && !!msg.original_text_body);
    if (showInfo) {
      const infoBtn = document.createElement('button');
      infoBtn.type = 'button';
      infoBtn.className = 'msg-info-btn';
      infoBtn.title = isCallEvent ? 'Call details' : 'Message details';
      infoBtn.setAttribute('aria-label', isCallEvent ? 'Call details' : 'Message details');
      infoBtn.textContent = 'ⓘ';
      infoBtn.addEventListener('click', e => {
        e.stopPropagation();
        if (isCallEvent) {
          showCallDetails(msg, infoBtn);
          return;
        }
        showMsgDetails(
          (msg.from_me && !msg.is_preserved) ? msg.msg_id : null,
          infoBtn,
          msg.archive_path || null,
          msg.is_preserved ? { reason: msg.preservation_reason, at: msg.preserved_at, platform: msg.source_platform || null } : null,
          msg.is_edited ? { original_text: msg.original_text_body || null, edited_ts: msg.edited_ts || null, mentions: msg.mentions || null } : null
        );
      });
      meta.appendChild(infoBtn);
    }
    if (msg.is_preserved) {
      const presSpan = document.createElement('span');
      presSpan.className = 'msg-preserved';
      presSpan.textContent = 'Preserved';
      const reasonLabel = msg.preservation_reason === 'revoked_by_sender'
        ? 'Revoked by sender'
        : (msg.preservation_reason === 'chat_deleted'
          ? 'Chat deleted on device'
          : 'Deleted on device');
      const presTs = typeof msg.preserved_at === 'number' ? msg.preserved_at * 1000 : Date.parse(msg.preserved_at);
      const presDate = !isNaN(presTs) ? fmtTime(presTs) : (msg.preserved_at || '');
      const backupPrefix = presDate ? `Preserved from backup (${presDate})` : 'Preserved from backup';
      presSpan.title = `${backupPrefix} • ${reasonLabel}`;
      meta.appendChild(presSpan);
    }
    if (msg.is_edited && msg.media_type !== 'deleted') {
      const editedSpan = document.createElement('span');
      editedSpan.className = 'msg-edited';
      editedSpan.textContent = 'Edited';
      if (msg.edited_ts) {
        editedSpan.title = 'Edited: ' + fmtTime(msg.edited_ts);
      }
      editedSpan.addEventListener('click', e => {
        e.stopPropagation();
        showMsgDetails(
          (msg.from_me && !msg.is_preserved) ? msg.msg_id : null,
          editedSpan,
          msg.archive_path || null,
          msg.is_preserved ? { reason: msg.preservation_reason, at: msg.preserved_at, platform: msg.source_platform || null } : null,
          { original_text: msg.original_text_body || null, edited_ts: msg.edited_ts || null, mentions: msg.mentions || null }
        );
      });
      meta.appendChild(editedSpan);
    }
    const isPinned = Boolean(
      msg.is_pinned || (_chatPins && _chatPins.some(p => String(p.id) === String(msg.msg_id)))
    );
    if (isPinned) {
      meta.appendChild(createMetaPinIcon());
    }
    if (msg.is_starred) {
      meta.appendChild(createMetaStarIcon());
    }
    meta.appendChild(document.createTextNode(fmtTime(msg.timestamp_ms)));
    if (msg.media_type === 'deleted') {
      bubble.classList.add('deleted');
      const delEl = document.createElement('div');
      delEl.className = 'msg-deleted-content';
      const icon = document.createElement('span');
      icon.className = 'deleted-icon';
      icon.textContent = '⃠';
      const txt = document.createElement('span');
      txt.className = 'deleted-text';
      txt.textContent = msg.text_body || 'This message was deleted';
      delEl.appendChild(icon);
      delEl.appendChild(document.createTextNode(' '));
      delEl.appendChild(txt);
      bubble.appendChild(delEl);
    } else if (msg.is_view_once && !msg.archive_path) {
      bubble.classList.add('view-once-opened');
      const voEl = document.createElement('div');
      voEl.className = 'msg-view-once-pill';
      const voIcon = document.createElement('span');
      voIcon.className = 'vo-icon';
      voIcon.textContent = '①';
      const voLabel = document.createElement('span');
      voLabel.className = 'vo-label';
      const typeLabel = msg.media_type === 'video' ? 'Video' : (msg.media_type === 'audio' ? 'Voice message' : 'Photo');
      voLabel.textContent = typeLabel + ' · Opened';
      voEl.appendChild(voIcon);
      voEl.appendChild(voLabel);
      bubble.appendChild(voEl);
    } else if (isCallEvent) {
      bubble.classList.add('call-event');
      bubble.setAttribute('role', 'article');
      const callMeta = msg.call;
      const callLabelPrefix = callMeta.is_group ? 'Group ' : '';
      const callKindLabel = callLabelPrefix + (callMeta.video ? 'video call' : 'voice call');
      const outcomeLabel = _formatCallOutcomeText(
        callMeta.call_type || (msg.from_me ? 'outgoing' : 'incoming'),
        callMeta.outcome || 'unanswered',
        callMeta.duration_sec || 0
      );
      bubble.setAttribute('aria-label', `${callKindLabel}, ${outcomeLabel}, ${fmtTime(msg.timestamp_ms)}`);
      bubble.appendChild(renderCallCard(msg));
    } else if (msg.media_type === 'poll') {
      bubble.appendChild(renderPoll(msg));
    } else if (msg.media_type === 'location' || msg.media_type === 'live_location') {
      bubble.classList.add('has-media');
      bubble.appendChild(renderLocation(msg));
    } else if (msg.media_type === 'text' || !msg.archive_path) {
      if (!msg.archive_path && msg.media_type !== 'text' && msg.media_type !== 'link') {
        bubble.classList.add('has-media');
        if (msg.media_type === 'audio' && isVoiceNote(msg) && (msg.waveform || msg.audio_waveform)) {
          bubble.appendChild(renderVoiceNoteBubble(msg));
        } else {
          bubble.appendChild(renderMissingMedia(msg));
        }
        if (msg.text_body && msg.media_type !== 'vcard') {
          const cap = document.createElement('div');
          cap.className = 'msg-caption';
          renderCollapsibleMessageText(cap, msg.text_body, msg.msg_id, msg.mentions);
          bubble.appendChild(cap);
        }
      } else {
        const txt = document.createElement('div');
        txt.className = 'msg-text';
        renderCollapsibleMessageText(txt, msg.text_body, msg.msg_id, msg.mentions);
        bubble.appendChild(txt);
        if (msg.link_title || msg.link_url || msg.link_desc) {
          bubble.appendChild(renderLinkPreview(msg));
        }
      }
    } else {
      bubble.classList.add('has-media');
      const mediaEl = renderMedia(msg);
      bubble.appendChild(mediaEl);
      if (msg.text_body && msg.media_type !== 'vcard') {
        const cap = document.createElement('div');
        cap.className = 'msg-caption';
        renderCollapsibleMessageText(cap, msg.text_body, msg.msg_id, msg.mentions);
        bubble.appendChild(cap);
      }
    }

    bubble.appendChild(meta);

    if (msg.reactions && msg.media_type !== 'deleted') {
      const emojis = msg.reactions.split(',').filter(Boolean);
      if (emojis.length) {
        row.classList.add('has-reactions');
        const rxEl = document.createElement('div');
        rxEl.className = 'msg-reactions';
        const distinct = Array.from(new Set(emojis));
        const emojiSpan = document.createElement('span');
        emojiSpan.className = 'rx-emojis';
        emojiSpan.textContent = distinct.slice(0, 3).join(' ');
        rxEl.appendChild(emojiSpan);

        if (emojis.length > 1) {
          const countSpan = document.createElement('span');
          countSpan.className = 'rx-count';
          countSpan.textContent = String(emojis.length);
          rxEl.appendChild(countSpan);
        }

        if (msg.msg_id !== undefined) {
          rxEl.addEventListener('click', e => {
            e.stopPropagation();
            showReactionDetails(msg.msg_id, rxEl);
          });
        }
        bubble.appendChild(rxEl);
      }
    }

    row.appendChild(bubble);
    return row;
  }

  function extractDomain(url) {
    if (!url) return '';
    try {
      const u = new URL(url);
      return u.hostname.replace(/^www\./, '').toUpperCase();
    } catch {
      return '';
    }
  }

  function renderLinkPreview(msg) {
    const card = document.createElement('div');
    const isBanner = Number(msg.link_preview_type) === 1;
    card.className = 'msg-link-preview' + (isBanner ? ' banner' : ' compact');

    const anchor = document.createElement('a');
    anchor.className = 'link-preview-anchor';
    anchor.href = msg.link_url || '#';
    anchor.target = '_blank';
    anchor.rel = 'noopener noreferrer';
    const domain = extractDomain(msg.link_url);
    const ariaTitle = msg.link_title || domain || 'Web page';
    anchor.setAttribute('aria-label', 'Link preview: ' + ariaTitle);

    if (msg.has_thumbnail && msg.msg_id) {
      const mediaBox = document.createElement('div');
      mediaBox.className = 'link-preview-media';
      const img = document.createElement('img');
      img.className = 'link-preview-thumb';
      img.src = '/api/thumbnail/' + msg.msg_id;
      img.alt = '';
      img.loading = 'lazy';
      img.onerror = () => {
        mediaBox.style.display = 'none';
      };
      mediaBox.appendChild(img);
      anchor.appendChild(mediaBox);
    }

    const body = document.createElement('div');
    body.className = 'link-preview-body';

    if (domain) {
      const domEl = document.createElement('div');
      domEl.className = 'link-preview-domain';
      domEl.textContent = domain;
      body.appendChild(domEl);
    }

    if (msg.link_title) {
      const titleEl = document.createElement('div');
      titleEl.className = 'link-preview-title';
      titleEl.textContent = msg.link_title;
      body.appendChild(titleEl);
    }

    if (msg.link_desc) {
      const descEl = document.createElement('div');
      descEl.className = 'link-preview-desc';
      descEl.textContent = msg.link_desc;
      body.appendChild(descEl);
    }

    anchor.appendChild(body);
    card.appendChild(anchor);
    return card;
  }

  function renderMissingMedia(msg) {
    const videoNote = msg.media_type === 'video' && isVideoNote(msg);
    const card = document.createElement('div');
    card.className = videoNote ? 'media-missing-card is-video-note' : 'media-missing-card';

    let ratio = msg.aspect_ratio || 0;
    if (!ratio && msg.media_width > 0 && msg.media_height > 0) {
      ratio = msg.media_width / msg.media_height;
    }
    if (videoNote) {
      card.style.aspectRatio = '1 / 1';
    } else if (ratio > 0) {
      card.style.aspectRatio = ratio.toFixed(3);
    } else if (msg.media_type === 'video' || msg.media_type === 'gif') {
      card.style.aspectRatio = '16 / 9';
    } else if (msg.media_type === 'image') {
      card.style.aspectRatio = '4 / 3';
    }

    if (msg.has_thumbnail) {
      const img = document.createElement('img');
      img.className = 'media-blurred-bg';
      img.src = '/api/thumbnail/' + encodeURIComponent(msg.msg_id);
      img.loading = 'lazy';
      img.alt = '';
      img.onerror = () => {
        img.style.display = 'none';
      };
      card.appendChild(img);
    }

    const overlay = document.createElement('div');
    overlay.className = 'media-missing-overlay';

    const isVn = msg.media_type === 'audio' && isVoiceNote(msg);
    const badge = document.createElement('div');
    badge.className = 'media-missing-badge';
    const icon = videoNote ? '🎥' :
                 isVn ? '🎤' :
                 msg.media_type === 'video' ? '🎥' :
                 msg.media_type === 'audio' ? '🎵' :
                 msg.media_type === 'sticker' ? '🩹' :
                 msg.media_type === 'gif' ? '🎞️' :
                 msg.media_type === 'vcard' ? '📇' :
                 msg.media_type === 'document' ? '📄' : '🖼️';
    badge.textContent = icon;
    overlay.appendChild(badge);

    const info = document.createElement('div');
    info.className = 'media-missing-info';

    const label = document.createElement('div');
    label.className = 'media-missing-label';
    label.textContent = videoNote ? 'Video message' :
                        isVn ? 'Voice message' :
                        msg.media_type === 'video' ? 'Video' :
                        msg.media_type === 'gif' ? 'GIF' :
                        msg.media_type === 'audio' ? 'Audio' :
                        msg.media_type === 'document' ? 'Document' :
                        msg.media_type === 'sticker' ? 'Sticker' : 'Photo';
    info.appendChild(label);

    const sizeStr = _formatBytes(msg.media_size);
    const durStr = (msg.media_duration && msg.media_duration > 0) ? _formatTimer(msg.media_duration) : '';

    const metaParts = [];
    if (sizeStr) metaParts.push(sizeStr);
    if (durStr) metaParts.push(durStr);
    if (metaParts.length > 0) {
      const meta = document.createElement('div');
      meta.className = 'media-missing-meta';
      meta.textContent = metaParts.join(' · ');
      info.appendChild(meta);
    }

    overlay.appendChild(info);
    card.appendChild(overlay);
    return card;
  }

  function _extractMsgAspectRatio(msg) {
    let ratio = Number(msg.aspect_ratio) || 0;
    if (!ratio && msg.media_width > 0 && msg.media_height > 0) {
      ratio = msg.media_width / msg.media_height;
    }
    return ratio;
  }

  function _applyMediaOrientationBox(wrap, rawRatio) {
    wrap.classList.remove('is-portrait', 'is-landscape');
    if (rawRatio > 0 && rawRatio < 1) {
      wrap.classList.add('is-portrait');
      const clamped = Math.max(0.75, Math.min(rawRatio, 0.999));
      wrap.style.aspectRatio = clamped.toFixed(3);
    } else if (rawRatio >= 1) {
      wrap.classList.add('is-landscape');
      const clamped = Math.max(1.0, Math.min(rawRatio, 1.778));
      wrap.style.aspectRatio = clamped.toFixed(3);
    } else {
      wrap.classList.add('is-landscape');
      wrap.style.aspectRatio = '1.333';
    }
  }

  function renderGif(msg) {
    const wrap = document.createElement('div');
    wrap.className = 'msg-media msg-gif';
    wrap.setAttribute('tabindex', '0');
    wrap.setAttribute('aria-label', 'Animated GIF: click to play');

    const ratio = _extractMsgAspectRatio(msg);
    _applyMediaOrientationBox(wrap, ratio);

    const src = '/media/' + msg.archive_path;
    const isImageGif = (msg.archive_path || '').toLowerCase().endsWith('.gif');
    if (isImageGif) {
      const img = document.createElement('img');
      img.src = src;
      img.loading = 'lazy';
      if (ratio <= 0) {
        img.addEventListener('load', () => {
          if (img.naturalWidth > 0 && img.naturalHeight > 0) {
            _applyMediaOrientationBox(wrap, img.naturalWidth / img.naturalHeight);
          }
        }, { once: true });
      }
      img.addEventListener('click', () => showLightbox(msg));
      wrap.appendChild(img);
      return wrap;
    }

    const vid = document.createElement('video');
    vid.className = 'msg-gif-video';
    vid.preload = 'metadata';
    vid.muted = true;
    vid.playsInline = true;
    vid.loop = true;
    vid.src = src;

    const probe = document.createElement('video');
    probe.src = src;
    probe.muted = true;
    probe.preload = 'metadata';
    probe.addEventListener('loadeddata', () => {
      if (ratio <= 0 && probe.videoWidth > 0 && probe.videoHeight > 0) {
        _applyMediaOrientationBox(wrap, probe.videoWidth / probe.videoHeight);
      }
      const canvas = document.createElement('canvas');
      canvas.width = probe.videoWidth;
      canvas.height = probe.videoHeight;
      canvas.getContext('2d').drawImage(probe, 0, 0);
      vid.poster = canvas.toDataURL('image/jpeg', 0.8);
      probe.src = '';
      const scroll = document.getElementById('message-scroll');
      if (scroll && isUserAtBottom) {
        suppressScroll = true;
        scroll.scrollTop = scroll.scrollHeight;
        suppressScroll = false;
      }
    }, { once: true });
    probe.addEventListener('error', () => { probe.src = ''; }, { once: true });

    const badgeWrap = document.createElement('div');
    badgeWrap.className = 'msg-gif-badge-wrap';
    badgeWrap.setAttribute('aria-hidden', 'true');
    const badge = document.createElement('div');
    badge.className = 'msg-gif-badge';
    badge.textContent = 'GIF';
    badgeWrap.appendChild(badge);

    function toggleGif() {
      if (vid.paused) {
        vid.play().then(() => {
          wrap.classList.add('is-playing');
          wrap.setAttribute('aria-label', 'Animated GIF: click to pause');
        }).catch(() => {});
      } else {
        vid.pause();
        wrap.classList.remove('is-playing');
        wrap.setAttribute('aria-label', 'Animated GIF: click to play');
      }
    }

    wrap.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleGif();
    });

    wrap.addEventListener('keydown', (e) => {
      if (e.key === ' ' || e.key === 'Enter') {
        e.preventDefault();
        e.stopPropagation();
        toggleGif();
      }
    });

    if (gifViewportObserver) {
      gifViewportObserver.observe(vid);
    }

    wrap.appendChild(vid);
    wrap.appendChild(badgeWrap);

    if (msg.is_view_once) {
      const voBadge = document.createElement('span');
      voBadge.className = 'msg-view-once-badge';
      voBadge.innerHTML = '<span class="vo-icon">①</span> View Once';
      wrap.appendChild(voBadge);
    }

    return wrap;
  }

  const VIDEO_NOTE_RING_CIRCUMFERENCE = 2 * Math.PI * 48;

  function renderVideoNote(msg) {
    const wrap = document.createElement('div');
    wrap.className = 'msg-media msg-video-note';
    wrap.setAttribute('tabindex', '0');

    const totalSec = msg.media_duration && msg.media_duration > 0 ? msg.media_duration : 0;
    const baseAriaLabel = totalSec > 0
      ? `Video message, ${_formatTimer(totalSec)}`
      : 'Video message';
    wrap.setAttribute('aria-label', `${baseAriaLabel}, click to play`);

    const src = '/media/' + msg.archive_path;

    const vid = document.createElement('video');
    vid.className = 'video-note-video';
    vid.playsInline = true;
    vid.preload = 'metadata';
    vid.src = src;

    vid.addEventListener('loadeddata', () => {
      if (!vid.dataset.posterCaptured && vid.videoWidth > 0 && vid.videoHeight > 0) {
        vid.dataset.posterCaptured = '1';
        try {
          const canvas = document.createElement('canvas');
          canvas.width = vid.videoWidth;
          canvas.height = vid.videoHeight;
          canvas.getContext('2d').drawImage(vid, 0, 0);
          vid.poster = canvas.toDataURL('image/jpeg', 0.8);
        } catch (_e) {
          // Ignore canvas errors if frame capture fails
        }
      }
      if (!totalSec && Number.isFinite(vid.duration) && vid.duration > 0) {
        durText.textContent = _formatTimer(Math.round(vid.duration));
      }
      const scroll = document.getElementById('message-scroll');
      if (scroll && isUserAtBottom) {
        suppressScroll = true;
        scroll.scrollTop = scroll.scrollHeight;
        suppressScroll = false;
      }
    }, { once: true });

    const svgNS = 'http://www.w3.org/2000/svg';
    const ringSvg = document.createElementNS(svgNS, 'svg');
    ringSvg.setAttribute('class', 'video-note-ring');
    ringSvg.setAttribute('viewBox', '0 0 100 100');
    ringSvg.setAttribute('aria-hidden', 'true');

    const trackCircle = document.createElementNS(svgNS, 'circle');
    trackCircle.setAttribute('class', 'video-note-ring-track');
    trackCircle.setAttribute('cx', '50');
    trackCircle.setAttribute('cy', '50');
    trackCircle.setAttribute('r', '48');

    const progCircle = document.createElementNS(svgNS, 'circle');
    progCircle.setAttribute('class', 'video-note-ring-progress');
    progCircle.setAttribute('cx', '50');
    progCircle.setAttribute('cy', '50');
    progCircle.setAttribute('r', '48');
    progCircle.style.strokeDasharray = VIDEO_NOTE_RING_CIRCUMFERENCE.toFixed(2);
    progCircle.style.strokeDashoffset = VIDEO_NOTE_RING_CIRCUMFERENCE.toFixed(2);

    ringSvg.appendChild(trackCircle);
    ringSvg.appendChild(progCircle);

    const playWrap = document.createElement('div');
    playWrap.className = 'video-note-play-wrap';
    playWrap.setAttribute('aria-hidden', 'true');
    const playBtn = document.createElement('div');
    playBtn.className = 'video-note-play-btn';
    playBtn.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><polygon points="7,4 20,12 7,20"/></svg>';
    playWrap.appendChild(playBtn);

    const durBadge = document.createElement('div');
    durBadge.className = 'video-note-duration';
    durBadge.setAttribute('aria-hidden', 'true');
    const durIcon = document.createElement('span');
    durIcon.className = 'video-note-cam-icon';
    durIcon.innerHTML = '<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><path d="M17 10.5V7a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-3.5l4 4v-11l-4 4z"/></svg>';
    const durText = document.createElement('span');
    durText.textContent = totalSec > 0 ? _formatTimer(totalSec) : '0:00';
    durBadge.appendChild(durIcon);
    durBadge.appendChild(durText);

    const actions = document.createElement('div');
    actions.className = 'video-note-actions';

    const muteBtn = document.createElement('button');
    muteBtn.type = 'button';
    muteBtn.className = 'video-note-action-btn';
    muteBtn.title = 'Mute';
    muteBtn.setAttribute('aria-label', 'Mute video message');
    function syncMuteIcon() {
      if (vid.muted) {
        muteBtn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>';
        muteBtn.title = 'Unmute';
        muteBtn.setAttribute('aria-label', 'Unmute video message');
      } else {
        muteBtn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>';
        muteBtn.title = 'Mute';
        muteBtn.setAttribute('aria-label', 'Mute video message');
      }
    }
    syncMuteIcon();
    muteBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      vid.muted = !vid.muted;
      syncMuteIcon();
    });

    const enlargeBtn = document.createElement('button');
    enlargeBtn.type = 'button';
    enlargeBtn.className = 'video-note-action-btn';

    function syncEnlargeIcon() {
      const isEnlarged = wrap.classList.contains('is-enlarged');
      if (isEnlarged) {
        enlargeBtn.title = 'Shrink';
        enlargeBtn.setAttribute('aria-label', 'Shrink video message');
        enlargeBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/><line x1="14" y1="10" x2="21" y2="3"/><line x1="3" y1="21" x2="10" y2="14"/></svg>';
      } else {
        enlargeBtn.title = 'Enlarge';
        enlargeBtn.setAttribute('aria-label', 'Enlarge video message');
        enlargeBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>';
      }
    }
    syncEnlargeIcon();

    enlargeBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const isEnlarged = wrap.classList.toggle('is-enlarged');
      const bubble = wrap.closest('.msg-bubble');
      if (bubble) {
        bubble.classList.toggle('has-video-note-enlarged', isEnlarged);
      }
      syncEnlargeIcon();
      const scroll = document.getElementById('message-scroll');
      if (scroll && isUserAtBottom) {
        requestAnimationFrame(() => {
          suppressScroll = true;
          scroll.scrollTop = scroll.scrollHeight;
          suppressScroll = false;
        });
      }
    });

    actions.appendChild(muteBtn);
    actions.appendChild(enlargeBtn);

    let rafId = null;

    function updateRingAndTimer() {
      const dur = Number.isFinite(vid.duration) && vid.duration > 0 ? vid.duration : totalSec;
      if (dur > 0) {
        const progress = Math.min(1, Math.max(0, vid.currentTime / dur));
        progCircle.style.strokeDashoffset = (VIDEO_NOTE_RING_CIRCUMFERENCE * (1 - progress)).toFixed(2);
        const remaining = Math.max(0, Math.ceil(dur - vid.currentTime));
        durText.textContent = _formatTimer(remaining);
      }
      if (!vid.paused && !vid.ended) {
        rafId = requestAnimationFrame(updateRingAndTimer);
      }
    }

    function pauseVideoNote() {
      if (rafId !== null) {
        cancelAnimationFrame(rafId);
        rafId = null;
      }
      if (!vid.paused) {
        vid.pause();
      }
      wrap.classList.remove('is-playing');
      wrap.setAttribute('aria-label', `${baseAriaLabel}, click to play`);
      if (activeVideoNotePause === pauseVideoNote) {
        activeVideoNotePause = null;
      }
    }

    function playVideoNote() {
      stopActiveVoiceNote();
      if (activeVideoNotePause && activeVideoNotePause !== pauseVideoNote) {
        activeVideoNotePause();
      }
      activeVideoNotePause = pauseVideoNote;
      vid.play().then(() => {
        wrap.classList.add('is-playing');
        wrap.setAttribute('aria-label', `${baseAriaLabel}, click to pause`);
        rafId = requestAnimationFrame(updateRingAndTimer);
      }).catch(() => {
        if (activeVideoNotePause === pauseVideoNote) {
          activeVideoNotePause = null;
        }
      });
    }

    function toggleVideoNote() {
      if (vid.paused || vid.ended) {
        playVideoNote();
      } else {
        pauseVideoNote();
      }
    }

    vid.addEventListener('ended', () => {
      pauseVideoNote();
      vid.currentTime = 0;
      progCircle.style.strokeDashoffset = VIDEO_NOTE_RING_CIRCUMFERENCE.toFixed(2);
      const dur = Number.isFinite(vid.duration) && vid.duration > 0 ? Math.round(vid.duration) : totalSec;
      durText.textContent = dur > 0 ? _formatTimer(dur) : '0:00';
    });

    wrap._pauseVideoNote = pauseVideoNote;
    if (videoNoteViewportObserver) {
      videoNoteViewportObserver.observe(wrap);
    }

    wrap.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleVideoNote();
    });

    wrap.addEventListener('keydown', (e) => {
      if (e.key === ' ' || e.key === 'Enter') {
        e.preventDefault();
        e.stopPropagation();
        toggleVideoNote();
      }
    });

    wrap.appendChild(vid);
    wrap.appendChild(ringSvg);
    wrap.appendChild(playWrap);
    wrap.appendChild(durBadge);
    wrap.appendChild(actions);

    if (msg.is_view_once) {
      const voBadge = document.createElement('span');
      voBadge.className = 'msg-view-once-badge';
      voBadge.innerHTML = '<span class="vo-icon">①</span> View Once';
      wrap.appendChild(voBadge);
    }

    return wrap;
  }

  const VOICE_PLAY_SVG = '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" style="transform: translateX(1px); display: block;"><polygon points="6,4 20,12 6,20"/></svg>';
  const VOICE_PAUSE_SVG = '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="4" width="4" height="16" rx="1"/><rect x="15" y="4" width="4" height="16" rx="1"/></svg>';

  const waveformCache = new Map(); // src -> number[]
  const decodeQueue = [];
  let activeDecodeCount = 0;
  const MAX_CONCURRENT_DECODES = 3;
  let sharedAudioContext = null;

  function getSharedAudioContext() {
    if (!sharedAudioContext) {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (AudioCtx) {
        sharedAudioContext = new AudioCtx();
      }
    }
    if (sharedAudioContext && sharedAudioContext.state === 'suspended') {
      sharedAudioContext.resume().catch(() => {});
    }
    return sharedAudioContext;
  }

  function extractWaveformFromBuffer(audioBuffer, numBars = 64) {
    const pcm = audioBuffer.getChannelData(0);
    const blockSize = Math.floor(pcm.length / numBars);
    if (blockSize <= 0) {
      return new Array(numBars).fill(15);
    }
    const peaks = new Array(numBars);
    let maxPeak = 0;
    for (let i = 0; i < numBars; i++) {
      const start = i * blockSize;
      const end = i === numBars - 1 ? pcm.length : start + blockSize;
      let peak = 0;
      for (let j = start; j < end; j++) {
        const val = Math.abs(pcm[j]);
        if (val > peak) peak = val;
      }
      peaks[i] = peak;
      if (peak > maxPeak) maxPeak = peak;
    }
    const normalized = new Array(numBars);
    const factor = maxPeak > 0 ? (100 / maxPeak) : 0;
    for (let i = 0; i < numBars; i++) {
      normalized[i] = Math.max(10, Math.min(100, Math.round(peaks[i] * factor)));
    }
    return normalized;
  }

  function fetchAndDecodeWaveform(src) {
    if (waveformCache.has(src)) {
      return Promise.resolve(waveformCache.get(src));
    }
    return new Promise((resolve) => {
      decodeQueue.push({ src, resolve });
      processDecodeQueue();
    });
  }

  function processDecodeQueue() {
    if (activeDecodeCount >= MAX_CONCURRENT_DECODES || decodeQueue.length === 0) {
      return;
    }
    const task = decodeQueue.shift();
    if (waveformCache.has(task.src)) {
      task.resolve(waveformCache.get(task.src));
      processDecodeQueue();
      return;
    }
    activeDecodeCount++;
    fetch(task.src)
      .then((res) => {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.arrayBuffer();
      })
      .then((buf) => {
        const ctx = getSharedAudioContext();
        if (!ctx) throw new Error('AudioContext unavailable');
        return ctx.decodeAudioData(buf);
      })
      .then((audioBuf) => {
        const wave = extractWaveformFromBuffer(audioBuf, 64);
        waveformCache.set(task.src, wave);
        task.resolve(wave);
      })
      .catch((err) => {
        console.debug('Waveform decode fallback:', task.src, err.message);
        task.resolve(null);
      })
      .finally(() => {
        activeDecodeCount--;
        processDecodeQueue();
      });
  }

  let activeVoiceNoteStop = null;

  function stopActiveVoiceNote() {
    if (activeVoiceNoteStop) {
      activeVoiceNoteStop();
      activeVoiceNoteStop = null;
    }
  }

  let voiceNoteViewportObserver = null;
  if (typeof IntersectionObserver !== 'undefined') {
    voiceNoteViewportObserver = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          const wrap = entry.target;
          if (wrap._loadWaveform) {
            wrap._loadWaveform();
            delete wrap._loadWaveform;
          }
          voiceNoteViewportObserver.unobserve(wrap);
        }
      });
    }, { rootMargin: '250px 0px' });
  }

  function _formatAudioTime(sec) {
    const s = Math.max(0, Math.floor(Number(sec) || 0));
    const m = Math.floor(s / 60);
    const rem = s % 60;
    return `${m}:${rem.toString().padStart(2, '0')}`;
  }

  function renderVoiceNoteBubble(msg) {
    const wrap = document.createElement('div');
    wrap.className = 'msg-media audio voice-player';
    wrap.setAttribute('tabindex', '0');
    wrap.setAttribute('role', 'region');

    const hasAudioFile = Boolean(msg.archive_path && !msg.is_missing);
    let totalSec = (msg.media_duration && msg.media_duration > 0) ? Number(msg.media_duration) : 0;
    const baseAriaLabel = totalSec > 0
      ? `Voice message, ${_formatAudioTime(totalSec)}`
      : 'Voice message';
    wrap.setAttribute('aria-label', baseAriaLabel);

    if (!hasAudioFile) {
      wrap.classList.add('is-unavailable');
    }

    const audio = document.createElement('audio');
    audio.preload = 'metadata';
    if (hasAudioFile) {
      audio.src = '/media/' + msg.archive_path;
    }

    let savedRate = parseFloat(sessionStorage.getItem('wa_voice_playback_rate') || '1') || 1;
    if (![1, 1.2, 1.5, 2].includes(savedRate)) savedRate = 1;
    audio.playbackRate = savedRate;

    const mainRow = document.createElement('div');
    mainRow.className = 'voice-main-row';

    const playBtn = document.createElement('button');
    playBtn.type = 'button';
    playBtn.className = 'voice-play-btn';
    playBtn.setAttribute('aria-label', 'Play voice message');
    playBtn.innerHTML = VOICE_PLAY_SVG;
    if (!hasAudioFile) {
      playBtn.disabled = true;
    }

    const waveformContainer = document.createElement('div');
    waveformContainer.className = 'voice-waveform-container';
    waveformContainer.setAttribute('role', 'slider');
    waveformContainer.setAttribute('aria-label', 'Voice message timeline');
    waveformContainer.setAttribute('aria-valuemin', '0');
    waveformContainer.setAttribute('aria-valuemax', '100');
    waveformContainer.setAttribute('aria-valuenow', '0');
    waveformContainer.setAttribute('tabindex', '0');

    const waveformEl = document.createElement('div');
    waveformEl.className = 'voice-waveform';
    const barEls = [];
    for (let i = 0; i < 64; i++) {
      const bar = document.createElement('div');
      bar.className = 'voice-bar';
      bar.style.height = '4px'; // Honest flat baseline
      waveformEl.appendChild(bar);
      barEls.push(bar);
    }
    waveformContainer.appendChild(waveformEl);

    mainRow.appendChild(playBtn);
    mainRow.appendChild(waveformContainer);

    const subRow = document.createElement('div');
    subRow.className = 'voice-sub-row';

    const timeText = document.createElement('span');
    timeText.className = 'voice-time';
    timeText.textContent = !hasAudioFile
      ? (totalSec > 0 ? `${_formatAudioTime(totalSec)} (unavailable)` : 'Unavailable')
      : (totalSec > 0 ? _formatAudioTime(totalSec) : '0:00');

    const speedPill = document.createElement('button');
    speedPill.type = 'button';
    speedPill.className = 'voice-speed-pill';
    speedPill.textContent = `${savedRate}x`;
    speedPill.setAttribute('aria-label', `Playback speed ${savedRate}x`);

    subRow.appendChild(timeText);
    subRow.appendChild(speedPill);

    wrap.appendChild(mainRow);
    wrap.appendChild(subRow);

    function applyWaveformHeights(arr) {
      if (!Array.isArray(arr) || arr.length === 0) return;
      for (let i = 0; i < 64; i++) {
        const val = i < arr.length ? arr[i] : 15;
        const h = Math.max(4, Math.round((Math.min(100, Math.max(0, val)) / 100) * 26) + 2);
        barEls[i].style.height = `${h}px`;
      }
      wrap.classList.remove('is-loading');
    }

    const precomputedWave = msg.waveform || msg.audio_waveform;
    if (Array.isArray(precomputedWave) && precomputedWave.length > 0) {
      applyWaveformHeights(precomputedWave);
    } else if (hasAudioFile) {
      const srcUrl = '/media/' + msg.archive_path;
      if (waveformCache.has(srcUrl)) {
        applyWaveformHeights(waveformCache.get(srcUrl));
      } else {
        wrap.classList.add('is-loading');
        wrap._loadWaveform = () => {
          fetchAndDecodeWaveform(srcUrl).then((wave) => {
            if (wave) {
              applyWaveformHeights(wave);
            } else {
              wrap.classList.remove('is-loading');
            }
          }).catch(() => {
            wrap.classList.remove('is-loading');
          });
        };
        if (voiceNoteViewportObserver) {
          voiceNoteViewportObserver.observe(wrap);
        } else {
          wrap._loadWaveform();
          delete wrap._loadWaveform;
        }
      }
    }

    function updateProgress(currentTime, duration) {
      const dur = duration > 0 ? duration : (totalSec > 0 ? totalSec : 0);
      const progress = dur > 0 ? Math.min(1, Math.max(0, currentTime / dur)) : 0;
      const playedIndex = Math.floor(progress * 64);
      for (let i = 0; i < 64; i++) {
        if (i < playedIndex) {
          barEls[i].classList.add('is-played');
        } else {
          barEls[i].classList.remove('is-played');
        }
      }
      waveformContainer.setAttribute('aria-valuenow', Math.round(progress * 100).toString());

      if (wrap.classList.contains('is-playing') || currentTime > 0) {
        timeText.textContent = dur > 0
          ? `${_formatAudioTime(currentTime)} / ${_formatAudioTime(dur)}`
          : _formatAudioTime(currentTime);
      } else {
        timeText.textContent = dur > 0 ? _formatAudioTime(dur) : '0:00';
      }
    }

    function pausePlayback() {
      audio.pause();
      wrap.classList.remove('is-playing');
      playBtn.innerHTML = VOICE_PLAY_SVG;
      playBtn.setAttribute('aria-label', 'Play voice message');
      if (activeVoiceNoteStop === pausePlayback) {
        activeVoiceNoteStop = null;
      }
    }

    function startPlayback() {
      if (!hasAudioFile) return;

      if (wrap._loadWaveform) {
        wrap._loadWaveform();
        delete wrap._loadWaveform;
      }

      if (activeVideoNotePause) {
        activeVideoNotePause();
      }
      if (activeVoiceNoteStop && activeVoiceNoteStop !== pausePlayback) {
        activeVoiceNoteStop();
      }
      activeVoiceNoteStop = pausePlayback;

      if (currentGalleryAudio && !currentGalleryAudio.paused) {
        currentGalleryAudio.pause();
        if (currentGalleryAudioCell) currentGalleryAudioCell.classList.remove('is-playing');
      }

      audio.play().then(() => {
        wrap.classList.add('is-playing');
        playBtn.innerHTML = VOICE_PAUSE_SVG;
        playBtn.setAttribute('aria-label', 'Pause voice message');
      }).catch((err) => {
        console.warn('Voice playback failed:', err);
        pausePlayback();
      });
    }

    function togglePlayback() {
      if (audio.paused || audio.ended) {
        startPlayback();
      } else {
        pausePlayback();
      }
    }

    playBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      togglePlayback();
    });

    audio.addEventListener('loadedmetadata', () => {
      if (Number.isFinite(audio.duration) && audio.duration > 0) {
        totalSec = audio.duration;
        updateProgress(0, totalSec);
      }
    });

    audio.addEventListener('timeupdate', () => {
      if (!isScrubbing) {
        updateProgress(audio.currentTime, audio.duration || totalSec);
      }
    });

    audio.addEventListener('ended', () => {
      pausePlayback();
      updateProgress(0, audio.duration || totalSec);
    });

    audio.addEventListener('error', () => {
      wrap.classList.add('is-unavailable');
      playBtn.disabled = true;
      timeText.textContent = 'Unavailable';
      pausePlayback();
    });

    let isScrubbing = false;

    function seekFromEvent(e) {
      const rect = waveformContainer.getBoundingClientRect();
      if (rect.width <= 0) return;
      const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      const dur = (Number.isFinite(audio.duration) && audio.duration > 0) ? audio.duration : totalSec;
      if (dur > 0) {
        const targetTime = ratio * dur;
        audio.currentTime = targetTime;
        updateProgress(targetTime, dur);
      }
    }

    waveformContainer.addEventListener('pointerdown', (e) => {
      if (!hasAudioFile || wrap.classList.contains('is-unavailable')) return;
      e.preventDefault();
      try {
        waveformContainer.setPointerCapture(e.pointerId);
      } catch (_e) {}
      isScrubbing = true;
      seekFromEvent(e);

      const onPointerMove = (ev) => {
        if (isScrubbing) {
          seekFromEvent(ev);
        }
      };

      const onPointerUp = (ev) => {
        if (isScrubbing) {
          isScrubbing = false;
          seekFromEvent(ev);
          try {
            waveformContainer.releasePointerCapture(ev.pointerId);
          } catch (_err) {}
          waveformContainer.removeEventListener('pointermove', onPointerMove);
          waveformContainer.removeEventListener('pointerup', onPointerUp);
          waveformContainer.removeEventListener('pointercancel', onPointerUp);
        }
      };

      waveformContainer.addEventListener('pointermove', onPointerMove);
      waveformContainer.addEventListener('pointerup', onPointerUp);
      waveformContainer.addEventListener('pointercancel', onPointerUp);
    });

    waveformContainer.addEventListener('keydown', (e) => {
      if (!hasAudioFile || wrap.classList.contains('is-unavailable')) return;
      const dur = (Number.isFinite(audio.duration) && audio.duration > 0) ? audio.duration : totalSec;
      if (!dur) return;
      if (e.key === 'ArrowLeft') {
        e.preventDefault();
        audio.currentTime = Math.max(0, audio.currentTime - 5);
        updateProgress(audio.currentTime, dur);
      } else if (e.key === 'ArrowRight') {
        e.preventDefault();
        audio.currentTime = Math.min(dur, audio.currentTime + 5);
        updateProgress(audio.currentTime, dur);
      } else if (e.key === ' ' || e.key === 'Enter') {
        e.preventDefault();
        togglePlayback();
      }
    });

    speedPill.addEventListener('click', (e) => {
      e.stopPropagation();
      const current = savedRate;
      const nextRate = current === 1 ? 1.2 : (current === 1.2 ? 1.5 : (current === 1.5 ? 2 : 1));
      savedRate = nextRate;
      sessionStorage.setItem('wa_voice_playback_rate', String(nextRate));
      audio.playbackRate = nextRate;
      speedPill.textContent = `${nextRate}x`;
      speedPill.setAttribute('aria-label', `Playback speed ${nextRate}x`);
      window.dispatchEvent(new CustomEvent('wa-voice-speed-change', { detail: { rate: nextRate } }));
    });

    const onGlobalSpeedChange = (e) => {
      if (e.detail && e.detail.rate) {
        savedRate = e.detail.rate;
        audio.playbackRate = savedRate;
        speedPill.textContent = `${savedRate}x`;
        speedPill.setAttribute('aria-label', `Playback speed ${savedRate}x`);
      }
    };
    window.addEventListener('wa-voice-speed-change', onGlobalSpeedChange);

    wrap.addEventListener('keydown', (e) => {
      if (e.target === wrap && (e.key === ' ' || e.key === 'Enter')) {
        e.preventDefault();
        togglePlayback();
      }
    });

    return wrap;
  }

  function renderMedia(msg) {
    if (msg.media_type === 'gif') {
      return renderGif(msg);
    }
    if (msg.media_type === 'video' && isVideoNote(msg)) {
      return renderVideoNote(msg);
    }
    if (msg.media_type === 'audio' && isVoiceNote(msg)) {
      return renderVoiceNoteBubble(msg);
    }

    const wrap = document.createElement('div');
    wrap.className = 'msg-media';
    const path = msg.archive_path;
    const src = '/media/' + path;
    const mt = msg.media_type;

    const ratio = _extractMsgAspectRatio(msg);

    if (mt === 'image' || mt === 'sticker') {
      if (mt === 'sticker') {
        wrap.classList.add('sticker');
        if (ratio > 0) wrap.style.aspectRatio = ratio.toFixed(3);
      } else {
        _applyMediaOrientationBox(wrap, ratio);
      }
      const cleanName = _sanitizeMediaName(msg.media_name);
      const img = document.createElement('img');
      img.src = src;
      img.loading = 'lazy';
      img.alt = cleanName || (mt === 'sticker' ? 'Sticker' : 'Photo');
      img.addEventListener('error', () => {
        msg.is_missing = 1;
        const missingCard = renderMissingMedia(msg);
        if (wrap.isConnected) {
          wrap.replaceWith(missingCard);
        } else {
          wrap.innerHTML = '';
          wrap.className = missingCard.className;
          wrap.style.cssText = missingCard.style.cssText;
          while (missingCard.firstChild) {
            wrap.appendChild(missingCard.firstChild);
          }
        }
        const scroll = document.getElementById('message-scroll');
        if (scroll && isUserAtBottom) {
          suppressScroll = true;
          scroll.scrollTop = scroll.scrollHeight;
          suppressScroll = false;
        }
      }, { once: true });
      if (mt !== 'sticker' && ratio <= 0) {
        img.addEventListener('load', () => {
          if (img.naturalWidth > 0 && img.naturalHeight > 0) {
            _applyMediaOrientationBox(wrap, img.naturalWidth / img.naturalHeight);
          }
        }, { once: true });
      }
      img.addEventListener('click', () => {
        if (!msg.is_missing) showLightbox(msg);
      });
      _attachMediaScrollAnchor(img);
      wrap.appendChild(img);

      if (msg.is_live_photo && msg.live_photo_path) {
        wrap.classList.add('msg-live-photo');
        const liveBadge = document.createElement('span');
        liveBadge.className = 'msg-live-badge';
        liveBadge.innerHTML = '<span class="live-icon">◎</span> LIVE';
        liveBadge.title = 'Live Photo: hover or click to play motion';
        wrap.appendChild(liveBadge);

        let liveVid = null;

        function startLivePlay() {
          if (!liveVid) {
            liveVid = document.createElement('video');
            liveVid.className = 'live-photo-video';
            liveVid.src = '/media/' + msg.live_photo_path;
            liveVid.muted = true;
            liveVid.loop = true;
            liveVid.playsInline = true;
            wrap.appendChild(liveVid);
          }
          liveVid.currentTime = 0;
          liveVid.play().then(() => {
            wrap.classList.add('is-live-playing');
          }).catch(() => {});
        }

        function stopLivePlay() {
          if (liveVid) {
            liveVid.pause();
            liveVid.currentTime = 0;
            wrap.classList.remove('is-live-playing');
          }
        }

        wrap.addEventListener('mouseenter', () => {
          startLivePlay();
        });
        wrap.addEventListener('mouseleave', () => {
          stopLivePlay();
        });

        liveBadge.addEventListener('click', (e) => {
          e.stopPropagation();
          if (wrap.classList.contains('is-live-playing')) {
            stopLivePlay();
          } else {
            startLivePlay();
          }
        });
      }
    } else if (mt === 'video') {
      _applyMediaOrientationBox(wrap, ratio);
      const vid = document.createElement('video');
      vid.controls = true;
      vid.preload = 'none';
      vid.src = src;
      wrap.appendChild(vid);
      const handleVideoError = () => {
        msg.is_missing = 1;
        const missingCard = renderMissingMedia(msg);
        if (wrap.isConnected) {
          wrap.replaceWith(missingCard);
        } else {
          wrap.innerHTML = '';
          wrap.className = missingCard.className;
          wrap.style.cssText = missingCard.style.cssText;
          while (missingCard.firstChild) {
            wrap.appendChild(missingCard.firstChild);
          }
        }
        const scroll = document.getElementById('message-scroll');
        if (scroll && isUserAtBottom) {
          suppressScroll = true;
          scroll.scrollTop = scroll.scrollHeight;
          suppressScroll = false;
        }
      };
      vid.addEventListener('error', handleVideoError, { once: true });
      // extract first frame as poster so the bubble isn't blank before playback
      const probe = document.createElement('video');
      probe.src = src;
      probe.muted = true;
      probe.preload = 'metadata';
      probe.addEventListener('loadeddata', () => {
        if (ratio <= 0 && probe.videoWidth > 0 && probe.videoHeight > 0) {
          _applyMediaOrientationBox(wrap, probe.videoWidth / probe.videoHeight);
        }
        const canvas = document.createElement('canvas');
        canvas.width = probe.videoWidth;
        canvas.height = probe.videoHeight;
        canvas.getContext('2d').drawImage(probe, 0, 0);
        vid.poster = canvas.toDataURL('image/jpeg', 0.8);
        probe.src = '';
        const scroll = document.getElementById('message-scroll');
        if (scroll && isUserAtBottom) {
          suppressScroll = true;
          scroll.scrollTop = scroll.scrollHeight;
          suppressScroll = false;
        }
      }, { once: true });
      probe.addEventListener('error', () => {
        probe.src = '';
        handleVideoError();
      }, { once: true });
    } else if (mt === 'audio') {
      if (isVoiceNote(msg)) {
        return renderVoiceNoteBubble(msg);
      }
      wrap.classList.add('audio');
      const aud = document.createElement('audio');
      aud.controls = true;
      aud.src = src;
      wrap.appendChild(aud);
    } else if (mt === 'vcard') {
      wrap.classList.add('vcard');
      const card = document.createElement('div');
      card.className = 'vcard-card';

      const avatar = document.createElement('div');
      avatar.className = 'vcard-avatar';
      avatar.innerHTML = '<span class="vcard-avatar-icon">👤</span>';

      const details = document.createElement('div');
      details.className = 'vcard-details';

      const nameEl = document.createElement('div');
      nameEl.className = 'vcard-name';
      nameEl.textContent = msg.text_body || msg.media_name || 'Contact';

      const subEl = document.createElement('div');
      subEl.className = 'vcard-sub';
      subEl.textContent = 'Contact Card';

      details.appendChild(nameEl);
      details.appendChild(subEl);
      card.appendChild(avatar);
      card.appendChild(details);

      const downloadLink = document.createElement('a');
      downloadLink.className = 'vcard-download';
      downloadLink.href = src;
      const downloadName = (msg.media_name && msg.media_name.toLowerCase().endsWith('.vcf'))
        ? msg.media_name
        : ((msg.text_body ? msg.text_body : 'contact') + '.vcf');
      downloadLink.download = downloadName;
      downloadLink.innerHTML = '<span class="vcard-download-icon">📇</span><span>View Contact (.vcf)</span>';

      wrap.appendChild(card);
      wrap.appendChild(downloadLink);
    } else {
      wrap.classList.add('doc');
      const a = document.createElement('a');
      a.href = src;
      a.className = 'doc-link';
      a.download = msg.media_name || 'file';
      a.innerHTML = `<span class="doc-icon">📄</span><span>${esc(msg.media_name || 'Download')}</span>`;
      wrap.appendChild(a);
    }
    if (msg.is_view_once) {
      const voBadge = document.createElement('span');
      voBadge.className = 'msg-view-once-badge';
      voBadge.innerHTML = '<span class="vo-icon">①</span> View Once';
      wrap.appendChild(voBadge);
    }
    return wrap;
  }

  function formatCoordinates(lat, lng) {
    const latNum = Number(lat);
    const lngNum = Number(lng);
    if (isNaN(latNum) || isNaN(lngNum)) return (lat || '') + ', ' + (lng || '');
    const latDir = latNum >= 0 ? 'N' : 'S';
    const lngDir = lngNum >= 0 ? 'E' : 'W';
    return Math.abs(latNum).toFixed(4) + '° ' + latDir + ', ' + Math.abs(lngNum).toFixed(4) + '° ' + lngDir;
  }

  function renderLocation(msg) {
    const isLive = msg.media_type === 'live_location'
      || msg.raw_msg_type === 16
      || Boolean(Number(msg.live_location_share_duration) > 0);
    const wrap = document.createElement('div');
    wrap.className = 'msg-location-wrap';

    const card = document.createElement('div');
    card.className = 'msg-location-card' + (isLive ? ' is-live-location' : ' is-static-location');

    // 1. Map Header (Pre-rendered thumbnail from DB or offline vector fallback)
    const mapHeader = document.createElement('div');
    mapHeader.className = 'location-map-header';

    const typeChip = document.createElement('div');
    typeChip.className = 'location-map-type-chip' + (isLive ? ' is-live' : ' is-static');
    typeChip.innerHTML = isLive
      ? '<span class="live-pulse-dot" aria-hidden="true"></span><span>Live Location</span>'
      : '<span class="loc-pin-icon" aria-hidden="true">📍</span><span>Location</span>';
    mapHeader.appendChild(typeChip);

    const fallbackVector = () => {
      const existingHint = mapHeader.querySelector('.location-map-hover-hint');
      const existingChip = mapHeader.querySelector('.location-map-type-chip');
      mapHeader.innerHTML = '';
      if (existingChip) {
        mapHeader.appendChild(existingChip);
      }
      const svgWrap = document.createElement('div');
      svgWrap.className = 'location-vector-map' + (isLive ? ' is-live' : '');
      svgWrap.innerHTML = `
        <svg viewBox="0 0 320 180" xmlns="http://www.w3.org/2000/svg" class="location-map-svg" aria-hidden="true">
          <defs>
            <pattern id="locGrid" width="40" height="40" patternUnits="userSpaceOnUse">
              <path d="M 40 0 L 0 0 0 40" fill="none" stroke="currentColor" stroke-width="0.75" stroke-opacity="0.18" />
            </pattern>
          </defs>
          <rect width="100%" height="100%" fill="currentColor" class="map-bg" />
          <rect width="100%" height="100%" fill="url(#locGrid)" />
          <path d="M-10 60 Q 90 40 180 80 T 330 110" fill="none" stroke="currentColor" stroke-width="5" stroke-opacity="0.32" />
          <path d="M 70 -10 Q 120 70 160 190" fill="none" stroke="currentColor" stroke-width="3" stroke-opacity="0.25" />
          <path d="M 230 -10 Q 200 80 250 190" fill="none" stroke="currentColor" stroke-width="2.5" stroke-opacity="0.2" />
          <circle cx="160" cy="90" r="${isLive ? 32 : 24}" fill="none" stroke="var(--accent)" stroke-width="1.5" stroke-opacity="0.35" class="location-ping-ring" />
          ${isLive ? '<circle cx="160" cy="90" r="16" fill="var(--accent)" fill-opacity="0.2" />' : ''}
          ${isLive 
            ? '<circle cx="160" cy="90" r="6" fill="var(--accent)" />'
            : '<path d="M 160 76 C 153.4 76 148 81.4 148 88 C 148 97 160 108 160 108 C 160 108 172 97 172 88 C 172 81.4 166.6 76 160 76 Z" fill="#ea4335" /><circle cx="160" cy="87" r="3.5" fill="#fff" />'
          }
        </svg>
      `;
      mapHeader.appendChild(svgWrap);
      if (existingHint) {
        mapHeader.appendChild(existingHint);
      }
    };

    if (msg.has_thumbnail) {
      const img = document.createElement('img');
      img.className = 'location-map-thumb';
      img.loading = 'lazy';
      img.alt = msg.location_name || (isLive ? 'Live Location' : 'Location map');
      img.src = '/api/thumbnail/' + msg.msg_id;
      img.addEventListener('error', fallbackVector, { once: true });
      mapHeader.appendChild(img);
    } else {
      fallbackVector();
    }

    if (msg.latitude && msg.longitude) {
      mapHeader.classList.add('is-interactive');
      mapHeader.setAttribute('role', 'button');
      mapHeader.setAttribute('tabindex', '0');
      mapHeader.setAttribute('aria-haspopup', 'dialog');
      mapHeader.setAttribute('aria-label', (msg.location_name ? (msg.location_name + ' — ') : '') + 'Open in Maps');
      mapHeader.title = 'Click to open in maps';

      const hoverHint = document.createElement('div');
      hoverHint.className = 'location-map-hover-hint';
      hoverHint.innerHTML = '<span class="hover-hint-icon" aria-hidden="true">🗺️</span><span>Open in Maps</span> <span class="hover-hint-arrow" aria-hidden="true">↗</span>';
      mapHeader.appendChild(hoverHint);

      const triggerMapChooser = (e) => {
        e.stopPropagation();
        openMapChooser({
          latitude: msg.latitude,
          longitude: msg.longitude,
          name: msg.location_name,
          address: msg.location_address,
          isLive: isLive
        }, mapHeader);
      };

      mapHeader.addEventListener('click', triggerMapChooser);
      mapHeader.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          triggerMapChooser(e);
        }
      });
    }

    card.appendChild(mapHeader);

    // 2. Details (Venue Name, Address, Status, Timestamps, Coordinates)
    const details = document.createElement('div');
    details.className = 'location-details';

    if (isLive) {
      const liveBadge = document.createElement('div');
      liveBadge.className = 'location-live-badge';
      liveBadge.innerHTML = '<span class="live-dot" aria-hidden="true"></span><span class="live-label">Live location ended</span>';
      details.appendChild(liveBadge);

      if (msg.live_location_share_duration || msg.timestamp_ms) {
        const timeInfo = document.createElement('div');
        timeInfo.className = 'location-time-info';
        let durStr = '';
        const dur = Number(msg.live_location_share_duration) || 0;
        if (dur >= 3600) {
          const hrs = Math.round((dur / 3600) * 10) / 10;
          durStr = hrs + (hrs === 1 ? ' hr' : ' hrs');
        } else if (dur > 0) {
          durStr = Math.round(dur / 60) + ' min';
        }

        let endStr = '';
        if (msg.timestamp_ms && dur > 0) {
          const endDate = new Date(Number(msg.timestamp_ms) + dur * 1000);
          endStr = 'Ended at ' + endDate.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
        }

        const summary = [durStr ? ('Shared for ' + durStr) : '', endStr].filter(Boolean).join(' · ');
        if (summary) {
          timeInfo.textContent = summary;
          details.appendChild(timeInfo);
        }
      }

      if (msg.latitude && msg.longitude) {
        const coordsEl = document.createElement('div');
        coordsEl.className = 'location-coords-sub';
        coordsEl.textContent = formatCoordinates(msg.latitude, msg.longitude);
        details.appendChild(coordsEl);
      }
    } else {
      if (msg.location_name) {
        const nameEl = document.createElement('div');
        nameEl.className = 'location-name';
        nameEl.textContent = msg.location_name;
        details.appendChild(nameEl);
      } else {
        const nameEl = document.createElement('div');
        nameEl.className = 'location-name';
        nameEl.textContent = 'Pinned location';
        details.appendChild(nameEl);
      }
      if (msg.location_address) {
        const addrEl = document.createElement('div');
        addrEl.className = 'location-address';
        addrEl.textContent = msg.location_address;
        details.appendChild(addrEl);
      }
      if (msg.latitude && msg.longitude) {
        const coordsEl = document.createElement('div');
        coordsEl.className = 'location-coords-sub';
        coordsEl.textContent = formatCoordinates(msg.latitude, msg.longitude);
        details.appendChild(coordsEl);
      }
    }

    card.appendChild(details);
    wrap.appendChild(card);

    if (msg.text_body) {
      const cleanBody = msg.text_body.trim();
      const isDuplicate = cleanBody === msg.location_name
        || cleanBody === msg.location_address
        || cleanBody === 'Location'
        || cleanBody === 'Pinned location'
        || cleanBody === 'Live Location'
        || cleanBody.startsWith('Live Location');
      if (!isDuplicate && cleanBody) {
        const cap = document.createElement('div');
        cap.className = 'msg-caption';
        renderCollapsibleMessageText(cap, cleanBody, msg.msg_id, msg.mentions);
        wrap.appendChild(cap);
      }
    }

    return wrap;
  }

  function renderPoll(msg) {
    const poll = msg.poll || {
      question: msg.text_body || 'Poll',
      multiple_answers: false,
      total_votes: 0,
      total_voters: 0,
      options: [],
    };

    const container = document.createElement('div');
    container.className = 'msg-poll';
    container.setAttribute('role', 'group');
    container.setAttribute('aria-label', 'Poll: ' + (poll.question || 'Poll'));

    const header = document.createElement('div');
    header.className = 'msg-poll-header';

    const q = document.createElement('div');
    q.className = 'msg-poll-question';
    q.innerHTML = highlight(poll.question || 'Poll', '');
    header.appendChild(q);

    const badge = document.createElement('div');
    badge.className = 'msg-poll-badge';
    badge.textContent = poll.multiple_answers ? 'Multiple choice' : 'Single choice';
    header.appendChild(badge);

    container.appendChild(header);

    const optionsList = document.createElement('div');
    optionsList.className = 'msg-poll-options';

    const totalVotes = poll.total_votes || 0;

    for (const opt of poll.options || []) {
      const optEl = document.createElement('div');
      optEl.className = 'msg-poll-option' + (opt.user_selected ? ' user-selected' : '');

      const optVotes = (opt.vote_count !== undefined ? opt.vote_count : opt.votes) || 0;
      const pct = totalVotes > 0 ? Math.round((optVotes / totalVotes) * 100) : 0;

      const fill = document.createElement('div');
      fill.className = 'msg-poll-option-fill';
      fill.style.width = pct + '%';
      fill.setAttribute('role', 'progressbar');
      fill.setAttribute('aria-valuenow', String(pct));
      fill.setAttribute('aria-valuemin', '0');
      fill.setAttribute('aria-valuemax', '100');
      optEl.appendChild(fill);

      const content = document.createElement('div');
      content.className = 'msg-poll-option-content';

      const titleEl = document.createElement('span');
      titleEl.className = 'msg-poll-option-title';
      if (opt.user_selected) {
        const checkIcon = document.createElement('span');
        checkIcon.className = 'msg-poll-check';
        checkIcon.textContent = '✓ ';
        titleEl.appendChild(checkIcon);
      }
      const nameSpan = document.createElement('span');
      nameSpan.innerHTML = highlight(opt.name || '', '');
      titleEl.appendChild(nameSpan);
      content.appendChild(titleEl);

      const countEl = document.createElement('span');
      countEl.className = 'msg-poll-option-count';
      countEl.textContent = String(optVotes);
      content.appendChild(countEl);

      optEl.appendChild(content);
      optionsList.appendChild(optEl);
    }

    container.appendChild(optionsList);

    const footer = document.createElement('div');
    footer.className = 'msg-poll-footer';

    const totalEl = document.createElement('span');
    totalEl.className = 'msg-poll-total';
    const vCount = poll.total_voters !== undefined ? poll.total_voters : totalVotes;
    totalEl.textContent = vCount === 1 ? '1 vote' : vCount + ' votes';
    footer.appendChild(totalEl);

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'msg-poll-view-votes-btn';
    btn.textContent = 'View votes';
    btn.setAttribute('aria-haspopup', 'dialog');
    btn.addEventListener('click', e => {
      e.stopPropagation();
      showPollDetails(msg.msg_id, btn);
    });
    footer.appendChild(btn);

    container.appendChild(footer);
    return container;
  }

  let lightboxItems = [];
  let lightboxIndex = 0;
  let lightboxContext = 'gallery'; // 'gallery' | 'chat' — controls pagination + type filtering

  function isLightboxMedia(m) {
    return m.archive_path && !m.is_missing && (m.media_type === 'image' || m.media_type === 'video' ||
      m.media_type === 'gif' || m.media_type === 'sticker');
  }

  function _closeLb() {
    const lb = document.getElementById('img-lightbox');
    if (!lb) return;
    lbZoomController?.destroy();
    lbZoomController = null;
    lb.classList.add('is-closing');
    lb.addEventListener('animationend', () => lb.remove(), { once: true });
    setTimeout(() => lb.isConnected && lb.remove(), 300);
  }

  let lightboxNavSeq = 0;
  let lbZoomController = null;

  function _setupLightboxZoom(lb) {
    let scale = 1;
    let panX = 0;
    let panY = 0;
    let isDragging = false;
    let hasDragged = false;
    let startX = 0;
    let startY = 0;
    let currentMedia = null;

    let zoomBadge = lb.querySelector('.lb-zoom-badge');
    if (!zoomBadge) {
      zoomBadge = document.createElement('div');
      zoomBadge.className = 'lb-zoom-badge';
      zoomBadge.style.display = 'none';
      zoomBadge.title = 'Click to reset zoom';
      lb.appendChild(zoomBadge);
    }

    function applyTransform(animated) {
      if (!currentMedia) return;
      currentMedia.style.transition = animated ? 'transform 0.12s ease-out' : 'none';
      currentMedia.style.transform = `translate(${panX}px, ${panY}px) scale(${scale})`;
      if (scale > 1) {
        currentMedia.style.cursor = isDragging ? 'grabbing' : 'grab';
        zoomBadge.textContent = `${Math.round(scale * 100)}% \u00d7 Reset`;
        zoomBadge.style.display = 'block';
      } else {
        currentMedia.style.cursor = 'zoom-in';
        zoomBadge.style.display = 'none';
      }
    }

    function clampPan() {
      if (!currentMedia || scale <= 1) {
        panX = 0;
        panY = 0;
        return;
      }
      const stage = lb.querySelector('.lb-stage');
      const stageW = (stage && stage.clientWidth) ? stage.clientWidth : window.innerWidth;
      const stageH = (stage && stage.clientHeight) ? stage.clientHeight : window.innerHeight;
      const scaledW = currentMedia.offsetWidth * scale;
      const scaledH = currentMedia.offsetHeight * scale;
      const maxPanX = Math.max(40, (scaledW - stageW) / 2 + 40);
      const maxPanY = Math.max(40, (scaledH - stageH) / 2 + 40);
      panX = Math.max(-maxPanX, Math.min(maxPanX, panX));
      panY = Math.max(-maxPanY, Math.min(maxPanY, panY));
    }

    function reset() {
      scale = 1;
      panX = 0;
      panY = 0;
      isDragging = false;
      hasDragged = false;
      if (currentMedia) {
        currentMedia.style.transition = 'none';
        currentMedia.style.transform = '';
        currentMedia.style.cursor = '';
      }
      if (zoomBadge) zoomBadge.style.display = 'none';
    }

    const onWheel = e => {
      if (!currentMedia || currentMedia.tagName !== 'IMG') return;
      e.preventDefault();
      const zoomFactor = e.deltaY < 0 ? 1.25 : (1 / 1.25);
      const prevScale = scale;
      let nextScale = scale * zoomFactor;
      if (nextScale < 1.05) {
        scale = 1;
        panX = 0;
        panY = 0;
        applyTransform(true);
        return;
      }
      if (nextScale > 6) {
        nextScale = 6;
      }
      const stage = lb.querySelector('.lb-stage');
      const stageRect = stage ? stage.getBoundingClientRect() : null;
      const cx = (stageRect ? (stageRect.left + stageRect.width / 2) : (window.innerWidth / 2)) + panX;
      const cy = (stageRect ? (stageRect.top + stageRect.height / 2) : (window.innerHeight / 2)) + panY;
      const dx = e.clientX - cx;
      const dy = e.clientY - cy;
      const ratio = nextScale / prevScale;
      panX -= dx * (ratio - 1);
      panY -= dy * (ratio - 1);
      scale = nextScale;
      clampPan();
      applyTransform(true);
    };

    lb.addEventListener('wheel', onWheel, { passive: false });

    const onMouseMove = e => {
      if (!isDragging || !currentMedia) return;
      hasDragged = true;
      panX = e.clientX - startX;
      panY = e.clientY - startY;
      clampPan();
      applyTransform(false);
    };

    const onMouseUp = () => {
      if (isDragging) {
        isDragging = false;
        window.removeEventListener('mousemove', onMouseMove);
        window.removeEventListener('mouseup', onMouseUp);
        applyTransform(false);
      }
    };

    const onMouseDown = e => {
      if (e.button !== 0 || !currentMedia) return;
      if (scale > 1) {
        isDragging = true;
        hasDragged = false;
        startX = e.clientX - panX;
        startY = e.clientY - panY;
        currentMedia.style.cursor = 'grabbing';
        window.addEventListener('mousemove', onMouseMove);
        window.addEventListener('mouseup', onMouseUp);
        e.preventDefault();
      }
    };

    const onClick = e => {
      if (hasDragged || !currentMedia) return;
      e.stopPropagation();
      if (scale === 1) {
        scale = 2.5;
        const stage = lb.querySelector('.lb-stage');
        const stageRect = stage ? stage.getBoundingClientRect() : null;
        const cx = stageRect ? (stageRect.left + stageRect.width / 2) : (window.innerWidth / 2);
        const cy = stageRect ? (stageRect.top + stageRect.height / 2) : (window.innerHeight / 2);
        panX = -(e.clientX - cx) * 1.5;
        panY = -(e.clientY - cy) * 1.5;
        clampPan();
        applyTransform(true);
      } else {
        scale = 1;
        panX = 0;
        panY = 0;
        applyTransform(true);
      }
    };

    zoomBadge.addEventListener('click', e => {
      e.stopPropagation();
      scale = 1;
      panX = 0;
      panY = 0;
      applyTransform(true);
    });

    return {
      attach: (mediaEl) => {
        reset();
        currentMedia = mediaEl;
        if (mediaEl && mediaEl.tagName === 'IMG') {
          mediaEl.draggable = false;
          mediaEl.addEventListener('mousedown', onMouseDown);
          mediaEl.addEventListener('click', onClick);
        }
      },
      reset: reset,
      destroy: () => {
        reset();
        window.removeEventListener('mousemove', onMouseMove);
        window.removeEventListener('mouseup', onMouseUp);
      }
    };
  }

  function _createLightboxMediaElement(msg) {
    const src = '/media/' + msg.archive_path;
    const mt = msg.media_type;
    let media;
    if ((mt === 'video' || mt === 'gif') && !src.toLowerCase().endsWith('.gif')) {
      media = document.createElement('video');
      media.controls = (mt === 'video');
      media.autoplay = true;
      media.loop = true;
      media.muted = (mt === 'gif');
      media.playsInline = true;
      media.src = src;
      if (msg.is_video_note || isVideoNote(msg)) {
        media.classList.add('lb-video-note');
      }
    } else {
      media = document.createElement('img');
      media.src = src;
    }
    return media;
  }

  function _preloadAdjacentLightboxMedia(index) {
    const nextIdx = _nextLightboxIndex(index, 1);
    if (nextIdx !== -1) {
      const nextMsg = lightboxItems[nextIdx];
      if (nextMsg && !nextMsg.is_missing && (nextMsg.media_type === 'image' || nextMsg.media_type === 'sticker')) {
        const img = new Image();
        img.src = '/media/' + nextMsg.archive_path;
      }
    }
    const prevIdx = _nextLightboxIndex(index, -1);
    if (prevIdx !== -1) {
      const prevMsg = lightboxItems[prevIdx];
      if (prevMsg && !prevMsg.is_missing && (prevMsg.media_type === 'image' || prevMsg.media_type === 'sticker')) {
        const img = new Image();
        img.src = '/media/' + prevMsg.archive_path;
      }
    }
  }

  function _setupLightboxLivePhoto(lb, stage, msg, media) {
    const oldBtn = lb.querySelector('.lb-live-btn');
    if (oldBtn) oldBtn.remove();
    const oldLiveVid = stage.querySelector('.lb-live-video');
    if (oldLiveVid) {
      oldLiveVid.pause();
      oldLiveVid.remove();
    }

    if (!msg.is_live_photo || !msg.live_photo_path) return;

    const liveBtn = document.createElement('button');
    liveBtn.className = 'lb-live-btn';
    liveBtn.innerHTML = '<span class="live-icon">◎</span> LIVE';
    liveBtn.title = 'Toggle Live Photo playback';
    let isPlaying = false;
    let lbVid = null;
    liveBtn.addEventListener('click', e => {
      e.stopPropagation();
      isPlaying = !isPlaying;
      if (isPlaying) {
        liveBtn.classList.add('active');
        if (!lbVid) {
          lbVid = document.createElement('video');
          lbVid.className = 'lb-live-video';
          lbVid.src = '/media/' + msg.live_photo_path;
          lbVid.autoplay = true;
          lbVid.loop = true;
          lbVid.muted = true;
          lbVid.playsInline = true;
        }
        media.style.display = 'none';
        stage.appendChild(lbVid);
      } else {
        liveBtn.classList.remove('active');
        if (lbVid) {
          lbVid.pause();
          lbVid.remove();
          lbVid = null;
        }
        media.style.display = '';
      }
    });
    lb.appendChild(liveBtn);
  }

  async function _transitionLightboxMedia(lb, stage, msg, dir, seq) {
    const incomingMedia = _createLightboxMediaElement(msg);

    if (dir === 0) {
      stage.innerHTML = '';
      stage.appendChild(incomingMedia);
      _setupLightboxLivePhoto(lb, stage, msg, incomingMedia);
      lbZoomController?.attach(incomingMedia);
      return;
    }

    if (incomingMedia instanceof HTMLImageElement && incomingMedia.decode) {
      try {
        await Promise.race([
          incomingMedia.decode(),
          new Promise(resolve => setTimeout(resolve, 80))
        ]);
      } catch (_) {
        // Fall back without blocking if decoding fails or aborts
      }
    }

    if (seq !== lightboxNavSeq) return;

    stage.querySelectorAll('.lb-media-exiting').forEach(el => el.remove());

    const outgoing = stage.querySelector('img:not(.lb-media-exiting), video:not(.lb-media-exiting)');
    if (outgoing) {
      if (outgoing.tagName === 'VIDEO') {
        outgoing.pause();
      }
      outgoing.classList.add('lb-media-exiting');
      outgoing.style.transform = dir > 0 ? 'translateX(-40px)' : 'translateX(40px)';
      outgoing.style.opacity = '0';
      let cleanedOutgoing = false;
      let cleanTimer = null;
      const cleanupOutgoing = () => {
        if (cleanedOutgoing) return;
        cleanedOutgoing = true;
        if (cleanTimer !== null) {
          clearTimeout(cleanTimer);
          cleanTimer = null;
        }
        if (outgoing.isConnected) outgoing.remove();
      };
      outgoing.addEventListener('transitionend', cleanupOutgoing, { once: true });
      cleanTimer = setTimeout(cleanupOutgoing, 250);
    }

    incomingMedia.classList.add('lb-media-entering');
    incomingMedia.style.transform = dir > 0 ? 'translateX(40px)' : 'translateX(-40px)';
    incomingMedia.style.opacity = '0';
    stage.appendChild(incomingMedia);

    _setupLightboxLivePhoto(lb, stage, msg, incomingMedia);

    requestAnimationFrame(() => {
      if (seq !== lightboxNavSeq) return;
      incomingMedia.style.transform = 'translateX(0)';
      incomingMedia.style.opacity = '1';
    });

    let finalized = false;
    let finalizeTimer = null;
    const finalizeIncoming = () => {
      if (finalized) return;
      finalized = true;
      if (finalizeTimer !== null) {
        clearTimeout(finalizeTimer);
        finalizeTimer = null;
      }
      if (seq !== lightboxNavSeq) return;
      incomingMedia.classList.remove('lb-media-entering');
      incomingMedia.style.transform = '';
      incomingMedia.style.opacity = '';
      incomingMedia.style.transition = '';
      lbZoomController?.attach(incomingMedia);
    };
    incomingMedia.addEventListener('transitionend', finalizeIncoming, { once: true });
    finalizeTimer = setTimeout(finalizeIncoming, 250);
  }

  function openLightboxAt(index, dir) {
    const msg = lightboxItems[index];
    if (!msg) return;

    const sep = outputRoot.includes('\\') ? '\\' : '/';
    const fullPath = outputRoot.replace(/[/\\]+$/, '') + sep + msg.archive_path.replace(/\//g, sep);

    let lb = document.getElementById('img-lightbox');
    const isNewLb = !lb;

    if (isNewLb) {
      lb = document.createElement('div');
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
      prevBtn.addEventListener('click', e => {
        e.stopPropagation();
        const i = _nextLightboxIndex(lightboxIndex, -1);
        if (i !== -1) { lightboxIndex = i; openLightboxAt(lightboxIndex, -1); }
      });
      lb.appendChild(prevBtn);

      const nextBtn = document.createElement('button');
      nextBtn.className = 'lb-arrow next';
      nextBtn.innerHTML = '&#10095;';
      nextBtn.title = 'Next';
      nextBtn.addEventListener('click', async e => {
        e.stopPropagation();
        let i = _nextLightboxIndex(lightboxIndex, 1);
        if (i !== -1) {
          lightboxIndex = i; openLightboxAt(lightboxIndex, 1);
        } else if (lightboxContext === 'gallery' && !galleryAllLoaded) {
          const oldest = currentGalleryItems[currentGalleryItems.length - 1]?.timestamp_ms;
          await _loadGalleryPage(oldest);
          i = _nextLightboxIndex(lightboxIndex, 1);
          if (i !== -1) { lightboxIndex = i; openLightboxAt(lightboxIndex, 1); }
        }
      });
      lb.appendChild(nextBtn);

      const tsEl = document.createElement('div');
      tsEl.className = 'lb-timestamp';
      lb.appendChild(tsEl);

      const stage = document.createElement('div');
      stage.className = 'lb-stage';
      lb.appendChild(stage);

      const pathRow = document.createElement('div');
      pathRow.className = 'lb-path';
      const pathText = document.createElement('span');
      const copyBtn = document.createElement('button');
      copyBtn.className = 'lb-copy-btn';
      copyBtn.title = 'Copy path';
      copyBtn.innerHTML = '&#128203;';
      copyBtn.addEventListener('click', e => {
        e.stopPropagation();
        const curMsg = lightboxItems[lightboxIndex];
        if (!curMsg) return;
        const curFullPath = outputRoot.replace(/[/\\]+$/, '') + sep + curMsg.archive_path.replace(/\//g, sep);
        navigator.clipboard.writeText(curFullPath).then(() => {
          copyBtn.classList.add('copied');
          setTimeout(() => copyBtn.classList.remove('copied'), 1500);
        });
      });
      pathRow.appendChild(pathText);
      pathRow.appendChild(copyBtn);
      lb.appendChild(pathRow);

      lb.addEventListener('click', e => { if (e.target === lb || e.target === stage) _closeLb(); });
      document.body.appendChild(lb);

      lbZoomController = _setupLightboxZoom(lb);
    } else {
      lbZoomController?.reset();
    }

    const tsEl = lb.querySelector('.lb-timestamp');
    if (tsEl) tsEl.textContent = fmtTime(msg.timestamp_ms);

    const pathText = lb.querySelector('.lb-path span');
    if (pathText) pathText.textContent = fullPath;

    const prevBtn = lb.querySelector('.lb-arrow.prev');
    if (prevBtn) prevBtn.disabled = _nextLightboxIndex(index, -1) === -1;

    const nextBtn = lb.querySelector('.lb-arrow.next');
    if (nextBtn) {
      nextBtn.disabled = _nextLightboxIndex(index, 1) === -1 &&
        (lightboxContext !== 'gallery' || galleryAllLoaded);
    }

    const stage = lb.querySelector('.lb-stage');
    const seq = ++lightboxNavSeq;
    _transitionLightboxMedia(lb, stage, msg, isNewLb ? 0 : dir, seq);
    _preloadAdjacentLightboxMedia(index);
  }

  function showLightbox(msg) {
    if (!msg || msg.is_missing) return;
    // Navigate the chat's currently-loaded media (chronological), not the stale
    // gallery list. msg is the same object reference held in msgList, so identity
    // lookup gives its position. Falls back to a single-item view if not found.
    lightboxContext = 'chat';
    lightboxItems = msgList.filter(isLightboxMedia);
    lightboxIndex = lightboxItems.indexOf(msg);
    if (lightboxIndex === -1) {
      lightboxItems = [msg];
      lightboxIndex = 0;
    }
    openLightboxAt(lightboxIndex, 0);
  }

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
  let currentGalleryStats = null;
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
          rootMargin: '150px 0px 150px 0px',
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
    const cells = grid.querySelectorAll('.gallery-item[data-hydrated="false"]:not(.type-hidden)');
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
        grid.insertBefore(scrollLoader, gallerySentinel || null);
      }
    }

    try {
      const beforeParam = (oldestTs !== Infinity && oldestTs > 0) ? '&before=' + oldestTs : '';
      const qs = 'chat_id=' + encodeURIComponent(currentChat.id) +
                 '&chat_type=' + encodeURIComponent(currentChat.type) +
                 '&limit=30' + beforeParam;
      const signal = gallerySessionAbortController ? gallerySessionAbortController.signal : undefined;
      const res = await fetch('/api/media/links?' + qs, { signal });
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
      const url = '/api/media?chat_id=' + encodeURIComponent(currentChat.id) +
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
      _loadMoreLinks();
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

    if (galleryLoadingMore) {
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
          task.img.classList.remove('gallery-video-thumb--err');
        }
      } catch {
        if (task.cell.isConnected) {
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
      } else if (videoThumbCache.has(archivePath)) {
        img.src = videoThumbCache.get(archivePath);
      } else {
        scheduleVisibleVideoExtraction();
      }
    } else if (mt === 'link') {
      img.src = src;
      img.onerror = () => {
        const box = cell.querySelector('.gallery-rich-link-media');
        if (box) box.innerHTML = '<span class="gallery-rich-link-fallback-icon">🔗</span>';
      };
    }
  }

  function dehydrateGalleryItem(cell) {
    if (cell.dataset.hydrated !== 'true') {
      return;
    }
    cell.dataset.hydrated = 'false';
    const mt = cell.dataset.type;
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

  function closeMediaGallery() {
    archiveViewActive = false;
    archiveLoadingAll = false;
    galleryBackfillGen++;
    galleryOpenGen++;
    galleryLoadingMore = false;
    linksLoadingMore = false;
    if (currentGalleryAudio) {
      currentGalleryAudio.pause();
      currentGalleryAudio = null;
      currentGalleryAudioCell = null;
    }
    document.getElementById('media-gallery').classList.remove('open');
    if (galleryObserver) { galleryObserver.disconnect(); galleryObserver = null; }
    if (gallerySentinel) { gallerySentinel.remove(); gallerySentinel = null; }
    const loaders = document.querySelectorAll('.gallery-filter-loading, .gallery-scroll-loading, .gallery-end-notice, .gallery-empty-notice');
    loaders.forEach(el => el.remove());
    if (gallerySessionAbortController) {
      gallerySessionAbortController.abort();
      gallerySessionAbortController = null;
    }
    if (galleryCurrentAbortController) {
      galleryCurrentAbortController.abort();
      galleryCurrentAbortController = null;
    }
    lastGalleryScrollTop = 0;
    galleryScrollDirection = 'down';
    galleryPendingScrollCheck = false;
    _setStatPillsLoading(false);
    teardownMediaObservers();
    clearVideoProbeQueue();
    clearTimeout(galleryScrollIdleTimer);
    galleryIsScrolling = false;
  }

  function _sanitizeMediaName(name) {
    if (!name) return '';
    const clean = String(name).replace(/\\/g, '/');
    const base = clean.split('/').pop() || '';
    return base;
  }

  let currentGalleryAudio = null;
  let currentGalleryAudioCell = null;

  function _formatAudioDuration(sec) {
    const s = Math.max(0, Math.floor(Number(sec) || 0));
    if (s <= 0) return '';
    const m = Math.floor(s / 60);
    const rem = s % 60;
    return `${m}:${rem.toString().padStart(2, '0')}`;
  }

  function toggleGalleryAudioPlayback(cell, audioSrc) {
    stopActiveVoiceNote();
    if (activeVideoNotePause) {
      activeVideoNotePause();
    }
    if (currentGalleryAudioCell === cell && currentGalleryAudio) {
      if (currentGalleryAudio.paused) {
        currentGalleryAudio.play();
        cell.classList.add('is-playing');
      } else {
        currentGalleryAudio.pause();
        cell.classList.remove('is-playing');
      }
      return;
    }
    if (currentGalleryAudio) {
      currentGalleryAudio.pause();
      currentGalleryAudio.currentTime = 0;
      if (currentGalleryAudioCell) {
        currentGalleryAudioCell.classList.remove('is-playing');
      }
    }
    const aud = new Audio(audioSrc);
    currentGalleryAudio = aud;
    currentGalleryAudioCell = cell;
    cell.classList.add('is-playing');
    aud.play().catch(() => {
      cell.classList.remove('is-playing');
      if (currentGalleryAudioCell === cell) {
        currentGalleryAudio = null;
        currentGalleryAudioCell = null;
      }
    });
    aud.addEventListener('ended', () => {
      cell.classList.remove('is-playing');
      currentGalleryAudio = null;
      currentGalleryAudioCell = null;
    });
    aud.addEventListener('pause', () => {
      cell.classList.remove('is-playing');
    });
  }

  function buildMissingGalleryCard(cell, msg) {
    if (msg) {
      msg.is_missing = true;
    }
    cell.classList.add('is-missing');
    cell.innerHTML = '';
    delete cell.dataset.src;
    delete cell.dataset.archivePath;
    cell.dataset.hydrated = 'true';

    // Clicking the unavailable media card should do nothing
    cell.addEventListener('click', e => {
      e.stopPropagation();
    });

    if (msg && msg.has_thumbnail && msg.msg_id) {
      const blurImg = document.createElement('img');
      blurImg.className = 'gallery-missing-blur';
      blurImg.src = '/api/thumbnail/' + encodeURIComponent(msg.msg_id);
      blurImg.alt = '';
      blurImg.loading = 'lazy';
      blurImg.onerror = () => { blurImg.style.display = 'none'; };
      cell.appendChild(blurImg);
    }

    const overlay = document.createElement('div');
    overlay.className = 'gallery-missing-overlay';

    const videoNote = Boolean(msg && msg.media_type === 'video' && (msg.is_video_note || isVideoNote(msg)));
    const isVn = Boolean(msg && (msg.is_voice_note || isVoiceNote(msg)));
    const mt = msg ? msg.media_type : '';

    const badge = document.createElement('div');
    badge.className = 'gallery-missing-badge';
    const icon = videoNote ? '🎥' :
                 isVn ? '🎤' :
                 mt === 'video' ? '🎥' :
                 mt === 'audio' ? '🎵' :
                 mt === 'sticker' ? '🩹' :
                 mt === 'gif' ? '🎞️' :
                 mt === 'vcard' ? '📇' :
                 mt === 'document' ? '📄' : '🖼️';
    badge.textContent = icon;
    overlay.appendChild(badge);

    const info = document.createElement('div');
    info.className = 'media-missing-info';

    const label = document.createElement('div');
    label.className = 'gallery-missing-label';
    if (msg && msg.is_view_once) {
      label.textContent = (mt === 'video') ? 'Video · View Once' : 'Photo · View Once';
    } else {
      const typeStr = videoNote ? 'Video message' :
                      isVn ? 'Voice message' :
                      mt === 'video' ? 'Video' :
                      mt === 'gif' ? 'GIF' :
                      mt === 'audio' ? 'Audio' :
                      mt === 'document' ? 'Document' :
                      mt === 'sticker' ? 'Sticker' :
                      (mt === 'location' || mt === 'live_location') ? 'Location' : 'Photo';
      label.textContent = typeStr + ' unavailable';
    }
    info.appendChild(label);

    const sizeStr = (msg && msg.media_size) ? _formatBytes(msg.media_size) : '';
    const durStr = (msg && msg.media_duration && msg.media_duration > 0) ? _formatTimer(msg.media_duration) : '';

    const metaParts = [];
    if (sizeStr) metaParts.push(sizeStr);
    if (durStr) metaParts.push(durStr);
    if (metaParts.length > 0) {
      const meta = document.createElement('div');
      meta.className = 'media-missing-meta';
      meta.textContent = metaParts.join(' · ');
      info.appendChild(meta);
    }

    overlay.appendChild(info);
    cell.appendChild(overlay);

    if (msg) {
      const btn = document.createElement('button');
      btn.className = 'gallery-goto';
      btn.title = 'Go to message';
      btn.textContent = '→ in chat';
      btn.addEventListener('click', e => {
        e.stopPropagation();
        if (btn.disabled) return;
        btn.disabled = true;
        btn.textContent = '⏳ in chat';
        closeMediaGallery();
        jumpToMessage(msg, 'default');
      });
      cell.appendChild(btn);
    }
  }

  function renderGalleryItem(msg) {
    const cell = document.createElement('div');
    cell.className = 'gallery-item';
    cell._msg = msg;
    const mt = msg.media_type;
    const isVn = (mt === 'audio' && isVoiceNote(msg));
    cell.dataset.type = isVn ? 'voice' : mt;
    cell.dataset.ts = String(msg.timestamp_ms || 0);
    if (msg.msg_id || msg.id) {
      cell.dataset.msgId = String(msg.msg_id || msg.id);
    }

    const isPtv = msg.is_video_note || isVideoNote(msg);
    if (isPtv) {
      cell.classList.add('is-video-note');
      const ptvBadge = document.createElement('div');
      ptvBadge.className = 'gallery-ptv-badge';
      ptvBadge.title = 'Video message';
      ptvBadge.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 14.5v-9l6 4.5-6 4.5z"/></svg>';
      cell.appendChild(ptvBadge);
    }

    const cleanName = _sanitizeMediaName(msg.media_name);

    if (!msg.archive_path && mt !== 'link' && mt !== 'vcard') {
      if (msg.has_thumbnail && msg.msg_id) {
        cell.classList.add('is-missing', 'has-thumb');
        cell.dataset.hydrated = 'true';
        const img = document.createElement('img');
        img.src = '/api/thumbnail/' + encodeURIComponent(msg.msg_id);
        img.alt = cleanName || (mt === 'video' ? 'Video' : 'Photo');
        img.className = 'gallery-thumb';
        cell.appendChild(img);

        const warnBadge = document.createElement('div');
        warnBadge.className = 'gallery-missing-indicator-badge';
        warnBadge.title = 'Media file not found on disk';
        warnBadge.innerHTML = '<svg class="gallery-missing-indicator-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';
        cell.appendChild(warnBadge);

        const btn = document.createElement('button');
        btn.className = 'gallery-goto';
        btn.title = 'Go to message';
        btn.textContent = '→ in chat';
        btn.addEventListener('click', e => {
          e.stopPropagation();
          if (btn.disabled) return;
          btn.disabled = true;
          btn.textContent = '⏳ in chat';
          closeMediaGallery();
          jumpToMessage(msg, 'default');
        });
        cell.appendChild(btn);

        // Clicking the unavailable media should do nothing
        cell.addEventListener('click', e => {
          e.stopPropagation();
        });
        return cell;
      }
      buildMissingGalleryCard(cell, msg);
      return cell;
    }

    const src = '/media/' + msg.archive_path;

    if (mt === 'image' || mt === 'sticker') {
      cell.dataset.src = src;
      cell.dataset.hydrated = 'false';
      const img = document.createElement('img');
      img.src = EMPTY_GALLERY_PIX;
      img.alt = cleanName || 'Photo';
      img.className = 'gallery-thumb';
      const idx = lightboxItems.length;
      lightboxItems.push(msg);
      img.addEventListener('click', () => { lightboxContext = 'gallery'; lightboxIndex = idx; openLightboxAt(idx, 0); });
      img.addEventListener('error', () => {
        buildMissingGalleryCard(cell, msg);
      }, { once: true });
      cell.appendChild(img);
    } else if (mt === 'video' || mt === 'gif') {
      cell.dataset.src = src;
      cell.dataset.archivePath = msg.archive_path;
      if (msg.has_thumbnail && msg.msg_id) {
        cell.dataset.thumbSrc = '/api/thumbnail/' + encodeURIComponent(msg.msg_id);
      }
      cell.dataset.hydrated = 'false';
      const img = document.createElement('img');
      img.src = EMPTY_GALLERY_PIX;
      img.alt = cleanName || (mt === 'gif' ? 'GIF' : 'Video');
      img.className = 'gallery-thumb gallery-video-thumb';
      const idx = lightboxItems.length;
      lightboxItems.push(msg);
      img.addEventListener('click', () => { lightboxContext = 'gallery'; lightboxIndex = idx; openLightboxAt(idx, 0); });
      img.addEventListener('error', () => {
        img.classList.add('gallery-video-thumb--err');
      }, { once: true });
      cell.appendChild(img);
    } else if (mt === 'audio') {
      cell.classList.add('gallery-audio-item');
      if (isVn) {
        cell.classList.add('is-voice-note');
      } else {
        cell.classList.add('is-audio-file');
      }

      const hdr = document.createElement('div');
      hdr.className = 'gallery-audio-header';

      const badge = document.createElement('div');
      badge.className = 'gallery-audio-badge ' + (isVn ? 'voice' : 'music');
      if (isVn) {
        badge.title = 'Voice message';
        badge.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/></svg>';
      } else {
        badge.title = 'Audio file';
        badge.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>';
      }
      hdr.appendChild(badge);

      const durText = _formatAudioDuration(msg.media_duration);
      if (durText) {
        const durPill = document.createElement('span');
        durPill.className = 'gallery-audio-dur';
        durPill.textContent = durText;
        hdr.appendChild(durPill);
      } else if (!isVn) {
        const ext = (cleanName.match(/\.([a-z0-9]+)$/i) || [])[1];
        if (ext) {
          const extPill = document.createElement('span');
          extPill.className = 'gallery-audio-dur';
          extPill.textContent = ext.toUpperCase();
          hdr.appendChild(extPill);
        }
      }
      cell.appendChild(hdr);

      const body = document.createElement('div');
      body.className = 'gallery-audio-body';

      const title = document.createElement('div');
      title.className = 'gallery-audio-title';
      if (isVn) {
        title.textContent = 'Voice message';
      } else {
        const cleanTitle = (cleanName && cleanName !== 'audio')
          ? cleanName.replace(/\.[a-z0-9]+$/i, '')
          : 'Audio track';
        title.textContent = cleanTitle;
        title.title = cleanName || 'Audio track';
      }
      body.appendChild(title);

      const sub = document.createElement('div');
      sub.className = 'gallery-audio-sub';
      const senderText = msg.sender || (msg.from_me ? 'You' : '');
      const timeText = msg.timestamp_ms ? fmtTime(msg.timestamp_ms) : '';
      sub.textContent = [senderText, timeText].filter(Boolean).join(' · ');
      body.appendChild(sub);
      cell.appendChild(body);

      const footer = document.createElement('div');
      footer.className = 'gallery-audio-footer';

      const playBtn = document.createElement('button');
      playBtn.className = 'gallery-audio-play';
      playBtn.title = 'Play / Pause';
      playBtn.setAttribute('aria-label', isVn ? 'Play voice message' : 'Play audio');
      playBtn.innerHTML = '<svg class="play-icon" width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg><svg class="pause-icon" width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>';
      playBtn.addEventListener('click', e => {
        e.stopPropagation();
        toggleGalleryAudioPlayback(cell, src);
      });
      footer.appendChild(playBtn);

      const gotoBtn = document.createElement('button');
      gotoBtn.className = 'gallery-goto';
      gotoBtn.title = 'Go to message';
      gotoBtn.textContent = '→ in chat';
      gotoBtn.addEventListener('click', e => {
        e.stopPropagation();
        if (gotoBtn.disabled) return;
        gotoBtn.disabled = true;
        gotoBtn.textContent = '⏳ in chat';
        if (currentGalleryAudio) {
          currentGalleryAudio.pause();
          currentGalleryAudio = null;
          currentGalleryAudioCell = null;
        }
        closeMediaGallery();
        jumpToMessage(msg, 'default');
      });
      footer.appendChild(gotoBtn);
      cell.appendChild(footer);

      cell.addEventListener('click', () => {
        toggleGalleryAudioPlayback(cell, src);
      });
    } else if (mt === 'link') {
      const urlMatch = (msg.text_body || '').match(/https?:\/\/[^\s<>"]+/);
      const rawUrl = msg.link_url || (urlMatch ? urlMatch[0] : '') || msg.text_body || '';
      const url = rawUrl.replace(/[.,;:!?)'"]+$/, '');
      const domain = extractDomain(url);

      cell.classList.add('gallery-link-card', 'gallery-rich-link-card');

      const mediaBox = document.createElement('div');
      mediaBox.className = 'gallery-rich-link-media';

      if (msg.has_thumbnail && msg.msg_id) {
        cell.dataset.src = '/api/thumbnail/' + msg.msg_id;
        cell.dataset.hydrated = 'false';
        const img = document.createElement('img');
        img.className = 'gallery-thumb gallery-rich-link-thumb';
        img.src = EMPTY_GALLERY_PIX;
        img.alt = '';
        mediaBox.appendChild(img);
      } else {
        mediaBox.innerHTML = '<span class="gallery-rich-link-fallback-icon">🔗</span>';
      }
      cell.appendChild(mediaBox);

      const body = document.createElement('div');
      body.className = 'gallery-rich-link-body';

      if (domain) {
        const domEl = document.createElement('div');
        domEl.className = 'gallery-rich-link-domain';
        domEl.textContent = domain;
        body.appendChild(domEl);
      }

      const titleText = msg.link_title || domain || url || 'Link';
      const titleEl = document.createElement('a');
      titleEl.className = 'gallery-rich-link-title' + (!msg.link_desc ? ' no-desc' : '');
      titleEl.href = url || '#';
      titleEl.target = '_blank';
      titleEl.rel = 'noopener noreferrer';
      titleEl.title = titleText;
      titleEl.textContent = titleText;
      titleEl.setAttribute('aria-label', 'Open link: ' + titleText);
      body.appendChild(titleEl);

      if (msg.link_desc) {
        const descEl = document.createElement('div');
        descEl.className = 'gallery-rich-link-desc';
        descEl.title = msg.link_desc;
        descEl.textContent = msg.link_desc;
        body.appendChild(descEl);
      }

      const metaEl = document.createElement('div');
      metaEl.className = 'gallery-rich-link-meta';
      const dateStr = new Date(msg.timestamp_ms).toLocaleDateString(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric'
      });
      const senderStr = msg.sender || 'You';
      const metaText = senderStr + ' · ' + dateStr + (url ? ' · ' + url : '');
      metaEl.textContent = metaText;
      metaEl.title = metaText;
      body.appendChild(metaEl);

      cell.appendChild(body);
      cell.addEventListener('click', e => {
        if (!e.target.closest('a') && !e.target.closest('.gallery-goto')) {
          if (url && url !== '#') window.open(url, '_blank');
        }
      });
    } else if (mt === 'document') {
      cell.classList.add('gallery-link-card');
      const name = document.createElement('div');
      name.className = 'gallery-link-url';
      name.textContent = '📄 ' + (cleanName || 'file');
      const meta = document.createElement('div');
      meta.className = 'gallery-link-meta';
      meta.textContent = (msg.sender || 'You') + ' · ' + new Date(msg.timestamp_ms).toLocaleDateString(undefined, {year:'numeric', month:'short', day:'numeric'});
      cell.appendChild(name);
      cell.appendChild(meta);
      cell.addEventListener('click', () => window.open(src, '_blank'));
    } else if (mt === 'vcard') {
      const d = document.createElement('div');
      d.className = 'gallery-doc';
      d.innerHTML = '<span style="font-size:28px">📇</span><span>' + esc(cleanName || msg.text_body || 'Contact') + '</span>';
      d.addEventListener('click', () => {
        if (msg.archive_path) {
          window.open(src, '_blank');
        } else {
          closeMediaGallery();
          jumpToMessage(msg, 'default');
        }
      });
      cell.appendChild(d);
    } else {
      const d = document.createElement('div');
      d.className = 'gallery-doc';
      d.innerHTML = '<span style="font-size:28px">📄</span><span>' + esc(cleanName || 'file') + '</span>';
      d.addEventListener('click', () => window.open(src, '_blank'));
      cell.appendChild(d);
    }

    const btn = document.createElement('button');
    btn.className = 'gallery-goto';
    btn.title = 'Go to message';
    btn.textContent = '→ in chat';
    btn.addEventListener('click', e => {
      e.stopPropagation();
      if (btn.disabled) return;
      btn.disabled = true;
      btn.textContent = '⏳ in chat';
      closeMediaGallery();
      jumpToMessage(msg, 'default');
    });
    cell.appendChild(btn);
    return cell;
  }

  function _insertGalleryGridBatch(grid, items) {
    if (!grid || !items || !items.length) return;
    const observer = getGalleryMediaObserver();
    const linkObserver = getGalleryLinkObserver();
    const cellsToObserve = [];
    const linkCellsToObserve = [];

    // Fast check: pure tail append if items are strictly older than the last cell in grid
    const existingHeaders = [...grid.querySelectorAll('.gallery-month-header')];
    const oldestHeader = existingHeaders[existingHeaders.length - 1];
    const oldestHeaderMk = oldestHeader ? parseInt(oldestHeader.dataset.month, 10) : null;

    const allCells = grid.querySelectorAll('.gallery-item');
    const lastCell = allCells.length ? allCells[allCells.length - 1] : null;
    const oldestTsInGrid = lastCell ? parseInt(lastCell.dataset.ts || '0', 10) : 0;

    const isTailAppend = !lastCell || (items[0].timestamp_ms <= oldestTsInGrid);

    if (isTailAppend) {
      const fragment = document.createDocumentFragment();
      let currentMk = oldestHeader ? oldestHeaderMk : null;

      for (const msg of items) {
        const msgId = msg.msg_id || msg.id;
        if (msgId) {
          const existing = grid.querySelector(`.gallery-item[data-msg-id="${msgId}"]`);
          if (existing) {
            if (!msg._prepopulated && existing.dataset.prepopulated === 'true') {
              delete existing.dataset.prepopulated;
            }
            if (activeTypes.size > 0) {
              existing.classList.toggle('type-hidden', !activeTypes.has(existing.dataset.type));
            } else {
              existing.classList.remove('type-hidden');
            }
            continue;
          }
        }

        const mk = monthKey(msg.timestamp_ms);
        if (mk !== currentMk) {
          currentMk = mk;
          if (!grid.querySelector(`.gallery-month-header[data-month="${mk}"]`)) {
            const monthHdr = document.createElement('div');
            monthHdr.className = 'gallery-month-header';
            monthHdr.dataset.month = String(mk);
            monthHdr.textContent = fmtMonthHeader(msg.timestamp_ms);
            fragment.appendChild(monthHdr);
          }
        }

        const cell = renderGalleryItem(msg);
        if (msg._prepopulated) {
          cell.dataset.prepopulated = 'true';
        }
        if (activeTypes.size > 0 && !activeTypes.has(cell.dataset.type || msg.media_type)) {
          cell.classList.add('type-hidden');
        }
        fragment.appendChild(cell);
        if (cell.dataset.src) {
          if (cell.dataset.type === 'link') {
            linkCellsToObserve.push(cell);
          } else {
            cellsToObserve.push(cell);
          }
        }
      }

      grid.insertBefore(fragment, gallerySentinel || null);
    } else {
      // Interleaved items fallback
      for (const msg of items) {
        _insertGalleryGridItem(grid, msg);
      }
    }

    if (observer) {
      for (const c of cellsToObserve) {
        observer.observe(c);
      }
    }
    if (linkObserver) {
      for (const c of linkCellsToObserve) {
        linkObserver.observe(c);
      }
    }
  }

  function _insertGalleryGridItem(grid, msg) {
    const isPrepopulated = Boolean(msg && msg._prepopulated);
    const msgId = msg.msg_id || msg.id;
    if (msgId) {
      const existing = grid.querySelector(`.gallery-item[data-msg-id="${msgId}"]`);
      if (existing) {
        if (!isPrepopulated && existing.dataset.prepopulated === 'true') {
          delete existing.dataset.prepopulated;
        }
        if (activeTypes.size > 0) {
          existing.classList.toggle('type-hidden', !activeTypes.has(existing.dataset.type));
        } else {
          existing.classList.remove('type-hidden');
        }
        return;
      }
    }

    const mk = monthKey(msg.timestamp_ms);
    let monthHdr = grid.querySelector(`.gallery-month-header[data-month="${mk}"]`);
    if (!monthHdr) {
      monthHdr = document.createElement('div');
      monthHdr.className = 'gallery-month-header';
      monthHdr.dataset.month = String(mk);
      monthHdr.textContent = fmtMonthHeader(msg.timestamp_ms);
      const olderHdr = [...grid.querySelectorAll('.gallery-month-header')]
        .find(el => parseInt(el.dataset.month, 10) < mk);
      grid.insertBefore(monthHdr, olderHdr || gallerySentinel || null);
    }

    const cell = renderGalleryItem(msg);
    if (isPrepopulated) {
      cell.dataset.prepopulated = 'true';
    }
    if (activeTypes.size > 0 && !activeTypes.has(cell.dataset.type || msg.media_type)) {
      cell.classList.add('type-hidden');
    }

    const nextSection = [...grid.querySelectorAll('.gallery-month-header')]
      .find(el => parseInt(el.dataset.month, 10) < mk) || gallerySentinel || null;

    let insertBeforeEl = nextSection;
    let curr = monthHdr.nextElementSibling;
    while (curr && curr !== nextSection) {
      if (curr.classList.contains('gallery-item')) {
        const currTs = parseInt(curr.dataset.ts || '0', 10);
        if (currTs < msg.timestamp_ms) {
          insertBeforeEl = curr;
          break;
        }
      }
      curr = curr.nextElementSibling;
    }
    grid.insertBefore(cell, insertBeforeEl);
    if (cell.dataset.src) {
      if (cell.dataset.type === 'link') {
        getGalleryLinkObserver()?.observe(cell);
      } else {
        getGalleryMediaObserver()?.observe(cell);
      }
    }
  }

  async function openMediaGallery() {
    if (!currentChat) return;
    closeChatInfo();
    closeChatStarred();
    const grid = document.getElementById('media-gallery-grid');
    const stats = document.getElementById('media-gallery-stats');
    const switcher = document.getElementById('media-view-switcher');

    const gen = ++galleryOpenGen;
    const requestedChatId = currentChat.id;

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
    stats.innerHTML = '';
    document.getElementById('media-gallery').classList.add('open');

    // reset pagination state
    currentGalleryItems = [];
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

    try {
      const typeOrder = ['image', 'video', 'voice', 'audio', 'gif', 'sticker', 'document', 'vcard', 'link'];
      const qs = 'chat_id=' + encodeURIComponent(currentChat.id) + '&chat_type=' + encodeURIComponent(currentChat.type);

      // Optimistically default to images + videos (filtering out any DEFAULT_DESELECTED_TYPES)
      activeTypes = new Set(['image', 'video'].filter(t => !DEFAULT_DESELECTED_TYPES.has(t)));
      defaultActiveTypes = new Set(activeTypes);
      isDefaultView = true;

      grid.innerHTML = '';

      // attach IntersectionObserver sentinel at bottom of grid
      gallerySentinel = document.createElement('div');
      gallerySentinel.className = 'gallery-sentinel';
      gallerySentinel.style.height = '1px';
      grid.appendChild(gallerySentinel);
      galleryObserver = new IntersectionObserver(entries => {
        if (entries[0].isIntersecting && !galleryLoadingMore && galleryScrollDirection === 'down') {
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
      const countPromise = fetch('/api/media/count?' + qs, { signal: sessionController.signal })
        .then(r => r.ok ? r.json() : null)
        .catch(err => {
          if (err.name !== 'AbortError') console.error('Failed to fetch media count:', err);
          return null;
        });

      // Render page 1 immediately
      await _loadGalleryPage(null);

      if (gen !== galleryOpenGen || !currentChat || currentChat.id !== requestedChatId) return;
      if (!document.getElementById('media-gallery').classList.contains('open')) return;

      // Handle counts when ready
      const counts = await countPromise;
      if (gen !== galleryOpenGen || !currentChat || currentChat.id !== requestedChatId) return;
      if (!document.getElementById('media-gallery').classList.contains('open')) return;

      if (counts) {
        currentGalleryStats = counts;
        stats.innerHTML = '';
        if (counts.total) {
          const totalMissing = Object.values(counts.by_type || {}).reduce((acc, d) => acc + (d.missing || 0), 0);
          stats.appendChild(_makeStatPill('total', counts.total, null, totalMissing));
          for (const t of typeOrder) {
            const d = counts.by_type[t];
            if (!d) continue;
            stats.appendChild(_makeStatPill(t, d.count, t, d.missing));
          }

          // If chat has no images or videos, switch to first available type
          const hasImagesOrVideos = (counts.by_type['image']?.count || 0) + (counts.by_type['video']?.count || 0) > 0;
          if (!hasImagesOrVideos && counts.total > 0) {
            const firstAvailable = typeOrder.find(t => counts.by_type[t]?.count > 0);
            if (firstAvailable) {
              activeTypes = new Set([firstAvailable]);
              defaultActiveTypes = new Set([firstAvailable]);
              _syncStatPills();
              await _loadGalleryPage(null);
            }
          } else {
            _syncStatPills();
          }
        }
      }

      if (!currentGalleryItems.length && !sessionController.signal.aborted) {
        grid.innerHTML = '<div class="gallery-empty-notice">No media in this chat.</div>';
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
      pills.forEach(p => p.classList.remove('is-loading'));
      return;
    }
    const currentType = activeTypes.size === 1 ? [...activeTypes][0] : (activeTypes.size === 0 ? 'total' : null);
    pills.forEach(p => {
      if (currentType && p.dataset.type === currentType) {
        p.classList.add('is-loading');
      } else if (!currentType && activeTypes.has(p.dataset.type)) {
        p.classList.add('is-loading');
      } else {
        p.classList.remove('is-loading');
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

    if ((totalExpected === 0 && type) || (isAllDone && visibleCount === 0)) {
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
    _setStatPillsLoading(true);
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

      _insertGalleryGridBatch(grid, items);

      if (archiveViewActive) {
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
      scheduleVisibleVideoExtraction();

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
            buildArchiveView(currentGalleryItems, activeArchiveDir());
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

    if (grid) {
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
    _ensureMediaLoadedForViewport(targetTs, anchorEl, anchorTopBefore, gen);
    if (activeTypes.has('link')) {
      _checkGalleryLoadMore();
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
          visible = block._messages.filter(m => activeTypes.has(m.media_type)).length;
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

  // ---- sidebar search -------------------------------------------------------

  let searchTimer = null;
  const searchInputEl = document.getElementById('search-input');
  const searchClearBtn = document.getElementById('search-clear');

  function updateSidebarSearchClearBtn() {
    if (searchClearBtn && searchInputEl) {
      searchClearBtn.style.display = searchInputEl.value ? 'flex' : 'none';
    }
  }

  function clearActiveSidebarSearch() {
    if (searchInputEl) {
      searchInputEl.value = '';
    }
    updateSidebarSearchClearBtn();
    if (currentNavTab === 'chats') {
      clearSearchResults();
    } else if (currentNavTab === 'calls') {
      loadCallsList(true);
    } else if (currentNavTab === 'starred') {
      loadStarredList(true);
    } else if (currentNavTab === 'archived') {
      renderArchivedList('');
    }
    if (searchInputEl) {
      searchInputEl.focus();
    }
  }

  if (searchClearBtn) {
    searchClearBtn.addEventListener('click', clearActiveSidebarSearch);
  }

  if (searchInputEl) {
    searchInputEl.addEventListener('input', function () {
      clearTimeout(searchTimer);
      updateSidebarSearchClearBtn();
      const q = this.value.trim();

      if (currentNavTab === 'chats') {
        if (!q) {
          clearSearchResults();
          return;
        }
        searchTimer = setTimeout(() => doSearch(q), 300);
      } else if (currentNavTab === 'calls') {
        searchTimer = setTimeout(() => loadCallsList(true), 250);
      } else if (currentNavTab === 'starred') {
        searchTimer = setTimeout(() => loadStarredList(true), 250);
      } else if (currentNavTab === 'archived') {
        renderArchivedList(q);
      }
    });

    searchInputEl.addEventListener('search', function () {
      updateSidebarSearchClearBtn();
      if (!this.value.trim()) {
        clearActiveSidebarSearch();
      }
    });
  }

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
      const el2 = _findChatListItem(chat.id, chat.type);
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
      <div class="sr-text">${highlight(resolveMentionsPlainText(snippet, r.mentions), q)}</div>
      <div class="sr-time">${fmtTime(r.timestamp_ms)}</div>
    `;
    el.addEventListener('click', async () => {
      document.getElementById('search-input').value = '';
      clearSearchResults();
      const chat = allChats.find(c => c.id === r.chat_id && c.type === r.chat_type);
      if (chat) {
        resetFilterTo('all');
        const el2 = _findChatListItem(r.chat_id, r.chat_type);
        if (el2) {
          await selectChat(chat, el2);
          expandMessageRowById(r.msg_id, null);
          const row = await jumpToTimestamp(r.timestamp_ms, null, true, 'default');
          if (row) {
            expandMessageRowById(row.dataset.msgId, row);
            const bubble = row.querySelector('.msg-bubble, .msg-service-bubble');
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
    const existing = rowForMsgId(result.msg_id);
    expandMessageRowById(result.msg_id, existing);
    if (existing) {
      highlightMessageRow(existing);
      return;
    }
    await jumpToTimestamp(result.timestamp_ms, result.msg_id, true, 'default');
    const loaded = rowForMsgId(result.msg_id);
    if (loaded) highlightMessageRow(loaded);
  }

  function highlightMessageRow(row) {
    const bubble = row.querySelector('.msg-bubble, .msg-service-bubble');
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
      await jumpToTimestamp(new Date(val).getTime(), null, false, 'default');
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

  // Scroll to bottom and hold it there as lazy media loads and shifts layout.
  // Re-snaps whenever images finish loading or layout dimensions change,
  // until layout holds steady or 1500 ms have passed. Bails immediately if a newer load superseded this one
  // or if the user manually initiates scrolling.
  async function _pinToBottom(gen) {
    const scroll = document.getElementById('message-scroll');
    if (!scroll) return;

    const snap = () => {
      suppressScroll = true;
      scroll.scrollTop = scroll.scrollHeight;
      suppressScroll = false;
    };
    snap();

    let done = false;
    let ro = null;
    const cleanup = () => {
      if (done) return;
      done = true;
      if (ro) ro.disconnect();
      scroll.removeEventListener('wheel', onUserScroll, { capture: true });
      scroll.removeEventListener('touchstart', onUserScroll, { capture: true });
      scroll.removeEventListener('pointerdown', onUserScroll, { capture: true });
      pendingImgs.forEach(img => {
        img.removeEventListener('load', onMediaLoad);
        img.removeEventListener('error', onMediaLoad);
      });
    };

    const onUserScroll = () => {
      isUserAtBottom = (scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight) < 100;
      cleanup();
    };

    scroll.addEventListener('wheel', onUserScroll, { capture: true, passive: true });
    scroll.addEventListener('touchstart', onUserScroll, { capture: true, passive: true });
    scroll.addEventListener('pointerdown', onUserScroll, { capture: true, passive: true });

    const pendingImgs = [...scroll.querySelectorAll('img')].filter(img => !img.complete);
    let remaining = pendingImgs.length;
    const onMediaLoad = () => {
      if (done || gen !== _loadGen) return;
      snap();
      remaining--;
      if (remaining <= 0) {
        requestAnimationFrame(() => {
          if (done || gen !== _loadGen) return;
          snap();
          cleanup();
        });
      }
    };
    pendingImgs.forEach(img => {
      img.addEventListener('load', onMediaLoad, { once: true });
      img.addEventListener('error', onMediaLoad, { once: true });
    });

    if (window.ResizeObserver) {
      ro = new ResizeObserver(() => {
        if (done || gen !== _loadGen) return;
        snap();
      });
      ro.observe(scroll);
    }

    if (pendingImgs.length === 0) {
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
      if (gen === _loadGen) {
        snap();
      }
      cleanup();
      return;
    }

    const start = performance.now();
    while (!done && performance.now() - start < 1500) {
      if (gen !== _loadGen) {
        cleanup();
        return;
      }
      await new Promise(r => requestAnimationFrame(r));
    }
    cleanup();
  }

  // Center targetEl and hold it there as lazy media above it loads and shifts layout.
  // Re-centers whenever images finish loading or layout dimensions change,
  // until layout holds steady or 1500 ms have passed. Bails immediately if a newer load superseded this one.
  async function _pinToMsg(targetEl, gen) {
    const scroll = document.getElementById('message-scroll');
    if (!scroll || !targetEl) return;

    const recenter = () => {
      suppressScroll = true;
      targetEl.scrollIntoView({ behavior: 'instant', block: 'center' });
      suppressScroll = false;
    };
    recenter();

    let done = false;
    let ro = null;
    const cleanup = () => {
      if (done) return;
      done = true;
      if (ro) ro.disconnect();
      scroll.removeEventListener('wheel', onUserScroll, { capture: true });
      scroll.removeEventListener('touchstart', onUserScroll, { capture: true });
      scroll.removeEventListener('pointerdown', onUserScroll, { capture: true });
      pendingImgs.forEach(img => {
        img.removeEventListener('load', onMediaLoad);
        img.removeEventListener('error', onMediaLoad);
      });
    };

    const onUserScroll = () => {
      cleanup();
    };

    const onMediaLoad = () => {
      if (done || gen !== _loadGen) return;
      recenter();
    };

    scroll.addEventListener('wheel', onUserScroll, { capture: true, passive: true });
    scroll.addEventListener('touchstart', onUserScroll, { capture: true, passive: true });
    scroll.addEventListener('pointerdown', onUserScroll, { capture: true, passive: true });

    const pendingImgs = Array.from(scroll.querySelectorAll('img')).filter(img => !img.complete);
    pendingImgs.forEach(img => {
      img.addEventListener('load', onMediaLoad, { once: true });
      img.addEventListener('error', onMediaLoad, { once: true });
    });

    if (typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(() => {
        if (done || gen !== _loadGen) return;
        recenter();
      });
      ro.observe(scroll);
      if (targetEl) ro.observe(targetEl);
    }

    const deadline = performance.now() + 1500;
    while (!done && performance.now() < deadline) {
      await new Promise(r => requestAnimationFrame(r));
      if (gen !== _loadGen) {
        cleanup();
        return;
      }
      if (pendingImgs.length === 0 || pendingImgs.every(img => img.complete)) {
        if (performance.now() > deadline - 1000) {
          recenter();
          break;
        }
      }
    }
    cleanup();
    recenter();
  }

  let _jumpHudTimeout = null;
  let _jumpAbortController = null;

  function updateJumpHudPosition() {
    const hud = document.getElementById('chat-jump-hud');
    const scroll = document.getElementById('message-scroll');
    if (!hud) return;

    let topOffset = scroll ? scroll.offsetTop : 0;
    if (topOffset <= 0) {
      const header = document.getElementById('chat-header');
      const pinned = document.getElementById('pinned-banner');
      const context = document.getElementById('context-jump-banner');
      topOffset = 0;
      if (header && header.style.display !== 'none') topOffset += header.offsetHeight;
      if (pinned && pinned.style.display !== 'none') topOffset += pinned.offsetHeight;
      if (context && context.style.display !== 'none') topOffset += context.offsetHeight;
    }

    const finalTop = topOffset > 0 ? (topOffset + 12) : 72;
    hud.style.top = `${finalTop}px`;
  }

  window.addEventListener('resize', () => {
    const hud = document.getElementById('chat-jump-hud');
    if (hud && hud.classList.contains('visible')) {
      updateJumpHudPosition();
    }
  });

  function showJumpHud(text) {
    const hud = document.getElementById('chat-jump-hud');
    const label = document.getElementById('chat-jump-hud-text');
    if (!hud || !label) return;
    if (_jumpHudTimeout) {
      clearTimeout(_jumpHudTimeout);
      _jumpHudTimeout = null;
    }
    updateJumpHudPosition();
    hud.classList.remove('error');
    label.textContent = text;
    hud.classList.add('visible');
  }

  function hideJumpHud() {
    const hud = document.getElementById('chat-jump-hud');
    if (!hud) return;
    if (_jumpHudTimeout) {
      clearTimeout(_jumpHudTimeout);
      _jumpHudTimeout = null;
    }
    hud.classList.remove('visible', 'error');
  }

  function showJumpHudError(errorText) {
    const hud = document.getElementById('chat-jump-hud');
    const label = document.getElementById('chat-jump-hud-text');
    if (!hud || !label) return;
    if (_jumpHudTimeout) {
      clearTimeout(_jumpHudTimeout);
    }
    updateJumpHudPosition();
    hud.classList.add('error', 'visible');
    label.textContent = errorText;
    _jumpHudTimeout = setTimeout(() => {
      hud.classList.remove('visible', 'error');
      _jumpHudTimeout = null;
    }, 3000);
  }

  // exactMsgId: when the caller needs a specific message in the DOM (e.g. jumpToMessage,
  // chat search), pass its msg_id to bypass the nearest-ts early-exit.
  // isExactTarget: true when jumping to a specific message or quote (tightens threshold to 1500ms
  // instead of the 24-hour date-picker window).
  async function jumpToTimestamp(ts, exactMsgId, isExactTarget, highlightType) {
    if (exactMsgId == null) {
      const near = nearestMsgToTs(ts);
      const threshold = isExactTarget ? 1500 : 86400000;
      const existing = near && Math.abs(near.timestamp_ms - ts) <= threshold && rowForMsgId(near.msg_id);
      if (existing) {
        const gen = ++_loadGen;
        isUserAtBottom = false;
        const loadingEl = document.getElementById('chat-loading');
        if (loadingEl) loadingEl.style.display = 'none';
        const btn = document.getElementById('scroll-to-bottom');
        if (btn) btn.classList.add('visible');
        await _pinToMsg(existing, gen);
        if (isExactTarget) flashHighlightRow(existing, highlightType);
        return existing;
      }
    }

    if (_jumpAbortController) {
      _jumpAbortController.abort();
    }
    _jumpAbortController = new AbortController();
    const abortSignal = _jumpAbortController.signal;

    const gen = ++_loadGen;
    loading = true;
    isUserAtBottom = false;
    const scroll = document.getElementById('message-scroll');
    if (scroll) scroll.classList.add('is-navigating');

    const dateLabel = ts ? fmtDateSep(ts) : '';
    const hudText = dateLabel ? `Jumping to message · ${dateLabel}` : 'Jumping to message…';
    showJumpHud(hudText);

    try {
      const params = new URLSearchParams({
        chat_id: currentChat.id,
        chat_type: currentChat.type,
        limit: 50
      });
      if (ts != null) params.set('ts', ts);
      if (exactMsgId != null) params.set('msg_id', exactMsgId);
      const res = await fetch('/api/messages/at?' + params, { signal: abortSignal });
      if (!res.ok) {
        if (gen === _loadGen) {
          showJumpHudError(`Failed to load message (${res.status})`);
        }
        throw new Error(`Server returned HTTP ${res.status} when loading messages at timestamp ${ts}`);
      }
      const msgs = await res.json();
      if (gen !== _loadGen) return null;
      if (!msgs.length) {
        showJumpHudError('Message not found');
        return null;
      }

      msgList = [];
      noMoreOlder = false;
      noMoreNewer = false;
      mergePage(msgs);
      renderWindow();
      const loadingEl = document.getElementById('chat-loading');
      if (loadingEl) loadingEl.style.display = 'none';
      const btn = document.getElementById('scroll-to-bottom');
      if (btn) btn.classList.add('visible');

      let targetEl = exactMsgId != null ? rowForMsgId(exactMsgId) : null;
      if (!targetEl) {
        const near = nearestMsgToTs(ts);
        targetEl = near ? rowForMsgId(near.msg_id) : null;
      }
      if (targetEl) {
        await _pinToMsg(targetEl, gen);
        if (isExactTarget) flashHighlightRow(targetEl, highlightType);
      }
      if (gen !== _loadGen) return null;
      hideJumpHud();
      return targetEl || null;
    } catch (err) {
      if (err.name === 'AbortError') {
        return null;
      }
      if (gen === _loadGen) {
        showJumpHudError('Unable to load message');
      }
      throw err;
    } finally {
      if (scroll) scroll.classList.remove('is-navigating');
      if (gen === _loadGen) {
        loading = false;
        const loadingEl = document.getElementById('chat-loading');
        if (loadingEl) loadingEl.style.display = 'none';
      }
    }
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
    const panel = document.getElementById('chat-info-panel');
    if (panel) {
      panel.classList.remove('open');
      panel.style.display = 'none';
    }
    const desc = document.getElementById('chat-info-desc');
    if (desc) {
      desc.textContent = '';
      desc.style.display = 'none';
    }
  }

  async function openChatInfo() {
    if (!currentChat) return;
    closeMediaGallery();
    closeChatStarred();
    const panel = document.getElementById('chat-info-panel');
    const heading = document.getElementById('chat-info-header-heading');
    const avatar = document.getElementById('chat-info-avatar');
    const title = document.getElementById('chat-info-title');
    const phone = document.getElementById('chat-info-phone');
    const desc  = document.getElementById('chat-info-desc');
    const details = document.getElementById('chat-info-details');
    const closeBtn = document.getElementById('chat-info-close');
    if (!panel || !details) return;

    if (heading) {
      heading.textContent = currentChat.type === 'group' ? 'Group Info' : 'Contact Info';
    }
    if (avatar) {
      const avatarKey = currentChat.phone_number || currentChat.id || currentChat.display_name;
      avatar.style.backgroundColor = getAvatarColor(avatarKey);
      avatar.textContent = getInitials(currentChat.display_name);
    }
    if (title) {
      title.textContent = currentChat.display_name || '';
    }
    if (phone) {
      if (currentChat.type === 'contact') {
        const fmtPhone = formatPhoneNumber(currentChat.phone_number || currentChat.id);
        phone.textContent = fmtPhone ? fmtPhone : '';
      } else {
        phone.textContent = 'Group conversation';
      }
    }
    if (desc) {
      desc.textContent = '';
      desc.style.display = 'none';
    }
    details.innerHTML = '';

    // Show loading skeleton
    const loadingRow = document.createElement('div');
    loadingRow.className = 'chat-info-row';
    loadingRow.appendChild(_spinner());
    details.appendChild(loadingRow);

    panel.style.display = 'flex';
    panel.classList.add('open');
    if (closeBtn) closeBtn.focus();

    const params = new URLSearchParams({ chat_id: currentChat.id, chat_type: currentChat.type });
    const chatAtOpen = currentChat;

    // Kick off both requests in parallel
    const sizePromise = fetch('/api/chat-info/media-size?' + params)
      .then(r => r.ok ? r.json() : { bytes: 0 })
      .catch(() => ({ bytes: 0 }));

    let info;
    try {
      const infoResp = await fetch('/api/chat-info?' + params);
      info = await infoResp.json();
    } catch (_) {
      if (currentChat === chatAtOpen && panel.classList.contains('open')) {
        details.innerHTML = '<div class="chat-info-row" style="color:var(--text-muted)">Could not load chat info.</div>';
      }
      return;
    }

    // Panel may have been closed or chat switched while awaiting
    if (currentChat !== chatAtOpen || !panel.classList.contains('open')) return;

    details.innerHTML = '';

    if (currentChat.type === 'group' && info.members && info.members.length) {
      if (phone) {
        phone.textContent = `${info.members.length} participants`;
      }
      const subEl = document.getElementById('chat-subtitle');
      if (subEl) {
        let subHtml = `${info.members.length} participants`;
        if (currentChat.is_preserved_only) {
          subHtml += ' <span class="chat-preserved-tag" title="Preserved from backup • Chat deleted on device">Preserved</span>';
        }
        subEl.innerHTML = subHtml;
      }
    }

    if (currentChat.type === 'group' && info.description && desc) {
      desc.textContent = info.description;
      desc.style.display = 'block';
    }

    const fmtTs = ts => ts ? new Date(ts).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : '—';
    const fmtNum = n => (n === null || n === undefined || n < 0) ? '—' : n.toLocaleString();

    if (currentChat.type === 'contact' && info.number) {
      details.appendChild(_infoRow('Phone', '+' + info.number).row);
    }

    if (currentChat.type === 'group' && info.created_ts) {
      details.appendChild(_infoRow('Group created', fmtTs(info.created_ts)).row);
      if (info.creator_number || info.creator_name) {
        const match = info.creator_number ? (info.members || []).find(m => m.number && m.number === info.creator_number) : null;
        const creatorName = (match && match.name) ? match.name : (info.creator_name || (info.creator_number ? '+' + info.creator_number : ''));
        if (creatorName) {
          details.appendChild(_infoRow('Created by', creatorName).row);
        }
      }
    }

    details.appendChild(_infoRow('Conversation since', fmtTs(info.first_ts)).row);

    if (info.first_ts && info.last_ts && info.last_ts !== info.first_ts) {
      const days = Math.round((info.last_ts - info.first_ts) / 86400000);
      const years = Math.floor(days / 365);
      const months = Math.floor((days % 365) / 30);
      let span = '';
      if (years > 0) span += years + (years === 1 ? ' year' : ' years');
      if (months > 0) span += (span ? ', ' : '') + months + (months === 1 ? ' month' : ' months');
      if (!span) span = days + (days === 1 ? ' day' : ' days');
      details.appendChild(_infoRow('Duration', span).row);
    }

    if (currentChat.type === 'contact') {
      const sentReceived = document.createElement('span');
      sentReceived.textContent = fmtNum(info.sent) + ' sent · ' + fmtNum(info.received) + ' received';
      details.appendChild(_infoRow('Messages', sentReceived).row);
    } else {
      details.appendChild(_infoRow('Total messages', fmtNum(info.total)).row);
    }

    if (info.calls_count) {
      details.appendChild(_infoRow('Calls', fmtNum(info.calls_count)).row);
    }

    // Gallery link row
    const galleryLink = document.createElement('button');
    galleryLink.className = 'chat-info-link';
    galleryLink.textContent = 'Open Media Gallery →';
    galleryLink.addEventListener('click', () => { closeChatInfo(); openMediaGallery(); });
    details.appendChild(_infoRow('Media', galleryLink).row);

    // Media size row
    const sizeRowObj = _infoRow('Media size', 'Calculating…');
    details.appendChild(sizeRowObj.row);
    sizePromise.then(sizeData => {
      if (currentChat === chatAtOpen && panel.classList.contains('open')) {
        sizeRowObj.val.textContent = _formatBytes(sizeData.bytes || 0);
      }
    });

    // Group members
    if (currentChat.type === 'group' && info.members && info.members.length > 0) {
      const SHOW = 10;
      const membersWrap = document.createElement('div');
      membersWrap.className = 'chat-info-members';
      const renderMember = m => {
        const el = document.createElement('div');
        el.className = 'chat-info-member';
        const displayName = m.name || (m.number ? '+' + m.number : '?');
        const numSuffix = m.number && ('+' + m.number) !== displayName ? ' (+' + m.number + ')' : '';
        el.textContent = displayName + numSuffix;
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
      details.appendChild(_infoRow('Members (' + info.members.length + ')', membersWrap).row);
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
      details.appendChild(_infoRow('Top senders', sendersWrap).row);
    }
  }

  function toggleChatInfo() {
    const panel = document.getElementById('chat-info-panel');
    if (panel && panel.classList.contains('open')) {
      closeChatInfo();
    } else {
      openChatInfo();
    }
  }

  document.getElementById('chat-info-btn').addEventListener('click', toggleChatInfo);
  const headerInfo = document.getElementById('chat-header-info');
  if (headerInfo) {
    headerInfo.addEventListener('click', toggleChatInfo);
    headerInfo.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        toggleChatInfo();
      }
    });
  }
  document.getElementById('chat-info-close').addEventListener('click', closeChatInfo);

  // Dismiss open drawers when clicking into the conversation or chat pane outside the drawer
  document.addEventListener('pointerdown', e => {
    const infoDrawer = document.getElementById('chat-info-panel');
    const starredDrawer = document.getElementById('chat-starred-drawer');

    if (infoDrawer && infoDrawer.classList.contains('open')) {
      const infoBtn = document.getElementById('chat-info-btn');
      const headerInfo = document.getElementById('chat-header-info');
      const isTrigger = (infoBtn && infoBtn.contains(e.target)) || (headerInfo && headerInfo.contains(e.target));
      if (!infoDrawer.contains(e.target) && !isTrigger) {
        closeChatInfo();
      }
    }

    if (starredDrawer && starredDrawer.classList.contains('open')) {
      const starredBtn = document.getElementById('chat-starred-btn');
      const isTrigger = starredBtn && starredBtn.contains(e.target);
      if (!starredDrawer.contains(e.target) && !isTrigger) {
        closeChatStarred();
      }
    }
  });

  // ---- chat starred flyout & count badge -----------------------------------

  let _starredBadgePollGen = 0;

  async function updateChatStarredBadge(chatId) {
    const badge = document.getElementById('chat-starred-badge');
    if (!badge) return;
    const gen = ++_starredBadgePollGen;
    try {
      const res = await fetch(`/api/chat/${encodeURIComponent(chatId)}/starred?limit=1`);
      if (gen !== _starredBadgePollGen || !currentChat || String(currentChat.id) !== String(chatId)) return;
      if (!res.ok) {
        badge.style.display = 'none';
        return;
      }
      const data = await res.json();
      const count = data.count || 0;
      if (count > 0) {
        badge.textContent = count > 99 ? '99+' : String(count);
        badge.style.display = 'inline-flex';
      } else {
        badge.style.display = 'none';
      }
    } catch {
      if (gen === _starredBadgePollGen) badge.style.display = 'none';
    }
  }

  function closeChatStarred() {
    const drawer = document.getElementById('chat-starred-drawer');
    if (drawer) {
      drawer.classList.remove('open');
      drawer.style.display = 'none';
      const triggerBtn = document.getElementById('chat-starred-btn');
      if (triggerBtn && document.activeElement && drawer.contains(document.activeElement)) {
        triggerBtn.focus();
      }
    }
  }

  async function openChatStarred() {
    if (!currentChat) return;
    closeChatInfo();
    closeMediaGallery();

    const drawer = document.getElementById('chat-starred-drawer');
    const headerCount = document.getElementById('chat-starred-header-count');
    const body = document.getElementById('chat-starred-body');
    const closeBtn = document.getElementById('chat-starred-close');
    if (!drawer || !body) return;

    drawer.style.display = 'flex';
    drawer.classList.add('open');
    if (closeBtn) closeBtn.focus();
    body.innerHTML = '<div style="color:var(--text-muted);padding:24px;text-align:center"><div class="chat-info-spinner"></div> Loading starred messages…</div>';

    try {
      const res = await fetch(`/api/chat/${encodeURIComponent(currentChat.id)}/starred?limit=100`);
      if (!res.ok) {
        body.innerHTML = '<div class="starred-empty"><span class="starred-empty-icon">⚠️</span><span>Failed to load starred messages</span></div>';
        return;
      }
      const data = await res.json();
      const messages = data.messages || [];
      if (headerCount) {
        headerCount.textContent = String(data.count || messages.length);
      }

      if (messages.length === 0) {
        body.innerHTML = '<div class="starred-empty"><span class="starred-empty-icon">⭐</span><span>No starred messages in this chat</span></div>';
        return;
      }

      body.innerHTML = '';
      const subheader = document.createElement('div');
      subheader.className = 'chat-starred-subheader';
      subheader.textContent = `${messages.length} starred message${messages.length === 1 ? '' : 's'} in this conversation:`;
      body.appendChild(subheader);

      messages.forEach(m => {
        const card = document.createElement('div');
        card.className = 'starred-card';
        card.setAttribute('tabindex', '0');
        const rawSender = m.sender_name || '';
        const cleanSender = _isUnreadableToken(rawSender) ? '' : rawSender;
        const senderDisplay = cleanSender || 'Unknown';
        card.setAttribute('aria-label', `Starred message from ${senderDisplay}`);

        const topRow = document.createElement('div');
        topRow.className = 'starred-card-top';

        const metaEl = document.createElement('span');
        metaEl.className = 'starred-card-meta';
        const dateStr = fmtDateSep(m.timestamp * 1000);
        metaEl.textContent = `${senderDisplay} (${dateStr})`;

        const chevron = document.createElement('span');
        chevron.className = 'starred-card-chevron';
        chevron.textContent = '›';

        topRow.appendChild(metaEl);
        topRow.appendChild(chevron);

        const snippetEl = document.createElement('div');
        snippetEl.className = 'starred-card-snippet';
        const formattedPreview = _formatStarredPreview(m);
        snippetEl.textContent = formattedPreview ? `"${formattedPreview}"` : '…';

        card.appendChild(topRow);
        card.appendChild(snippetEl);

        card.addEventListener('click', () => {
          jumpToMessage({ msg_id: m.id, timestamp_ms: m.timestamp * 1000 }, 'gold');
        });
        card.addEventListener('keydown', e => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            card.click();
          }
        });

        body.appendChild(card);
      });
    } catch {
      body.innerHTML = '<div class="starred-empty"><span class="starred-empty-icon">⚠️</span><span>Unable to load starred messages</span></div>';
    }
  }

  function toggleChatStarred() {
    const drawer = document.getElementById('chat-starred-drawer');
    if (drawer && drawer.classList.contains('open')) {
      closeChatStarred();
    } else {
      openChatStarred();
    }
  }

  const starredBtn = document.getElementById('chat-starred-btn');
  if (starredBtn) {
    starredBtn.addEventListener('click', toggleChatStarred);
  }
  const starredCloseBtn = document.getElementById('chat-starred-close');
  if (starredCloseBtn) {
    starredCloseBtn.addEventListener('click', closeChatStarred);
  }
  const starredDrawer = document.getElementById('chat-starred-drawer');
  if (starredDrawer) {
    starredDrawer.addEventListener('keydown', e => {
      if (e.key === 'Tab') {
        const focusable = Array.from(starredDrawer.querySelectorAll('button, [tabindex="0"]')).filter(
          el => el.offsetParent !== null
        );
        if (focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    });
  }

  // ---- pinned message banner -----------------------------------------------

  const _RAW_STANZA_ID_RE = /^(?:3EB0[0-9A-Za-z_-]{4,60}|[0-9A-Fa-f]{16,64}|[0-9]{15,20}[-_][0-9]+)$/;
  const _BASE64_TOKEN_RE = /^(?:[A-Za-z0-9+/]{6,}={1,2}|[A-Za-z0-9+/]{16,}={0,2}|C[A-Za-z0-9+/]{6,}={0,2})$/;
  const _LID_RE = /^[0-9]+@lid$/;

  function _isRawStanzaId(str) {
    if (!str || typeof str !== 'string') return false;
    return _RAW_STANZA_ID_RE.test(str.trim());
  }

  function _isUnreadableToken(str) {
    if (!str || typeof str !== 'string') return false;
    const s = str.trim();
    return _RAW_STANZA_ID_RE.test(s) || _BASE64_TOKEN_RE.test(s) || _LID_RE.test(s);
  }

  function _formatMessagePreview(m) {
    let preview = m.text || m.text_body || '';
    if (m.mentions && typeof resolveMentionsPlainText === 'function') {
      preview = resolveMentionsPlainText(preview, m.mentions);
    }
    if (_isUnreadableToken(preview)) {
      preview = '';
    }
    const mt = (m.media_type || '').toLowerCase();
    if (mt) {
      if (mt === 'photo' || mt === 'image') {
        return preview ? `📷 Photo • ${preview}` : '📷 Photo';
      }
      if (mt === 'video') {
        const durStr = m.duration > 0 ? ` (${fmtDuration(m.duration)})` : '';
        const cap = preview ? ` • ${preview}` : '';
        return `📹 Video${durStr}${cap}`;
      }
      if (mt === 'audio') {
        const durStr = m.duration > 0 ? ` (${fmtDuration(m.duration)})` : '';
        return `🎤 Voice message${durStr}`;
      }
      if (mt === 'document') {
        const rawFname = m.file_name || '';
        const cleanFname = _isUnreadableToken(rawFname) ? '' : rawFname;
        const cleanPreview = _isUnreadableToken(preview) ? '' : preview;
        if (cleanFname && cleanPreview && cleanFname !== cleanPreview) {
          return `📄 ${cleanFname} • ${cleanPreview}`;
        }
        const display = cleanFname || cleanPreview || 'Document';
        return `📄 ${display}`;
      }
      if (mt === 'location') {
        const rawLoc = m.place_name || preview;
        const loc = _isUnreadableToken(rawLoc) ? '' : rawLoc;
        return loc ? `📍 Location • ${loc}` : '📍 Location';
      }
      if (mt === 'live_location') {
        return '📍 Live location';
      }
      if (mt === 'vcard') {
        const rawContact = preview || 'Contact';
        const contact = _isUnreadableToken(rawContact) ? 'Contact' : rawContact;
        return `👤 ${contact}`;
      }
      if (mt === 'sticker') {
        return '🎭 Sticker';
      }
      if (mt === 'poll') {
        return preview ? `📊 Poll • ${preview}` : '📊 Poll';
      }
      if (mt === 'gif') {
        return preview ? `🎞 GIF • ${preview}` : '🎞 GIF';
      }
      if (mt === 'deleted') {
        return '🚫 This message was deleted';
      }
      if (mt === 'link') {
        return preview ? `🔗 ${preview}` : '🔗 Link';
      }
      const label = MEDIA_LABELS[mt] || '📎 Media';
      return preview ? `${label} • ${preview}` : label;
    }
    return preview || (m.is_pinned ? 'Pinned message' : 'Message');
  }

  function _formatStarredPreview(m) {
    return _formatMessagePreview(m);
  }

  const _dismissedPins = new Set();
  const _chatPinsCache = new Map();
  let _chatPins = [];
  let _currentPinIndex = 0;
  let _pinnedBannerPollGen = 0;

  async function loadChatPinnedBanner(chatId, chatType) {
    const banner = document.getElementById('pinned-banner');
    if (!banner) return;
    const gen = ++_pinnedBannerPollGen;
    try {
      const params = new URLSearchParams({ chat_type: chatType });
      const res = await fetch(`/api/chat/${encodeURIComponent(chatId)}/pinned?` + params);
      if (gen !== _pinnedBannerPollGen || !currentChat || String(currentChat.id) !== String(chatId)) return;
      if (!res.ok) {
        banner.style.display = 'none';
        return;
      }
      const data = await res.json();
      const rawPins = data.pinned_messages || (data.pinned_message ? [data.pinned_message] : []);
      _chatPinsCache.set(String(chatId), rawPins);
      _chatPins = rawPins.filter(p => !_dismissedPins.has(p.id));
      _currentPinIndex = 0;
      renderPinnedBanner();
      _syncRenderedMessagePins();
    } catch {
      if (gen === _pinnedBannerPollGen) {
        banner.style.display = 'none';
      }
    }
  }

  function _syncRenderedMessagePins() {
    const activePinIds = new Set((_chatPins || []).map(p => String(p.id)));
    document.querySelectorAll('.msg-row .msg-pinned-icon').forEach(icon => {
      const row = icon.closest('.msg-row');
      if (row && !activePinIds.has(String(row.dataset.msgId))) {
        icon.remove();
      }
    });
    activePinIds.forEach(id => {
      const escId = (window.CSS && CSS.escape) ? CSS.escape(id) : id;
      const row = document.querySelector(`.msg-row[data-msg-id="${escId}"]`);
      if (row && !row.querySelector('.msg-pinned-icon')) {
        const meta = row.querySelector('.msg-meta');
        if (meta) {
          const pinIcon = document.createElement('span');
          pinIcon.className = 'msg-meta-icon msg-pinned-icon';
          pinIcon.title = 'Pinned message';
          pinIcon.setAttribute('aria-label', 'Pinned message');
          pinIcon.innerHTML = '<svg viewBox="0 0 16 16" width="12" height="12" fill="currentColor"><path d="M4.5 1.5A.5.5 0 0 1 5 1h6a.5.5 0 0 1 .5.5v2.879a2.5 2.5 0 0 0 .732 1.768l1.06 1.06A1.5 1.5 0 0 1 13 8.268V9a.5.5 0 0 1-.5.5H9v5.5a.5.5 0 0 1-1 0V9.5H3.5A.5.5 0 0 1 3 9v-.732a1.5 1.5 0 0 1 .707-1.27l1.061-1.06A2.5 2.5 0 0 0 5.5 4.379V1.5z"/></svg>';
          meta.insertBefore(pinIcon, meta.firstChild);
        }
      }
    });
  }

  function renderPinnedBanner() {
    const banner = document.getElementById('pinned-banner');
    if (!banner) return;
    if (!_chatPins || _chatPins.length === 0 || _currentPinIndex >= _chatPins.length) {
      banner.style.display = 'none';
      updateJumpHudPosition();
      return;
    }
    banner.style.display = 'flex';
    const pin = _chatPins[_currentPinIndex];
    const authorEl = document.getElementById('pinned-banner-author');
    const snippetEl = document.getElementById('pinned-banner-snippet');
    if (authorEl) {
      const pinLabel = _chatPins.length > 1
        ? `Pinned Message (${_currentPinIndex + 1} of ${_chatPins.length})`
        : 'Pinned';
      const cleanAuthor = _isUnreadableToken(pin.sender_name) ? '' : (pin.sender_name || '');
      authorEl.textContent = cleanAuthor ? `${pinLabel} • ${cleanAuthor}:` : `${pinLabel}:`;
    }
    if (snippetEl) {
      snippetEl.textContent = _formatMessagePreview(pin);
    }

    const navEl = document.getElementById('pinned-banner-nav');
    const countEl = document.getElementById('pinned-banner-count');
    if (navEl && countEl) {
      if (_chatPins.length > 1) {
        navEl.style.display = 'flex';
        countEl.textContent = `${_currentPinIndex + 1} of ${_chatPins.length}`;
      } else {
        navEl.style.display = 'none';
      }
    }
    updateJumpHudPosition();
  }

  const pinnedBanner = document.getElementById('pinned-banner');
  if (pinnedBanner) {
    const contentEl = pinnedBanner.querySelector('.pinned-banner-content');
    if (contentEl) {
      contentEl.addEventListener('click', async () => {
        if (_chatPins && _chatPins.length > 0 && _currentPinIndex < _chatPins.length) {
          const pin = _chatPins[_currentPinIndex];
          await jumpToMessage({ msg_id: pin.id, timestamp_ms: pin.timestamp * 1000 }, 'emerald');
        }
      });
      contentEl.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          contentEl.click();
        }
      });
    }

    const prevBtn = document.getElementById('pinned-banner-prev');
    if (prevBtn) {
      prevBtn.addEventListener('click', e => {
        e.stopPropagation();
        if (_chatPins.length > 1) {
          _currentPinIndex = (_currentPinIndex - 1 + _chatPins.length) % _chatPins.length;
          renderPinnedBanner();
        }
      });
    }

    const nextBtn = document.getElementById('pinned-banner-next');
    if (nextBtn) {
      nextBtn.addEventListener('click', e => {
        e.stopPropagation();
        if (_chatPins.length > 1) {
          _currentPinIndex = (_currentPinIndex + 1) % _chatPins.length;
          renderPinnedBanner();
        }
      });
    }

    const dismissBtn = document.getElementById('pinned-banner-dismiss');
    if (dismissBtn) {
      dismissBtn.addEventListener('click', e => {
        e.stopPropagation();
        if (_chatPins && _chatPins.length > 0 && _currentPinIndex < _chatPins.length) {
          const pin = _chatPins[_currentPinIndex];
          _dismissedPins.add(pin.id);
          _chatPins = _chatPins.filter(p => !_dismissedPins.has(p.id));
          if (_currentPinIndex >= _chatPins.length) {
            _currentPinIndex = 0;
          }
          renderPinnedBanner();
        }
      });
    }
  }

  // ---- starred messages sidebar list ---------------------------------------

  let _starredList = [];
  let _starredOffset = 0;
  let _starredHasMore = false;
  let _starredLoading = false;
  let _activeStarredMsgId = null;

  async function loadStarredList(reset) {
    if (_starredLoading) return;
    _starredLoading = true;
    const chatListEl = document.getElementById('chat-list');
    if (!chatListEl) {
      _starredLoading = false;
      return;
    }

    const searchInput = document.getElementById('search-input');
    const q = (currentNavTab === 'starred' && searchInput) ? searchInput.value.trim() : '';

    if (reset) {
      _starredOffset = 0;
      _starredList = [];
      chatListEl.innerHTML = '<div id="chat-list-notice"><div class="loading-spinner"></div>Loading starred messages…</div>';
    }

    try {
      const params = new URLSearchParams({
        offset: String(_starredOffset),
        limit: '50'
      });
      if (q) params.set('q', q);

      const res = await fetch(`/api/starred?${params}`);
      if (!res.ok) {
        throw new Error(`Server returned HTTP ${res.status} when fetching starred messages`);
      }
      const data = await res.json();
      if (currentNavTab !== 'starred') return;

      const msgs = data.messages || [];
      _starredHasMore = Boolean(data.has_more);
      _starredOffset += msgs.length;

      if (reset) {
        chatListEl.innerHTML = '';
        if (msgs.length === 0) {
          if (q) {
            chatListEl.innerHTML = `<div class="sidebar-tab-placeholder"><span class="placeholder-icon">🔍</span><strong>No Matches</strong><span>No starred bookmarks matching "${esc(q)}"</span></div>`;
          } else {
            chatListEl.innerHTML = '<div class="sidebar-tab-placeholder"><span class="placeholder-icon">⭐</span><strong>Starred Messages</strong><span>No starred bookmarks found</span></div>';
          }
          return;
        }
      }

      _starredList.push(...msgs);
      msgs.forEach(m => {
        chatListEl.appendChild(renderStarredItem(m));
      });
    } catch (err) {
      if (currentNavTab !== 'starred') return;
      if (reset) {
        chatListEl.innerHTML = '<div class="sidebar-tab-placeholder"><span class="placeholder-icon">⭐</span><strong>Starred Messages</strong><span>Failed to load bookmarks</span></div>';
      }
      console.error('Failed to load starred bookmarks:', err);
    } finally {
      _starredLoading = false;
    }
  }

  function renderStarredItem(m) {
    const el = document.createElement('div');
    const isActive = _activeStarredMsgId !== null && _activeStarredMsgId === String(m.id);
    el.className = 'chat-item' + (m.chat_type === 'group' ? ' group' : '') + (isActive ? ' active' : '');
    el.dataset.msgId = String(m.id);
    el.dataset.chatId = String(m.chat_id);
    if (m.chat_type) el.dataset.chatType = m.chat_type;

    const avatar = document.createElement('div');
    avatar.className = 'chat-item-avatar';
    const chatName = m.chat_name || 'Chat';
    const avatarKey = m.chat_phone || m.chat_id || chatName;
    avatar.style.backgroundColor = getAvatarColor(avatarKey);
    avatar.textContent = getInitials(chatName);
    avatar.setAttribute('aria-hidden', 'true');
    el.appendChild(avatar);

    const body = document.createElement('div');
    body.className = 'chat-item-body';
    const preview = _formatStarredPreview(m);
    const rawSender = m.sender_name || '';
    const cleanSender = _isUnreadableToken(rawSender) ? '' : rawSender;
    const authorPrefix = cleanSender ? `${cleanSender}: ` : '';
    body.innerHTML = `<div class="chat-name">${esc(chatName)}</div><div class="chat-meta">${esc(authorPrefix + preview)} · ${fmtTime(m.timestamp * 1000)}</div>`;
    el.appendChild(body);

    el.addEventListener('click', () => {
      openChatAtMessage(m.chat_id, m.id, m.chat_type, m.chat_name);
    });
    return el;
  }

  function filterStarredList(searchQuery) {
    const list = document.getElementById('chat-list');
    if (!list) return;

    if (_starredList.length === 0) {
      list.innerHTML = '<div class="sidebar-tab-placeholder"><span class="placeholder-icon">⭐</span><strong>Starred Messages</strong><span>No starred bookmarks found</span></div>';
      return;
    }

    const q = (searchQuery || '').trim().toLowerCase();
    const matches = q
      ? _starredList.filter(m => (m.chat_name || '').toLowerCase().includes(q) || (m.sender_name || '').toLowerCase().includes(q) || (m.text || m.text_body || '').toLowerCase().includes(q))
      : _starredList;

    list.innerHTML = '';
    if (matches.length === 0) {
      list.innerHTML = `<div class="sidebar-tab-placeholder"><span class="placeholder-icon">🔍</span><strong>No Matches</strong><span>No starred bookmarks matching "${esc(searchQuery)}"</span></div>`;
      return;
    }

    matches.forEach(m => {
      list.appendChild(renderStarredItem(m));
    });
  }

  // ---- calls history & inspector -------------------------------------------

  let _callsList = [];
  let _callsOffset = 0;
  let _callsHasMore = false;
  let _callsLoading = false;

  function filterCallsList(searchQuery) {
    const list = document.getElementById('chat-list');
    if (!list) return;

    if (_callsList.length === 0) {
      list.innerHTML = '<div class="sidebar-tab-placeholder"><span class="placeholder-icon">📞</span><strong>No Call History</strong><span>No calls found in this backup</span></div>';
      return;
    }

    const q = (searchQuery || '').trim().toLowerCase();
    const matches = q
      ? _callsList.filter(c => (c.caller_name || '').toLowerCase().includes(q) || (c.caller_phone || '').toLowerCase().includes(q))
      : _callsList;

    list.innerHTML = '';
    if (matches.length === 0) {
      list.innerHTML = `<div class="sidebar-tab-placeholder"><span class="placeholder-icon">🔍</span><strong>No Matches</strong><span>No calls matching "${esc(searchQuery)}"</span></div>`;
      return;
    }

    matches.forEach(c => {
      list.appendChild(renderCallItem(c));
    });
  }

  async function loadCallsList(reset) {
    if (_callsLoading) return;
    _callsLoading = true;
    const chatListEl = document.getElementById('chat-list');
    if (!chatListEl) {
      _callsLoading = false;
      return;
    }

    const searchInput = document.getElementById('search-input');
    const q = (currentNavTab === 'calls' && searchInput) ? searchInput.value.trim() : '';

    if (reset) {
      _callsOffset = 0;
      _callsList = [];
      chatListEl.innerHTML = '<div id="chat-list-notice"><div class="loading-spinner"></div>Loading Call History…</div>';
    }

    try {
      const params = new URLSearchParams({
        offset: String(_callsOffset),
        limit: '50'
      });
      if (q) params.set('q', q);

      const res = await fetch(`/api/calls?${params}`);
      if (!res.ok) {
        throw new Error(`Server returned HTTP ${res.status} when fetching calls`);
      }
      const data = await res.json();
      if (currentNavTab !== 'calls') return;

      const calls = data.calls || [];
      _callsHasMore = Boolean(data.has_more);
      _callsOffset += calls.length;

      if (reset) {
        chatListEl.innerHTML = '';
        if (calls.length === 0) {
          if (q) {
            chatListEl.innerHTML = `<div class="sidebar-tab-placeholder"><span class="placeholder-icon">🔍</span><strong>No Matches</strong><span>No calls matching "${esc(q)}"</span></div>`;
          } else {
            chatListEl.innerHTML = '<div class="sidebar-tab-placeholder"><span class="placeholder-icon">📞</span><strong>No Call History</strong><span>No calls found in this backup</span></div>';
          }
          return;
        }
      }

      _callsList.push(...calls);
      calls.forEach(c => {
        chatListEl.appendChild(renderCallItem(c));
      });
    } catch (err) {
      if (currentNavTab !== 'calls') return;
      if (reset) {
        chatListEl.innerHTML = '<div class="sidebar-tab-placeholder"><span class="placeholder-icon">⚠️</span><strong>Call History Error</strong><span>Unable to load call records</span></div>';
      }
      console.error('Failed to load call history:', err);
    } finally {
      _callsLoading = false;
    }
  }

  function _getCallMediaSvg(isVideo, size) {
    const px = size || 16;
    if (isVideo) {
      return `<svg width="${px}" height="${px}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>`;
    }
    return `<svg width="${px}" height="${px}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"/></svg>`;
  }

  function _getCallDirectionSvg(callType) {
    if (callType === 'missed') {
      return '<svg class="call-item-arrow call-icon-missed" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/></svg>';
    }
    if (callType === 'incoming') {
      return '<svg class="call-item-arrow call-icon-incoming" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><polyline points="16 18 22 18 22 12"/><line x1="22" y1="18" x2="11" y2="7"/></svg>';
    }
    return '<svg class="call-item-arrow call-icon-outgoing" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><polyline points="8 6 2 6 2 12"/><line x1="2" y1="6" x2="13" y2="17"/></svg>';
  }

  function _formatCallOutcomeText(callType, outcome, durationSec) {
    if (callType === 'missed' && (!durationSec || durationSec <= 0)) {
      return outcome === 'declined' ? 'Declined' : 'Missed';
    }
    const dirLabel = callType === 'outgoing' ? 'Outgoing' : 'Incoming';
    if (durationSec > 0) {
      return `${dirLabel} · ${fmtDuration(durationSec)}`;
    }
    if (outcome === 'canceled') {
      return `${dirLabel} · Canceled`;
    }
    return `${dirLabel} · No answer`;
  }

  function renderCallCard(msg) {
    const c = msg.call || {};
    let callType = c.call_type || (msg.from_me ? 'outgoing' : 'incoming');
    if ((c.duration_sec && c.duration_sec > 0) && callType === 'missed') {
      callType = c.from_me ? 'outgoing' : 'incoming';
    }
    const isMissed = callType === 'missed';

    const card = document.createElement('div');
    card.className = 'msg-call-card';

    const iconWrap = document.createElement('div');
    iconWrap.className = 'msg-call-icon' + (isMissed ? ' missed' : '');
    iconWrap.innerHTML = _getCallMediaSvg(!!c.video, 18);
    card.appendChild(iconWrap);

    const body = document.createElement('div');
    body.className = 'msg-call-body';

    const title = document.createElement('div');
    title.className = 'msg-call-title';
    const baseLabel = c.video ? 'Video call' : 'Voice call';
    title.textContent = c.is_group ? `Group ${baseLabel.toLowerCase()}` : baseLabel;
    body.appendChild(title);

    const status = document.createElement('div');
    status.className = 'msg-call-status' + (isMissed ? ' missed' : '');

    const arrowSpan = document.createElement('span');
    arrowSpan.className = 'msg-call-arrow';
    arrowSpan.innerHTML = _getCallDirectionSvg(callType);
    status.appendChild(arrowSpan);

    const statusLabel = document.createElement('span');
    statusLabel.textContent = _formatCallOutcomeText(callType, c.outcome, c.duration_sec || 0);
    status.appendChild(statusLabel);

    body.appendChild(status);
    card.appendChild(body);
    return card;
  }

  function renderCallItem(call) {
    const el = document.createElement('div');
    el.className = 'call-item';
    el.dataset.callId = call.id;
    el.tabIndex = 0;
    el.setAttribute('role', 'button');

    const callerName = call.caller_name || 'Unknown';
    const tsMs = call.timestamp_ms ?? (call.timestamp * 1000);
    const timeLabel = fmtTime(tsMs);
    const durSec = call.duration || 0;
    let callType = call.call_type;
    if (durSec > 0 && callType === 'missed') {
      callType = call.from_me ? 'outgoing' : 'incoming';
    }
    const isMissed = callType === 'missed';
    const outcomeText = isMissed
      ? (call.outcome === 'declined' ? 'Declined' : 'Missed')
      : _formatCallOutcomeText(callType, durSec > 0 ? 'answered' : (call.outcome || 'unanswered'), durSec);
    el.setAttribute(
      'aria-label',
      `${callerName}, ${call.is_video ? 'Video call' : 'Voice call'}, ${outcomeText}, ${timeLabel}`
    );

    const avatar = document.createElement('div');
    avatar.className = 'call-item-avatar';
    const avatarKey = call.caller_phone || call.caller_name || 'Call';
    avatar.style.backgroundColor = getAvatarColor(avatarKey);
    avatar.textContent = getInitials(callerName);
    el.appendChild(avatar);

    const body = document.createElement('div');
    body.className = 'call-item-body';

    const row1 = document.createElement('div');
    row1.className = 'call-item-row-1';

    const nameEl = document.createElement('div');
    nameEl.className = 'call-item-name';
    nameEl.textContent = callerName;
    if (call.is_group) {
      const grpIcon = document.createElement('span');
      grpIcon.className = 'call-item-group-icon';
      grpIcon.title = 'Group call';
      grpIcon.innerHTML = ' 👥';
      nameEl.appendChild(grpIcon);
    }
    if (call.is_preserved) {
      const presBadge = document.createElement('span');
      presBadge.className = 'call-item-pres-badge';
      presBadge.textContent = 'Preserved';
      nameEl.appendChild(presBadge);
    }
    row1.appendChild(nameEl);

    const mediaIcon = document.createElement('span');
    mediaIcon.className = 'call-item-media-icon';
    mediaIcon.innerHTML = _getCallMediaSvg(!!call.is_video, 15);
    mediaIcon.title = call.is_video ? 'Video call' : 'Voice call';
    row1.appendChild(mediaIcon);
    body.appendChild(row1);

    const row2 = document.createElement('div');
    row2.className = 'call-item-row-2';

    const sub = document.createElement('div');
    sub.className = 'call-item-sub';
    const subClass = isMissed ? ' call-icon-missed' : '';
    sub.innerHTML = _getCallDirectionSvg(callType) +
      `<span class="call-item-sub-text${subClass}">${esc(outcomeText)}</span>`;
    row2.appendChild(sub);

    const timeEl = document.createElement('span');
    timeEl.className = 'call-item-time';
    timeEl.textContent = timeLabel;
    row2.appendChild(timeEl);

    body.appendChild(row2);
    el.appendChild(body);

    el.addEventListener('click', () => {
      selectCallItem(call, el);
    });
    el.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        selectCallItem(call, el);
      }
    });

    return el;
  }

  function selectCallItem(call, el) {
    document.querySelectorAll('.call-item').forEach(item => {
      item.classList.remove('active');
    });
    if (el) el.classList.add('active');

    const emptyPane = document.getElementById('empty-pane');
    if (emptyPane) emptyPane.style.display = 'none';

    const chatHeader = document.getElementById('chat-header');
    if (chatHeader) chatHeader.style.display = 'none';

    const pinnedBanner = document.getElementById('pinned-banner');
    if (pinnedBanner) pinnedBanner.style.display = 'none';

    const contextBanner = document.getElementById('context-jump-banner');
    if (contextBanner) contextBanner.style.display = 'none';

    const msgScroll = document.getElementById('message-scroll');
    if (msgScroll) msgScroll.style.display = 'none';

    const scrollBottom = document.getElementById('scroll-to-bottom');
    if (scrollBottom) scrollBottom.classList.remove('visible');

    closeChatInfo();
    closeChatStarred();
    closeMediaGallery();

    const callInspector = document.getElementById('pane-call-inspector');
    if (!callInspector) return;
    callInspector.style.display = 'flex';

    const avatarEl = document.getElementById('call-inspect-avatar');
    if (avatarEl) {
      avatarEl.textContent = getInitials(call.caller_name || 'Call');
      avatarEl.style.backgroundColor = getAvatarColor(call.caller_phone || call.caller_name || 'Call');
    }

    const nameEl = document.getElementById('call-inspect-name');
    if (nameEl) nameEl.textContent = call.caller_name || 'Unknown';

    const phoneEl = document.getElementById('call-inspect-phone');
    if (phoneEl) {
      const formatted = formatPhoneNumber(call.caller_phone || '');
      phoneEl.textContent = formatted ? formatted : (call.caller_phone || '');
    }

    const badgeIcon = document.getElementById('call-inspect-badge-icon');
    const badgeLabel = document.getElementById('call-inspect-badge-label');
    if (badgeIcon && badgeLabel) {
      const typeStr = call.is_group ? 'Group ' : '';
      if (call.is_video) {
        badgeIcon.textContent = '📹';
        badgeLabel.textContent = `${typeStr}Video Call`;
      } else {
        badgeIcon.textContent = call.is_group ? '👥' : '📞';
        badgeLabel.textContent = `${typeStr}Voice Call`;
      }
      if (call.is_preserved) {
        badgeLabel.textContent += ' • Preserved';
      }
    }

    let inspectCallType = call.call_type;
    if (call.duration > 0 && inspectCallType === 'missed') {
      inspectCallType = call.from_me ? 'outgoing' : 'incoming';
    }

    const dirEl = document.getElementById('call-inspect-dir');
    if (dirEl) {
      if (inspectCallType === 'missed') {
        dirEl.innerHTML = '<span class="call-icon-missed call-dir-label">Missed</span>';
      } else if (inspectCallType === 'incoming') {
        dirEl.innerHTML = '<svg class="call-item-arrow call-icon-incoming" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="16 18 22 18 22 12"/><line x1="22" y1="18" x2="11" y2="7"/></svg> <span class="call-icon-incoming call-dir-label">Incoming</span>';
      } else {
        dirEl.innerHTML = '<svg class="call-item-arrow call-icon-outgoing" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="8 6 2 6 2 12"/><line x1="2" y1="6" x2="13" y2="17"/></svg> <span class="call-icon-outgoing call-dir-label">Outgoing</span>';
      }
    }

    const timeEl = document.getElementById('call-inspect-time');
    if (timeEl) {
      const d = new Date(call.timestamp * 1000);
      timeEl.textContent = d.toLocaleString(undefined, {
        weekday: 'short',
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
      });
    }

    const durEl = document.getElementById('call-inspect-duration');
    if (durEl) {
      if (call.duration > 0) {
        durEl.textContent = fmtDurationLong(call.duration);
      } else if (inspectCallType === 'missed') {
        durEl.textContent = call.outcome === 'declined' ? '0s (Declined)' : '0s (Missed)';
      } else if (inspectCallType === 'outgoing') {
        durEl.textContent = call.outcome === 'canceled' ? '0s (Canceled)' : '0s (No answer)';
      } else {
        durEl.textContent = '0s';
      }
    }

    const dataRow = document.getElementById('call-inspect-data-row');
    const dataVal = document.getElementById('call-inspect-data');
    if (dataRow && dataVal) {
      if (call.data_transferred && call.data_transferred > 0) {
        dataRow.style.display = 'flex';
        dataVal.textContent = _formatBytes(call.data_transferred);
      } else {
        dataRow.style.display = 'none';
      }
    }

    const jumpBtn = document.getElementById('call-inspect-jump-btn');
    if (jumpBtn) {
      let matchedChat = null;
      if (call.chat_id) {
        matchedChat = allChats.find(c => String(c.id) === String(call.chat_id) || c.jid === call.chat_id);
      }
      if (!matchedChat && call.caller_phone) {
        const cleanPhone = call.caller_phone.replace(/[^0-9]/g, '');
        if (cleanPhone) {
          matchedChat = allChats.find(c => {
            const cp = (c.phone_number || '').replace(/[^0-9]/g, '');
            if (!cp) return false;
            if (cp === cleanPhone) return true;
            if (cleanPhone.length >= 7 && cp.length >= 7) {
              return cp.endsWith(cleanPhone) || cleanPhone.endsWith(cp);
            }
            return false;
          });
        }
      }

      if (matchedChat) {
        jumpBtn.disabled = false;
        jumpBtn.title = 'Jump to this moment in conversation';
        jumpBtn.onclick = () => {
          jumpFromCallToChat(call, matchedChat);
        };
      } else {
        jumpBtn.disabled = true;
        jumpBtn.title = 'No archived conversation found for this caller';
        jumpBtn.onclick = null;
      }
    }
  }

  let _pendingJumpCallId = null;

  function jumpFromCallToChat(call, matchedChat) {
    if (!matchedChat) return;
    const callInspector = document.getElementById('pane-call-inspector');
    if (callInspector) callInspector.style.display = 'none';
    _pendingJumpCallId = call && call.id ? ('call:' + call.id) : null;
    openChatAtTimestamp(matchedChat.id, call.timestamp_ms ?? (call.timestamp * 1000));
  }

  const chatListScroll = document.getElementById('chat-list');
  if (chatListScroll) {
    chatListScroll.addEventListener('scroll', () => {
      if (currentNavTab === 'calls' && _callsHasMore && !_callsLoading) {
        if (chatListScroll.scrollTop + chatListScroll.clientHeight >= chatListScroll.scrollHeight - 80) {
          loadCallsList(false);
        }
      } else if (currentNavTab === 'starred' && _starredHasMore && !_starredLoading) {
        if (chatListScroll.scrollTop + chatListScroll.clientHeight >= chatListScroll.scrollHeight - 80) {
          loadStarredList(false);
        }
      }
    });
  }

  // ---- cross-chat jump engine ----------------------------------------------

  function showContextJumpBanner(icon, text, onSwitch) {
    const contextBanner = document.getElementById('context-jump-banner');
    if (!contextBanner) return;
    contextBanner.style.display = 'flex';
    const bannerIcon = contextBanner.querySelector('.context-jump-icon');
    if (bannerIcon) bannerIcon.textContent = icon;
    const bannerText = document.getElementById('context-jump-text');
    if (bannerText) bannerText.textContent = text;
    const switchBtn = document.getElementById('context-jump-switch-btn');
    if (switchBtn) {
      switchBtn.onclick = onSwitch;
    }
    updateJumpHudPosition();
  }

  function _findChatInList(chatId, chatType) {
    const cid = String(chatId);
    if (chatType) {
      const exact = allChats.find(c => String(c.id) === cid && c.type === chatType);
      if (exact) return exact;
    }
    return allChats.find(c => String(c.id) === cid || c.jid === chatId) || null;
  }

  async function navigateToMentionChat(chatId, chatType) {
    if (!chatId) return;
    let chat = _findChatInList(chatId, chatType || 'contact');
    if (!chat) {
      try {
        await loadChats();
        chat = _findChatInList(chatId, chatType || 'contact');
      } catch (err) {
        console.warn('Failed to reload chats for mention navigation:', err);
      }
    }
    if (!chat) return;
    if (chat.is_archived) {
      if (currentNavTab !== 'archived') switchNavTab('archived');
    } else {
      if (currentNavTab !== 'chats') switchNavTab('chats');
      if (currentFilter !== 'all' && currentFilter !== chat.type) {
        resetFilterTo('all');
      }
    }
    const itemEl = _findChatListItem(chat.id, chat.type);
    await selectChat(chat, itemEl);
  }

  const _msgScrollForMentions = document.getElementById('message-scroll');
  if (_msgScrollForMentions) {
    _msgScrollForMentions.addEventListener('click', e => {
      const link = e.target.closest('a.wa-mention-link');
      if (!link) return;
      e.preventDefault();
      e.stopPropagation();
      navigateToMentionChat(link.dataset.chatId, link.dataset.chatType);
    });
  }

  async function openChatAtMessage(chatId, messageId, chatType, chatName) {
    if (_jumpAbortController) {
      _jumpAbortController.abort();
    }
    _jumpAbortController = new AbortController();
    const abortSignal = _jumpAbortController.signal;
    const gen = ++_loadGen;
    loading = true;

    let chat = _findChatInList(chatId, chatType);
    if (!chat) {
      try {
        await loadChats();
        chat = _findChatInList(chatId, chatType);
      } catch (err) {
        if (err.name === 'AbortError') return null;
      }
    }
    if (!chat) {
      if (chatId !== undefined && chatId !== null && String(chatId) !== '') {
        chat = {
          id: String(chatId),
          type: chatType || 'contact',
          display_name: chatName || String(chatId),
          phone_number: '',
          is_archived: 0,
          is_preserved_only: false
        };
      } else {
        loading = false;
        showJumpHudError('Unable to locate chat for this message');
        return null;
      }
    }

    if (currentNavTab === 'starred') {
      _activeStarredMsgId = String(messageId);
      document.querySelectorAll('.chat-item').forEach(e => {
        const match = e.dataset.msgId === String(messageId);
        e.classList.toggle('active', match);
        if (match) e.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      });
    } else {
      _activeStarredMsgId = null;
      document.querySelectorAll('.chat-item').forEach(e => {
        const match = e.dataset.id === String(chat.id) && e.dataset.type === chat.type;
        e.classList.toggle('active', match);
        if (match) e.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      });
    }

    const scroll = _prepareChatPane(chat, true);
    if (!_chatPinsCache.has(String(chat.id))) {
      await loadChatPinnedBanner(chat.id, chat.type);
    }

    const loadingEl = document.getElementById('chat-loading');
    loadingEl.style.display = 'flex';
    showJumpHud('Jumping to message in context…');

    try {
      const params = new URLSearchParams({
        chat_id: chat.id,
        chat_type: chat.type,
        msg_id: messageId,
        limit: 50
      });
      const res = await fetch('/api/messages/at?' + params, { signal: abortSignal });
      if (!res.ok) {
        throw new Error(`Server returned HTTP ${res.status} when loading messages at msg_id ${messageId}`);
      }
      const msgs = await res.json();
      if (gen !== _loadGen) return null;

      mergePage(msgs);
      renderWindow();

      let targetEl = rowForMsgId(messageId);
      if (targetEl) {
        await _pinToMsg(targetEl, gen);
        flashHighlightRow(targetEl, 'gold');
      }

      showContextJumpBanner('⭐', 'Viewing message in context', () => {
        const contextBanner = document.getElementById('context-jump-banner');
        if (contextBanner) contextBanner.style.display = 'none';
        _activeStarredMsgId = null;
        if (chat.is_archived) {
          switchNavTab('archived');
        } else {
          switchNavTab('chats');
          resetFilterTo('all');
        }
        const activeItem = _findChatListItem(chat.id, chat.type);
        if (activeItem) activeItem.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      });

      hideJumpHud();
      return targetEl || null;
    } catch (err) {
      if (err.name === 'AbortError') return null;
      showJumpHudError('Unable to load message');
      throw err;
    } finally {
      if (scroll) scroll.classList.remove('is-navigating');
      if (gen === _loadGen) {
        loadingEl.style.display = 'none';
        loading = false;
      }
    }
  }

  async function openChatAtTimestamp(chatId, timestampMs) {
    const targetCallMsgId = _pendingJumpCallId;
    _pendingJumpCallId = null;
    if (_jumpAbortController) {
      _jumpAbortController.abort();
    }
    _jumpAbortController = new AbortController();
    const abortSignal = _jumpAbortController.signal;
    const gen = ++_loadGen;
    loading = true;

    let chat = allChats.find(c => String(c.id) === String(chatId) || c.jid === chatId);
    if (!chat) {
      await loadChats();
      chat = allChats.find(c => String(c.id) === String(chatId) || c.jid === chatId);
    }
    if (!chat) {
      loading = false;
      showJumpHudError('Unable to locate chat for this call');
      return null;
    }

    document.querySelectorAll('.chat-item').forEach(e => {
      const match = e.dataset.id === String(chat.id) && e.dataset.type === chat.type;
      e.classList.toggle('active', match);
      if (match) e.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    });

    const scroll = _prepareChatPane(chat, true);
    if (!_chatPinsCache.has(String(chat.id))) {
      await loadChatPinnedBanner(chat.id, chat.type);
    }

    const loadingEl = document.getElementById('chat-loading');
    loadingEl.style.display = 'flex';
    showJumpHud('Jumping to conversation moment…');

    try {
      const params = new URLSearchParams({
        chat_id: chat.id,
        chat_type: chat.type,
        ts: timestampMs,
        limit: 50
      });
      const res = await fetch('/api/messages/at?' + params, { signal: abortSignal });
      if (!res.ok) {
        throw new Error(`Server returned HTTP ${res.status} when loading messages at timestamp ${timestampMs}`);
      }
      const msgs = await res.json();
      if (gen !== _loadGen) return null;

      mergePage(msgs);
      renderWindow();

      let targetEl = targetCallMsgId ? rowForMsgId(targetCallMsgId) : null;
      let minDiff = targetEl ? Math.abs(Number(targetEl.dataset.ts || timestampMs) - timestampMs) : Infinity;
      let targetTs = targetEl ? Number(targetEl.dataset.ts || timestampMs) : 0;
      if (!targetEl) {
        document.querySelectorAll('.msg-row').forEach(row => {
          const rowTs = Number(row.dataset.ts);
          if (rowTs) {
            const diff = Math.abs(rowTs - timestampMs);
            if (diff < minDiff) {
              minDiff = diff;
              targetEl = row;
              targetTs = rowTs;
            }
          }
        });
      }

      if (targetEl) {
        await _pinToMsg(targetEl, gen);
        flashHighlightRow(targetEl, 'emerald');
      }

      const gapSuffix = targetTs ? fmtTimeGap(minDiff, targetTs < timestampMs) : '';
      showContextJumpBanner('📞', `Viewing Call Moment in context${gapSuffix}`, () => {
        const contextBanner = document.getElementById('context-jump-banner');
        if (contextBanner) contextBanner.style.display = 'none';
        if (chat.is_archived) {
          switchNavTab('archived');
        } else {
          switchNavTab('chats');
          resetFilterTo('all');
        }
        const activeItem = _findChatListItem(chat.id, chat.type);
        if (activeItem) activeItem.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      });

      hideJumpHud();
      return targetEl || null;
    } catch (err) {
      if (err.name === 'AbortError') return null;
      showJumpHudError('Unable to load moment');
      throw err;
    } finally {
      if (scroll) scroll.classList.remove('is-navigating');
      if (gen === _loadGen) {
        loadingEl.style.display = 'none';
        loading = false;
      }
    }
  }

  // ---- message details popup ------------------------------------------------

  const msgDetailsPopup = document.getElementById('msg-details-popup');

  function hideMsgDetails() {
    msgDetailsPopup.classList.remove('visible');
  }

  document.addEventListener('click', e => {
    if (!msgDetailsPopup.contains(e.target)) hideMsgDetails();
  });


  function fmtReceipt(ts) {
    if (!ts) return '—';
    return new Date(ts).toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
    });
  }

  // Recent iOS messages record that a message was read (aggregate status) but no
  // longer store the precise read time. Show that state instead of a blank dash.
  function fmtRead(entry) {
    if (entry.read_ts) return fmtReceipt(entry.read_ts);
    if (entry.read_known) return '<span class="msg-details-na">Read (time not stored)</span>';
    return '—';
  }

  function showCallDetails(msg, anchorEl) {
    const c = msg.call || {};
    const kind = (c.is_group ? 'Group ' : '') + (c.video ? 'Video call' : 'Voice call');
    const dir = c.from_me ? 'Outgoing' : 'Incoming';
    const outcomeMap = {
      answered: 'Answered',
      missed: 'Missed',
      declined: 'Declined',
      canceled: 'Canceled',
      unanswered: 'No answer'
    };
    const statusLabel = outcomeMap[c.outcome] || 'Recorded';
    const durText = (c.duration_sec && c.duration_sec > 0) ? fmtDurationLong(c.duration_sec) : '—';

    let html =
      `<div class="msg-details-row"><span class="msg-details-label">Type</span><span class="msg-details-value">${esc(kind)}</span></div>` +
      `<div class="msg-details-row"><span class="msg-details-label">Direction</span><span class="msg-details-value">${esc(dir)}</span></div>` +
      `<div class="msg-details-row"><span class="msg-details-label">Outcome</span><span class="msg-details-value">${esc(statusLabel)}</span></div>` +
      `<div class="msg-details-row"><span class="msg-details-label">Duration</span><span class="msg-details-value">${esc(durText)}</span></div>` +
      `<div class="msg-details-row"><span class="msg-details-label">Time</span><span class="msg-details-value">${esc(fmtTime(msg.timestamp_ms))}</span></div>`;

    if (c.is_group && c.participants > 1) {
      html += `<div class="msg-details-row"><span class="msg-details-label">Participants</span><span class="msg-details-value">${esc(String(c.participants))}</span></div>`;
    }
    if (msg.is_preserved) {
      html += `<div class="msg-details-row"><span class="msg-details-label">Source</span><span class="msg-details-value">Preserved from backup</span></div>`;
    }

    msgDetailsPopup.innerHTML = html;
    positionPopup(anchorEl);
    msgDetailsPopup.classList.add('visible');
  }

  async function showMsgDetails(msgId, anchorEl, archivePath, presInfo, editInfo) {
    let html = '';

    if (presInfo) {
      const reasonText = presInfo.reason === 'revoked_by_sender'
        ? 'Revoked by sender'
        : (presInfo.reason === 'chat_deleted' ? 'Chat deleted on device' : 'Deleted on device');
      const presTs = typeof presInfo.at === 'number' ? presInfo.at * 1000 : Date.parse(presInfo.at);
      const atText = !isNaN(presTs) ? fmtTime(presTs) : (presInfo.at || '');
      const platformText = presInfo.platform ? (' • ' + esc(presInfo.platform)) : '';
      const backupPrefix = atText ? `Preserved from backup (${atText})` : 'Preserved from backup';
      html += `<div class="msg-details-row"><span class="msg-details-label">Preserved</span><span class="msg-details-value">${backupPrefix} • ${reasonText}${platformText}</span></div>`;
    }

    if (editInfo && (editInfo.original_text || editInfo.edited_ts)) {
      html += '<div class="msg-details-section msg-edit-history">';
      if (editInfo.original_text) {
        html += `<div class="msg-details-row"><span class="msg-details-label">Original (Pre-edit)</span><span class="msg-details-value msg-pre-edit-text">${esc(resolveMentionsPlainText(editInfo.original_text, editInfo.mentions))}</span></div>`;
      }
      if (editInfo.edited_ts) {
        html += `<div class="msg-details-row"><span class="msg-details-label">Edited On</span><span class="msg-details-value">${fmtTime(editInfo.edited_ts)}</span></div>`;
      }
      html += '</div>';
    }

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
        if (!html) {
          html = '<span class="msg-details-na">No receipt data available.</span>';
        }
      } else if (data.members.length === 1) {
        html +=
          `<div class="msg-details-row"><span class="msg-details-label">Delivered</span><span class="msg-details-value">${fmtReceipt(data.delivered_ts)}</span></div>` +
          `<div class="msg-details-row"><span class="msg-details-label">Read</span><span class="msg-details-value">${fmtRead(data)}</span></div>`;
      } else {
        let rows = '';
        for (const m of data.members) {
          rows += `<tr><td>${esc(m.name || m.jid || '?')}</td><td>${fmtReceipt(m.delivered_ts)}</td><td>${fmtRead(m)}</td></tr>`;
        }
        html += `<table class="msg-details-table"><thead><tr><th>Member</th><th>Delivered</th><th>Read</th></tr></thead><tbody>${rows}</tbody></table>`;
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

  async function showReactionDetails(msgId, anchorEl) {
    msgDetailsPopup.innerHTML = '<span style="color:var(--text-muted)">Loading…</span>';
    positionPopup(anchorEl);
    msgDetailsPopup.classList.add('visible');

    let data;
    try {
      const res = await fetch('/api/reaction_details/' + msgId);
      data = await res.json();
    } catch (_) {
      data = { available: false };
    }

    let html;
    if (!data.available || !data.reactors || !data.reactors.length) {
      html = '<span class="msg-details-na">No reaction data available.</span>';
    } else {
      const label = data.total === 1 ? '1 reaction' : `${data.total} reactions`;
      html = `<div class="rx-details-total">${label}</div>`;
      for (const r of data.reactors) {
        const name = r.from_me ? 'You' : esc(r.name || '?');
        html += `<div class="msg-details-row"><span class="msg-details-label">${esc(r.emoji)}</span><span class="msg-details-value">${name}</span></div>`;
      }
    }

    msgDetailsPopup.innerHTML = html;
    positionPopup(anchorEl);
    msgDetailsPopup.classList.add('visible');
  }

  async function showPollDetails(msgId, anchorEl) {
    msgDetailsPopup.innerHTML = '<span style="color:var(--text-muted)">Loading…</span>';
    positionPopup(anchorEl);
    msgDetailsPopup.classList.add('visible');

    let data;
    try {
      const res = await fetch('/api/poll_details/' + msgId);
      data = await res.json();
    } catch (_) {
      data = { available: false };
    }

    let html;
    if (!data.available || !data.options) {
      html = '<span class="msg-details-na">No poll vote data available.</span>';
    } else {
      const totalVoters = data.total_voters || 0;
      const label = totalVoters === 1 ? '1 voter' : `${totalVoters} voters`;
      html = `<div class="rx-details-total" style="margin-bottom:8px;font-weight:600">Poll votes · ${label}</div>`;

      const getVoteCount = opt =>
        opt.vote_count !== undefined ? opt.vote_count : (opt.voters ? opt.voters.length : 0);

      let maxVotes = 0;
      for (const opt of data.options) {
        const count = getVoteCount(opt);
        if (count > maxVotes) maxVotes = count;
      }

      const sortedOptions = [...data.options].sort((a, b) => getVoteCount(b) - getVoteCount(a));

      let hasAnyVotes = false;
      for (const opt of sortedOptions) {
        const count = getVoteCount(opt);
        if (!count && (!opt.voters || !opt.voters.length)) continue;
        hasAnyVotes = true;
        const isWinner = maxVotes > 0 && count === maxVotes;
        const star = isWinner
          ? '<span class="msg-poll-winner-star" title="Winning option" aria-label="Winner">★</span>'
          : '';
        html += `<div class="msg-poll-details-option-hdr"><span>${esc(opt.name)}${star ? ' ' + star : ''}</span><span class="msg-poll-details-option-count">(${count})</span></div>`;
        if (opt.voters && opt.voters.length) {
          for (const v of opt.voters) {
            const name = esc(v.name || '?');
            const timeStr = v.timestamp_ms ? fmtTime(v.timestamp_ms) : '';
            html += `<div class="msg-details-row"><span class="msg-details-label">${name}</span><span class="msg-details-value">${timeStr}</span></div>`;
          }
        }
      }
      if (!hasAnyVotes) {
        html += '<span class="msg-details-na">No votes recorded yet.</span>';
      }
    }

    msgDetailsPopup.innerHTML = html;
    positionPopup(anchorEl);
    msgDetailsPopup.classList.add('visible');
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
    const sBody = document.getElementById('settings-body');
    if (sBody) sBody.scrollTop = 0;
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
        const res = await fetch('/api/index/optimize', { method: 'POST' });
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

  loadPrefs().then(() => loadChats());
})();
