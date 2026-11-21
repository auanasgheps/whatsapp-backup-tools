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
    const parts = (archivePath || '').replace(/\\/g, '/').split('/');
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

    const typesKey = activeTypes.size > 0 ? [...activeTypes].sort().join(',') : 'all';
    const isDone = archiveAllLoaded || (activeTypes.size === 0 && galleryAllLoaded) || typesAllLoaded.has(typesKey);

    if (!filtered.length) {
      tree.innerHTML = isDone
        ? '<div class="archive-empty-notice" style="color:var(--text-muted);padding:16px">No media in this chat.</div>'
        : '<div class="gallery-filter-loading"><span class="gallery-filter-spinner"></span><span>Loading media…</span></div>';
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
    // clear empty notice and loader placeholders if present
    tree.querySelectorAll('.archive-empty-notice, .gallery-filter-loading').forEach(el => el.remove());

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
