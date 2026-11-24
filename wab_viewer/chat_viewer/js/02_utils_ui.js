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
      const res = await fetch(API + '/calls?limit=1');
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

  // ---- chat header overflow menu -------------------------------------------

  function openChatMenu() {
    const dropdown = document.getElementById('chat-menu-dropdown');
    const btn = document.getElementById('chat-menu-btn');
    if (!dropdown || !btn) return;
    dropdown.style.display = 'block';
    requestAnimationFrame(() => {
      dropdown.classList.add('open');
    });
    btn.setAttribute('aria-expanded', 'true');
    btn.classList.add('active');
    const firstItem = dropdown.querySelector('.chat-menu-item');
    if (firstItem) {
      firstItem.focus();
    }
  }

  function closeChatMenu() {
    const dropdown = document.getElementById('chat-menu-dropdown');
    const btn = document.getElementById('chat-menu-btn');
    if (!dropdown || !btn) return;
    if (!dropdown.classList.contains('open') && dropdown.style.display === 'none') return;
    dropdown.classList.remove('open');
    btn.setAttribute('aria-expanded', 'false');
    btn.classList.remove('active');
    setTimeout(() => {
      if (!dropdown.classList.contains('open')) {
        dropdown.style.display = 'none';
      }
    }, 140);
  }

  function toggleChatMenu() {
    const dropdown = document.getElementById('chat-menu-dropdown');
    if (dropdown && dropdown.classList.contains('open')) {
      closeChatMenu();
    } else {
      openChatMenu();
    }
  }

  const chatMenuBtn = document.getElementById('chat-menu-btn');
  if (chatMenuBtn) {
    chatMenuBtn.addEventListener('click', e => {
      e.stopPropagation();
      toggleChatMenu();
    });
    chatMenuBtn.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown') {
        e.preventDefault();
        openChatMenu();
      }
    });
  }

  const chatMenuDropdown = document.getElementById('chat-menu-dropdown');
  if (chatMenuDropdown) {
    chatMenuDropdown.addEventListener('keydown', e => {
      const items = Array.from(chatMenuDropdown.querySelectorAll('.chat-menu-item'));
      if (items.length === 0) return;
      const activeIdx = items.indexOf(document.activeElement);

      if (e.key === 'Escape') {
        e.preventDefault();
        closeChatMenu();
        const btn = document.getElementById('chat-menu-btn');
        if (btn) btn.focus();
      } else if (e.key === 'ArrowDown') {
        e.preventDefault();
        const nextIdx = (activeIdx + 1) % items.length;
        items[nextIdx].focus();
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        const prevIdx = (activeIdx - 1 + items.length) % items.length;
        items[prevIdx].focus();
      } else if (e.key === 'Tab') {
        closeChatMenu();
      }
    });
  }
