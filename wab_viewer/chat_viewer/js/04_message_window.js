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
