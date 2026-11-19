const outputRoot = window.OUTPUT_ROOT;
  // Prefix the viewer is served under (--base-path), '' at the root. Every request to the
  // viewer's own routes is built from API and MEDIA, never from a bare /api or /media string.
  const BASE_PATH = window.BASE_PATH || '';
  const API = BASE_PATH + '/api';
  const MEDIA = BASE_PATH + '/media/';
  let allChats = [];
  let currentChat = null;
  let msgList = [];
  let suppressScroll = false;
  let prefs = { theme: 'dark', date_format: 'DD/MM/YYYY', font_size: 'medium' };

  async function loadPrefs() {
    const res = await fetch(API + '/preferences');
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
