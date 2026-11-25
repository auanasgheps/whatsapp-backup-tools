// ---- i18n ----------------------------------------------------------------

  const SUPPORTED_LANGS = ['en', 'de', 'fr', 'it', 'es'];
  let _activeLang = (typeof window.INITIAL_LOCALE === 'string' && SUPPORTED_LANGS.includes(window.INITIAL_LOCALE))
    ? window.INITIAL_LOCALE
    : 'en';
  let _localeData = window.LOCALE_DATA || {};
  const _enFallbackData = window.EN_FALLBACK_DATA || {};

  const _SENTINEL_KEYS = {
    __YOU__: 'common.you',
    __SOMEONE__: 'common.someone',
    __A_CONTACT__: 'common.a_contact',
    __THIS_CONTACT__: 'common.this_contact',
    __A_PARTICIPANT__: 'common.a_participant',
  };

  function getActiveLang() {
    return _activeLang;
  }

  function resolveLanguage(prefLang) {
    if (prefLang && prefLang !== 'auto') {
      const base = String(prefLang).split('-')[0].toLowerCase();
      if (SUPPORTED_LANGS.includes(base)) return base;
    }
    const navList = (Array.isArray(navigator.languages) && navigator.languages.length > 0)
      ? navigator.languages
      : (navigator.language ? [navigator.language] : []);
    for (const raw of navList) {
      const base = String(raw || '').split('-')[0].toLowerCase();
      if (SUPPORTED_LANGS.includes(base)) return base;
    }
    return 'en';
  }

  async function ensureLocaleLoaded(langCode) {
    const target = SUPPORTED_LANGS.includes(langCode) ? langCode : 'en';
    if (_activeLang === target && Object.keys(_localeData).length > 0) {
      return;
    }
    try {
      const res = await fetch(API + '/locales/' + encodeURIComponent(target));
      if (res.ok) {
        _localeData = await res.json();
        _activeLang = target;
        document.documentElement.lang = target;
        return;
      }
    } catch (_) {}
    _activeLang = 'en';
    _localeData = _enFallbackData;
    document.documentElement.lang = 'en';
  }

  function _lookupKey(obj, key) {
    if (!obj || !key) return undefined;
    let cur = obj;
    const parts = key.split('.');
    for (let i = 0; i < parts.length; i++) {
      if (cur == null || typeof cur !== 'object' || !(parts[i] in cur)) return undefined;
      cur = cur[parts[i]];
    }
    return typeof cur === 'string' ? cur : undefined;
  }

  function _resolveSentinel(val) {
    if (typeof val === 'string' && val in _SENTINEL_KEYS) {
      return t(_SENTINEL_KEYS[val]);
    }
    return val;
  }

  function t(key, params) {
    let str = _lookupKey(_localeData, key);
    if (str === undefined) str = _lookupKey(_enFallbackData, key);
    if (str === undefined) return key;
    if (params && typeof params === 'object') {
      for (const [k, rawVal] of Object.entries(params)) {
        const val = _resolveSentinel(rawVal);
        str = str.replace(new RegExp('\\{' + k + '\\}', 'g'), String(val ?? ''));
      }
    }
    return str;
  }

  function tp(baseKey, count, params) {
    let cat = 'other';
    try {
      cat = new Intl.PluralRules(_activeLang).select(Number(count) || 0);
    } catch (_) {
      cat = Number(count) === 1 ? 'one' : 'other';
    }
    const variantKey = baseKey + '_' + (cat === 'one' ? 'one' : 'other');
    const merged = Object.assign({ count }, params || {});
    const resolved = _lookupKey(_localeData, variantKey) ?? _lookupKey(_enFallbackData, variantKey);
    if (resolved !== undefined) {
      return t(variantKey, merged);
    }
    return t(baseKey + '_other', merged);
  }

  function formatSystemEventText(msg) {
    if (msg && msg.system_event && msg.system_event.key) {
      const ev = msg.system_event;
      const params = ev.params || {};
      if (params.plural && params.count !== undefined) {
        return tp(ev.key, params.count, params);
      }
      return t(ev.key, params);
    }
    return (msg && msg.text_body) || '';
  }

  function localizeSender(name) {
    if (name === 'You') return t('common.you');
    if (name === 'Unknown') return t('common.unknown');
    return name || '';
  }

  function applyDOMTranslations() {
    document.querySelectorAll('[data-i18n]').forEach(el => {
      const key = el.getAttribute('data-i18n');
      if (key) el.textContent = t(key);
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
      const key = el.getAttribute('data-i18n-placeholder');
      if (key) el.setAttribute('placeholder', t(key));
    });
    document.querySelectorAll('[data-i18n-title]').forEach(el => {
      const key = el.getAttribute('data-i18n-title');
      if (key) el.setAttribute('title', t(key));
    });
    document.querySelectorAll('[data-i18n-aria]').forEach(el => {
      const key = el.getAttribute('data-i18n-aria');
      if (key) el.setAttribute('aria-label', t(key));
    });
  }
