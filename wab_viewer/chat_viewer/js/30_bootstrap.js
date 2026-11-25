loadPrefs().then(() => loadChats()).then(() => {
  try {
    const raw = sessionStorage.getItem('wab_reopen_chat');
    if (raw) {
      sessionStorage.removeItem('wab_reopen_chat');
      const target = JSON.parse(raw);
      const match = (allChats || []).find(c => c.id === target.id && c.type === target.type);
      if (match) selectChat(match);
    }
  } catch (_) {}
});
