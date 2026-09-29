/*
 * DNS Bench — web UI.
 * Vanilla ES2022, no dependencies, no build step. Charts are hand-built SVG.
 *
 * How it fits together:
 * - The server owns every rule (GET /api/schema, POST /api/config/validate, /api/estimate) and
 *   every analysis; this file keeps presentation only.
 * - `state` is the app's state. Actions (event handlers, the async loaders, ensureDraft, ...) change
 *   it and then call scheduleRender(), which draws once per animation frame.
 * - View builders (viewOverview, viewResolver, viewDomain, viewHistory, viewSettings) read state and
 *   return DOM. They register handles to what they drew in `viewHooks`, never in `state`.
 *
 * Sections, in order: constants, state, DOM helpers, utilities (schema, formatting, scales), API,
 * banners & toasts, colours, tooltip, chart infrastructure, generic pieces, dataset helpers,
 * estimate, run controls & progress, dataset loading, trend, the five views, empty & shell,
 * drawing, routing, bootstrap.
 *
 * Safety: every piece of text that can come from data (resolver names,
 * domains, IPs, error messages) is inserted through the h()/s() helpers,
 * which use createElement + textContent / createTextNode. innerHTML is
 * never used anywhere in this file.
 */
// biome-ignore lint/suspicious/noRedundantUseStrict: index.html loads this as a classic script, not a module, so strict mode is not implied.
'use strict';

(() => {
  // ================================================================ constants
  const TABS = [
    { id: 'overview', label: 'Overview' },
    { id: 'resolver', label: 'By resolver' },
    { id: 'domain', label: 'By domain' },
    { id: 'history', label: 'History' },
    { id: 'settings', label: 'Settings' },
  ];
  const TAB_IDS = new Set(TABS.map((t) => t.id));
  const DATA_TABS = new Set(['overview', 'resolver', 'domain']);
  const PALETTE_SIZE = 8;
  const POLL_MS = 500;
  const POLL_BACKOFF_MAX_MS = 10000; // slowest retry while the server doesn't answer
  const POLL_WARN_AFTER = 3; // failed polls in a row before saying so
  const API_TIMEOUT_MS = 15000; // a request the server hasn't answered by then is given up
  const TREND_MAX_RUNS = 30;
  const DOMAIN_CHART_LIMIT = 60;
  const SLOW_TABLE_MAX = 200; // rows shown in a resolver's "Slow queries" table
  const FAILED_TABLE_MAX = 200; // rows drawn in a resolver's "Failed queries" table (a run can have 20,000)
  const RUN_CACHE_MAX = 6; // full run records kept in memory

  // How each setting is presented in Settings, in this order. Its type, bounds and default come
  // from GET /api/schema (state.schema), so the rules themselves are never copied here.
  const SETTING_TEXT = {
    per_server_interval_ms: {
      label: 'Per-server interval',
      step: 10,
      help: 'Minimum gap between two queries to the same server. This is the politeness limit: 250 ms means at most 4 queries per second to any one server.',
    },
    rounds: {
      label: 'Rounds',
      step: 1,
      help: "How many times each domain is looked up on each server. Repeats are mostly answered from the resolver's cache, so latency figures use only each domain's first answer; more rounds sharpen the failure rates. For steadier latency, combine runs taken at different times.",
    },
    timeout_ms: {
      label: 'Timeout',
      step: 50,
      help: 'How long to wait for an answer before the query counts as a timeout.',
    },
    tries: {
      label: 'Tries per query',
      step: 1,
      help: "With 1, a dropped packet counts as a failure. With 2 or 3, a query that only answered on a retry counts as retried: its latency is the retry's round trip, and it is penalised in the score because the first attempt cost a full timeout.",
    },
    slow_threshold_ms: {
      label: 'Slow threshold',
      step: 10,
      help: 'Answers slower than this are flagged as slow.',
    },
    record_type: {
      label: 'Record type',
      options: { A: 'A (IPv4 address)', AAAA: 'AAAA (IPv6 address)' },
      help: 'Which DNS record type to ask for.',
    },
    shuffle: {
      label: 'Shuffle domain order',
      help: 'Each server gets the domains in its own random order, so two servers of one provider never ask for the same name at the same moment.',
    },
  };
  const SETTING_ORDER = Object.keys(SETTING_TEXT);

  // ================================================================ state
  const state = {
    bootstrapped: false,
    schema: null, // GET /api/schema: defaults, setting types and bounds, limits, presets
    config: null,
    configError: null,
    configErrors: [], // the server's validation of the SAVED config (it may be hand-edited)
    configInvalid: false, // the Settings draft was built from a saved config with problems
    estimate: null, // the server's estimate of a run of the saved config
    runEstimate: null, // the same with the header's rounds override (POST /api/estimate)
    draftCheck: null, // POST /api/config/validate of the Settings draft: {config, errors, estimate, ...}
    info: null, // GET /api/info: where the server keeps config.json and runs/
    runs: [],
    runCache: new Map(),
    aggCache: null,
    datasetKey: 'latest',
    dataset: null,
    datasetError: null,
    dsToken: 0,
    colors: new Map(),
    route: { tab: 'overview', arg: null },
    focusAfterRender: null, // {sel, tab}: what to focus once that tab's view is drawn (an action's own control)
    tabKeyNav: false, // the arrow keys moved between tabs: focus stays on the tab
    job: { running: false },
    pollTimer: null,
    pollFailures: 0,
    live: { seen: new Set(), slow: 0, failed: 0, last: null },
    cancelling: false,
    roundsTouched: false,
    sorts: {},
    ui: { domainQuery: '', domainSort: 'list', lastResolver: null, showAllDomains: false },
    draft: null,
    dirty: false,
    saving: false,
    saveErrors: null,
  };
  const els = {};
  // Handles into the view on screen, set by the view builders and cleared by render(). They are not
  // app state: they only let an update reach the DOM that is already drawn.
  const viewHooks = {
    update: null, // (arg) => true if the view updated itself in place for a new route arg
    settings: null, // Settings: { refresh() } redraws its status line and estimate
    trendSlot: null, // Overview: the trend chart's box, filled when the run history arrives
  };
  let FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif';

  // ================================================================ DOM helpers
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const PROP_ATTRS = new Set(['value', 'checked', 'disabled', 'selected', 'indeterminate', 'readOnly']);

  function setAttrs(el, attrs) {
    if (!attrs) return el;
    for (const key of Object.keys(attrs)) {
      const v = attrs[key];
      if (v === null || v === undefined || v === false) continue;
      if (key === 'class') el.setAttribute('class', String(v));
      else if (key === 'text') el.textContent = String(v);
      else if (key === 'style') {
        // CSSOM only (never a style="" string) so values are never parsed as markup.
        for (const p of Object.keys(v))
          if (v[p] !== null && v[p] !== undefined) el.style.setProperty(p, String(v[p]));
      } else if (key === 'dataset') {
        for (const d of Object.keys(v)) el.dataset[d] = String(v[d]);
      } else if (key.startsWith('on') && typeof v === 'function') {
        el.addEventListener(key.slice(2).toLowerCase(), v);
      } else if (PROP_ATTRS.has(key)) el[key] = v;
      else el.setAttribute(key, v === true ? '' : String(v));
    }
    return el;
  }

  function appendKids(el, kids) {
    for (const k of kids) {
      if (k === null || k === undefined || k === false || k === true) continue;
      if (Array.isArray(k)) appendKids(el, k);
      else if (k instanceof Node) el.appendChild(k);
      else el.appendChild(document.createTextNode(String(k)));
    }
  }

  /** h('div', {class: 'x', onClick: fn}, 'text', child, [more]) — HTML element. */
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    setAttrs(el, attrs);
    appendKids(el, kids);
    return el;
  }

  /** Replace an element's children; skips null/false and flattens arrays (unlike replaceChildren). */
  function setKids(el, ...kids) {
    while (el.firstChild) el.removeChild(el.firstChild);
    appendKids(el, kids);
    return el;
  }

  /** s('rect', {...}) — SVG element. */
  function s(tag, attrs, ...kids) {
    const el = document.createElementNS(SVG_NS, tag);
    setAttrs(el, attrs);
    appendKids(el, kids);
    return el;
  }

  const ICONS = {
    copy: [
      ['rect', { x: 9, y: 9, width: 11, height: 11, rx: 2 }],
      ['path', { d: 'M5 15V6a2 2 0 0 1 2-2h9' }],
    ],
    check: [['path', { d: 'M5 12.5l4.5 4.5L19 7.5' }]],
    x: [['path', { d: 'M6 6l12 12M18 6L6 18' }]],
    info: [
      ['circle', { cx: 12, cy: 12, r: 9 }],
      ['path', { d: 'M12 11v5.5M12 7.6v.1' }],
    ],
    alert: [
      ['path', { d: 'M10.3 4.2L2.8 17.5A2 2 0 0 0 4.5 20.5h15a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0z' }],
      ['path', { d: 'M12 9.5v4M12 17v.1' }],
    ],
    play: [['path', { d: 'M8 5.5v13l10.5-6.5z', fill: 'currentColor' }]],
    stop: [['rect', { x: 6.5, y: 6.5, width: 11, height: 11, rx: 1.5, fill: 'currentColor' }]],
    download: [['path', { d: 'M12 4v11M7 10.5l5 5 5-5M5 20h14' }]],
    plus: [['path', { d: 'M12 5v14M5 12h14' }]],
    search: [
      ['circle', { cx: 11, cy: 11, r: 6.5 }],
      ['path', { d: 'M20 20l-4.2-4.2' }],
    ],
    refresh: [
      ['path', { d: 'M20 12a8 8 0 1 1-2.35-5.65' }],
      ['path', { d: 'M20 4.5V9h-4.5' }],
    ],
    arrow: [['path', { d: 'M5 12h14M13 6l6 6-6 6' }]],
    clock: [
      ['circle', { cx: 12, cy: 12, r: 9 }],
      ['path', { d: 'M12 7v5l3 2' }],
    ],
    layers: [
      ['path', { d: 'M12 3l9 5-9 5-9-5 9-5z' }],
      ['path', { d: 'M3 13l9 5 9-5' }],
    ],
  };

  function icon(name, size = 16) {
    const svg = s('svg', {
      class: 'icon',
      viewBox: '0 0 24 24',
      width: size,
      height: size,
      'aria-hidden': 'true',
      focusable: 'false',
      fill: 'none',
      stroke: 'currentColor',
      'stroke-width': 2,
      'stroke-linecap': 'round',
      'stroke-linejoin': 'round',
    });
    for (const [tag, attrs] of ICONS[name] || []) svg.appendChild(s(tag, attrs));
    return svg;
  }

  // ================================================================ utilities
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  /** 'smooth' scrolling unless the user asked the system for less motion. */
  const scrollBehavior = () =>
    window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const r2 = (v) => Math.round(v * 100) / 100;

  function debounce(fn, ms) {
    let t = null;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), ms);
    };
  }
  function mean(arr) {
    return arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : null;
  }
  function nearestRank(sorted, p) {
    const n = sorted.length;
    if (!n) return null;
    return sorted[clamp(Math.ceil((p / 100) * n), 1, n) - 1];
  }
  /** Compare with null/undefined/NaN always sorted last, whatever the direction. */
  function cmpNullLast(a, b, dir = 1) {
    const an = a === null || a === undefined || (typeof a === 'number' && !Number.isFinite(a));
    const bn = b === null || b === undefined || (typeof b === 'number' && !Number.isFinite(b));
    if (an && bn) return 0;
    if (an) return 1;
    if (bn) return -1;
    if (typeof a === 'string' || typeof b === 'string') {
      return dir * String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });
    }
    return dir * (a - b);
  }
  function hashFor(tab, arg) {
    return `#${tab}${arg ? `/${encodeURIComponent(arg)}` : ''}`;
  }
  function go(tab, arg) {
    location.hash = hashFor(tab, arg);
  }

  // ---------------------------------------------------------------- schema
  /** The settings from the schema, in display order, each with its presentation (label, help, ...). */
  function settingFields() {
    const list = Array.isArray(state.schema?.settings) ? state.schema.settings : [];
    const rank = (key) => {
      const i = SETTING_ORDER.indexOf(key);
      return i < 0 ? SETTING_ORDER.length : i;
    };
    return list
      .slice()
      .sort((a, b) => rank(a.key) - rank(b.key))
      .map((s) => ({ ...s, ...(SETTING_TEXT[s.key] || { label: s.key, help: '' }) }));
  }
  function settingSchema(key) {
    return (
      (Array.isArray(state.schema?.settings) ? state.schema.settings : []).find((s) => s.key === key) || null
    );
  }
  /** A setting's value in a run's (or the saved) config, else its default. */
  function settingOf(settings, key) {
    if (settings && Object.hasOwn(settings, key)) return settings[key];
    return settingSchema(key)?.default;
  }

  // ---------------------------------------------------------------- formatting
  function fmtNum(v, dp = 1) {
    if (!isNum(v)) return '—';
    return v.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp });
  }
  function fmtMs(v) {
    if (!isNum(v)) return '—';
    return fmtNum(v, Math.abs(v) >= 100 ? 0 : 1);
  }
  function fmtMsU(v) {
    return isNum(v) ? `${fmtMs(v)} ms` : '—';
  }
  function fmtInt(v) {
    return isNum(v) ? Math.round(v).toLocaleString() : '—';
  }
  function fmtPct(rate) {
    if (!isNum(rate)) return '—';
    if (rate === 0) return '0%';
    const p = rate * 100;
    if (p < 0.1) return '<0.1%';
    return `${fmtNum(p, p >= 10 ? 0 : 1)}%`;
  }
  function fmtQps(q) {
    if (!isNum(q)) return '—';
    const r = Math.round(q * 10) / 10;
    return Number.isInteger(r) ? String(r) : r.toFixed(1);
  }
  function fmtDuration(sec) {
    if (!isNum(sec)) return '—';
    if (sec < 1) return '<1 s';
    if (sec < 10) return `${sec.toFixed(1)} s`;
    if (sec < 59.5) return `${Math.round(sec)} s`;
    const total = Math.round(sec);
    const m = Math.floor(total / 60);
    const r = total % 60;
    if (m < 60) return r ? `${m} min ${r} s` : `${m} min`;
    return `${Math.floor(m / 60)} h ${m % 60} min`;
  }
  function fmtDate(iso, style) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    if (style === 'short')
      return d.toLocaleString(undefined, {
        day: 'numeric',
        month: 'short',
        hour: 'numeric',
        minute: '2-digit',
      });
    return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  }
  function capitalize(str) {
    str = String(str);
    return str.charAt(0).toUpperCase() + str.slice(1);
  }
  function plural(n, one, many) {
    return `${fmtInt(n)} ${n === 1 ? one : many || `${one}s`}`;
  }
  function decimalsOf(step) {
    const str = String(+Number(step).toPrecision(6));
    const i = str.indexOf('.');
    return i < 0 ? 0 : Math.min(4, str.length - i - 1);
  }
  function fmtTick(v, step) {
    return v.toLocaleString(undefined, { maximumFractionDigits: decimalsOf(step) });
  }
  function fmtPctTick(v, step) {
    return `${(v * 100).toLocaleString(undefined, { maximumFractionDigits: decimalsOf(step * 100) })}%`;
  }

  // ---------------------------------------------------------------- scales
  function niceCeil(x) {
    if (!(x > 0)) return 1;
    const e = Math.floor(Math.log10(x));
    const f = x / 10 ** e;
    const nf = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
    return nf * 10 ** e;
  }
  function niceTicks(max, target = 5, minMax = 0, integer = false) {
    let m = Math.max(isNum(max) ? max : 0, minMax || 0);
    if (!(m > 0)) m = 1;
    let step = niceCeil(m / target);
    if (integer) step = Math.max(1, Math.round(step));
    let top = Math.ceil(m / step - 1e-9) * step;
    // With only a couple of ticks, rounding up a whole step can waste half the plot
    // (max 539 -> axis 0-1,000). Use the next finer nice step if one more interval fits.
    if (top > m * 1.3) {
      let finer = niceCeil(step / 2.2);
      if (integer) finer = Math.max(1, Math.round(finer));
      const finerTop = Math.ceil(m / finer - 1e-9) * finer;
      if (finer < step && finerTop < top && Math.round(finerTop / finer) <= target + 1) {
        step = finer;
        top = finerTop;
      }
    }
    const ticks = [];
    for (let i = 0; i * step <= top + step * 1e-6; i++) ticks.push(+(i * step).toFixed(10));
    return { max: top, step, ticks };
  }

  // ---------------------------------------------------------------- text measuring
  let measureCtx = null;
  function textWidth(str, font) {
    if (!measureCtx) measureCtx = document.createElement('canvas').getContext('2d');
    if (!measureCtx) return String(str).length * 7;
    measureCtx.font = font;
    return measureCtx.measureText(String(str)).width;
  }
  function fitText(str, maxW, font) {
    str = String(str);
    if (textWidth(str, font) <= maxW) return str;
    let lo = 1;
    let hi = str.length;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (textWidth(`${str.slice(0, mid)}…`, font) <= maxW) lo = mid;
      else hi = mid - 1;
    }
    return `${str.slice(0, lo)}…`;
  }

  // ================================================================ API
  class ApiError extends Error {
    constructor(message, status, details) {
      super(message);
      this.status = status;
      this.details = details || null;
    }
  }

  async function api(path, opts = {}) {
    // A server that accepts the connection but never answers would otherwise hang the page (FE-7).
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), API_TIMEOUT_MS);
    const init = {
      method: opts.method || 'GET',
      headers: { Accept: 'application/json' },
      cache: 'no-store',
      signal: abort.signal,
    };
    if (init.method !== 'GET') {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body === undefined ? {} : opts.body);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch (err) {
      clearTimeout(timer);
      throw new ApiError(
        err?.name === 'AbortError'
          ? `The DNS Bench server did not answer within ${API_TIMEOUT_MS / 1000} s.`
          : 'Cannot reach the DNS Bench server. Is it still running?',
        0,
      );
    }
    // The server answered: a body that then can't be read is an empty one, not a lost server.
    const text = await res.text().catch(() => '');
    clearTimeout(timer);
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (_) {
        data = null;
      }
    }
    if (!res.ok) {
      const msg =
        (data && typeof data.error === 'string' && data.error) ||
        `Request failed (${res.status} ${res.statusText || ''})`.replace(/\s+\)/, ')');
      throw new ApiError(msg, res.status, data && Array.isArray(data.details) ? data.details : null);
    }
    return data;
  }

  async function loadRun(id) {
    const cache = state.runCache;
    if (cache.has(id)) {
      const hit = cache.get(id);
      cache.delete(id); // re-insert: Map order doubles as least-recently-used order
      cache.set(id, hit);
      return hit;
    }
    const run = await api(`/api/runs/${encodeURIComponent(id)}`);
    cache.set(id, run);
    // Full records can be megabytes each: keep only a few, never the one on screen.
    const keep = state.dataset && state.dataset.kind === 'run' ? state.dataset.id : null;
    for (const key of cache.keys()) {
      if (cache.size <= RUN_CACHE_MAX) break;
      if (key !== keep && key !== id) cache.delete(key);
    }
    return run;
  }

  async function loadRuns() {
    try {
      const res = await api('/api/runs');
      state.runs = Array.isArray(res?.runs) ? res.runs : [];
      return true;
    } catch (e) {
      showBanner(`Could not load saved runs: ${e.message}`);
      return false;
    }
  }

  // ================================================================ banners & toasts
  /** action: optional {href, label} link shown under the message. */
  function showBanner(message, kind = 'error', details, action) {
    const msg = String(message);
    for (const b of els.banners.children) if (b.dataset.msg === msg) return;
    const banner = h(
      'div',
      { class: `banner banner-${kind}`, role: kind === 'error' ? 'alert' : 'status', dataset: { msg } },
      icon(kind === 'error' ? 'alert' : 'info', 18),
      h(
        'div',
        { class: 'banner-body' },
        h('div', { class: 'banner-msg' }, msg),
        details?.length
          ? h(
              'ul',
              { class: 'banner-details' },
              details.map((d) => h('li', null, String(d?.message ?? d))),
            )
          : null,
        action ? h('a', { class: 'btn btn-sm banner-action', href: action.href }, action.label) : null,
      ),
      h(
        'button',
        {
          type: 'button',
          class: 'btn btn-icon btn-ghost banner-close',
          'aria-label': 'Dismiss message',
          onClick: () => banner.remove(),
        },
        icon('x'),
      ),
    );
    els.banners.appendChild(banner);
  }
  function clearBanners(prefix) {
    for (const b of Array.from(els.banners.children))
      if (String(b.dataset.msg || '').startsWith(prefix)) b.remove();
  }

  function toast(message, kind = 'ok') {
    const t = h(
      'div',
      { class: `toast toast-${kind}` },
      icon(kind === 'ok' ? 'check' : 'info'),
      h('span', null, message),
    );
    els.toasts.appendChild(t);
    requestAnimationFrame(() => t.classList.add('is-in'));
    setTimeout(() => {
      t.classList.remove('is-in');
      setTimeout(() => t.remove(), 300);
    }, 4500);
  }

  // ================================================================ colours
  // Colour follows the resolver, never its rank: slots are handed out in config
  // order (then first appearance), so filtering or re-sorting never repaints.
  function resetColors() {
    state.colors = new Map();
    for (const r of state.config?.resolvers || []) colorIndex(r.name);
  }
  function colorIndex(name) {
    const key = String(name);
    if (!state.colors.has(key)) state.colors.set(key, state.colors.size);
    return state.colors.get(key);
  }
  function slotColor(i) {
    return i < PALETTE_SIZE ? `var(--s${i + 1})` : 'var(--s-other)';
  }
  function colorOf(name) {
    return slotColor(colorIndex(name));
  }
  /** A resolver's colour if it already has one; a neutral one for a name typed but not saved yet. */
  function knownColor(name) {
    const i = state.colors.get(String(name));
    return i === undefined ? 'var(--s-other)' : slotColor(i);
  }
  function dot(name, cls) {
    return h('span', {
      class: `dot${cls ? ` ${cls}` : ''}`,
      style: { background: colorOf(name) },
      'aria-hidden': 'true',
    });
  }

  // ================================================================ tooltip
  // Pointer tooltips follow the cursor; keyboard (focus) tooltips stay anchored to
  // the focused mark, so scrolling it into view repositions rather than hides them.
  let tipAnchor = null;
  function showTip(evt, content) {
    const tip = els.tooltip;
    setKids(tip, ...content);
    tip.hidden = false;
    tipAnchor = evt && evt.type === 'focus' ? evt.currentTarget : null;
    positionTip(evt);
  }
  function anchorPoint(el) {
    const r = el.getBoundingClientRect();
    return [r.left + Math.min(r.width, 240) / 2, r.top + r.height / 2];
  }
  function onScrollTip() {
    if (els.tooltip.hidden) return;
    if (tipAnchor?.isConnected) placeTip(...anchorPoint(tipAnchor));
    else hideTip();
  }
  function positionTip(evt) {
    if (els.tooltip.hidden) return;
    if (evt && typeof evt.clientX === 'number' && (evt.clientX || evt.clientY))
      placeTip(evt.clientX, evt.clientY);
    else if (evt?.currentTarget?.getBoundingClientRect) placeTip(...anchorPoint(evt.currentTarget));
  }
  function placeTip(x, y) {
    const tip = els.tooltip;
    const tw = tip.offsetWidth;
    const th = tip.offsetHeight;
    let left = x + 14;
    let top = y + 16;
    if (left + tw > window.innerWidth - 8) left = x - tw - 14;
    if (left < 8) left = 8;
    if (top + th > window.innerHeight - 8) top = y - th - 12;
    if (top < 8) top = 8;
    tip.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
  }
  function hideTip() {
    tipAnchor = null;
    if (els.tooltip) els.tooltip.hidden = true;
  }
  /** True when focus came from the keyboard (not a mouse click). */
  function isFocusVisible(el) {
    try {
      return el.matches(':focus-visible');
    } catch (_) {
      return true;
    }
  }
  function bindTip(el, contentFn) {
    const show = (e) => showTip(e, contentFn());
    el.addEventListener('pointerenter', show);
    el.addEventListener('pointermove', positionTip);
    el.addEventListener('pointerleave', hideTip);
    el.addEventListener('focus', show);
    el.addEventListener('blur', hideTip);
  }
  /** rows: [value, label, colour?]. Values lead, labels follow. */
  /**
   * One tab stop for a group of items, such as a chart's bars (FE-10): Tab reaches the group once,
   * then the arrow keys, Home and End move between the items. The selected item, else the first,
   * starts as the tab stop.
   */
  function roving(container, items, prevKeys, nextKeys) {
    if (!items.length) return;
    const start = Math.max(
      0,
      items.findIndex((el) => el.classList.contains('is-selected')),
    );
    for (const [i, el] of items.entries()) el.setAttribute('tabindex', i === start ? '0' : '-1');
    container.addEventListener('keydown', (e) => {
      const i = items.indexOf(e.target);
      if (i < 0) return;
      let j = null;
      if (prevKeys.includes(e.key)) j = Math.max(0, i - 1);
      else if (nextKeys.includes(e.key)) j = Math.min(items.length - 1, i + 1);
      else if (e.key === 'Home') j = 0;
      else if (e.key === 'End') j = items.length - 1;
      if (j === null || j === i) return;
      e.preventDefault();
      items[i].setAttribute('tabindex', '-1');
      items[j].setAttribute('tabindex', '0');
      items[j].focus();
    });
  }

  function tipContent(title, rows, color) {
    return [
      h(
        'div',
        { class: 'tip-title' },
        color ? h('span', { class: 'dot', style: { background: color } }) : null,
        h('span', null, title),
      ),
      h(
        'div',
        { class: 'tip-grid' },
        rows
          .filter(Boolean)
          .map(([v, l, c]) => [
            h(
              'span',
              { class: 'tip-val' },
              c ? h('span', { class: 'tip-key', style: { background: c } }) : null,
              v,
            ),
            h('span', { class: 'tip-label' }, l),
          ]),
      ),
    ];
  }
  function statTip(title, st, rank, color) {
    st = st || {};
    return tipContent(
      title,
      [
        [fmtMsU(st.mean), 'mean'],
        [fmtMsU(st.median), 'median'],
        [fmtMsU(st.p95), '95th percentile'],
        [`${fmtPct(st.failure_rate)} (${fmtInt(st.failures)} of ${fmtInt(st.n)})`, 'failed'],
        rank ? [`#${rank.rank}`, `rank · score ${fmtNum(rank.score, 1)}`] : null,
      ],
      color,
    );
  }

  // ================================================================ chart infrastructure
  let pendingCharts = [];
  const chartRO =
    'ResizeObserver' in window
      ? new ResizeObserver((entries) => {
          for (const entry of entries) {
            const box = entry.target;
            const w = Math.floor(entry.contentRect.width);
            if (w > 0 && w !== box._w) drawChart(box, w);
          }
        })
      : null;

  /** A container whose SVG is (re)drawn at its real pixel width. */
  function chartBox(renderFn, cls) {
    const box = h('div', { class: `chart${cls ? ` ${cls}` : ''}` });
    box._render = renderFn;
    pendingCharts.push(box);
    return box;
  }
  function drawChart(box, w) {
    box._w = w;
    hideTip();
    // A redraw replaces every bar; a keyboard user inside the chart stays on the same one.
    const items = () => Array.from(box.querySelectorAll('[tabindex]'));
    const had = box.contains(document.activeElement) ? items().indexOf(document.activeElement) : -1;
    let node;
    try {
      node = box._render(w);
    } catch (err) {
      console.error(err);
      node = h('div', { class: 'chart-placeholder' }, 'This chart could not be drawn.');
    }
    setKids(box, node);
    if (had < 0) return;
    const now = items();
    const el = now[Math.min(had, now.length - 1)];
    if (!el) return;
    for (const x of now) if (x !== el && x.getAttribute('tabindex') === '0') x.setAttribute('tabindex', '-1');
    el.setAttribute('tabindex', '0');
    el.focus({ preventScroll: true });
  }
  function flushCharts() {
    const list = pendingCharts;
    pendingCharts = [];
    for (const box of list) {
      if (!box.isConnected) {
        pendingCharts.push(box);
        continue;
      }
      const w = Math.floor(box.clientWidth);
      if (w > 0) drawChart(box, w);
      if (chartRO) chartRO.observe(box);
    }
  }

  function hbarPath(x, y, w, hgt) {
    const r = Math.max(0, Math.min(4, w / 2, hgt / 2));
    const xr = x + w;
    return `M${r2(x)},${r2(y)}H${r2(xr - r)}A${r},${r} 0 0 1 ${r2(xr)},${r2(y + r)}V${r2(y + hgt - r)}A${r},${r} 0 0 1 ${r2(xr - r)},${r2(y + hgt)}H${r2(x)}Z`;
  }
  function vbarPath(x, y, w, hgt) {
    const r = Math.max(0, Math.min(4, w / 2, hgt));
    return `M${r2(x)},${r2(y + hgt)}V${r2(y + r)}A${r},${r} 0 0 1 ${r2(x + r)},${r2(y)}H${r2(x + w - r)}A${r},${r} 0 0 1 ${r2(x + w)},${r2(y + r)}V${r2(y + hgt)}Z`;
  }

  function legend(items) {
    return h(
      'div',
      { class: 'legend' },
      items.map((it) =>
        h('span', { class: 'legend-item' }, swatch(it.kind, it.color), h('span', null, it.label)),
      ),
    );
  }
  function swatch(kind, color) {
    const svg = s('svg', {
      class: 'swatch',
      width: 18,
      height: 12,
      viewBox: '0 0 18 12',
      'aria-hidden': 'true',
    });
    if (kind === 'bar')
      svg.appendChild(s('path', { d: hbarPath(1, 2, 16, 8), style: { fill: color || 'var(--text-2)' } }));
    else if (kind === 'line')
      svg.appendChild(s('line', { x1: 1, x2: 17, y1: 6, y2: 6, class: 'sw-line', style: { stroke: color } }));
    else if (kind === 'tick') {
      svg.appendChild(s('path', { d: hbarPath(1, 2, 16, 8), class: 'sw-muted' }));
      svg.appendChild(s('line', { x1: 9, x2: 9, y1: 0, y2: 12, class: 'median-halo' }));
      svg.appendChild(s('line', { x1: 9, x2: 9, y1: 0.5, y2: 11.5, class: 'median-tick' }));
    } else if (kind === 'whisker') {
      svg.appendChild(s('line', { x1: 2, x2: 16, y1: 6, y2: 6, class: 'whisker' }));
      svg.appendChild(s('line', { x1: 16, x2: 16, y1: 2, y2: 10, class: 'whisker' }));
    } else if (kind === 'threshold')
      svg.appendChild(s('line', { x1: 9, x2: 9, y1: 0, y2: 12, class: 'threshold-line' }));
    else if (kind === 'slow') svg.appendChild(s('path', { d: hbarPath(1, 2, 16, 8), class: 'bar-slow' }));
    return svg;
  }
  function rangeLegend() {
    return legend([
      { kind: 'bar', label: 'Mean' },
      { kind: 'tick', label: 'Median' },
      { kind: 'whisker', label: '95th percentile' },
    ]);
  }

  /**
   * Horizontal bar chart. rows: {label, color, value, median?, p95?, valueText?, valueUnit?,
   * extra?, extraClass?, status?, missingText?, tip?, onClick?, aria?, selected?}
   */
  function barChart(width, rows, opt = {}) {
    const rowH = opt.rowH || 34;
    const barH = Math.min(opt.barH || 16, 24);
    const padTop = 4;
    const axisH = 26;
    const labelFont = `12.5px ${FONT}`;
    const maxLabel = Math.max(36, ...rows.map((r) => textWidth(r.label, labelFont)));
    const labelW = Math.ceil(Math.min(maxLabel + 22, opt.maxLabelW || 220, width * 0.42));
    const valueW = Math.min(opt.valueW || 84, width * 0.3);
    const x0 = labelW;
    const x1 = Math.max(x0 + 40, width - valueW);
    const vals = [];
    for (const r of rows) for (const k of ['value', 'median', 'p95']) if (isNum(r[k])) vals.push(r[k]);
    if (!vals.length && opt.emptyText) return h('div', { class: 'chart-placeholder' }, opt.emptyText);
    const scale = niceTicks(
      vals.length ? Math.max(...vals) : 0,
      clamp(Math.floor((x1 - x0) / 80), 2, 6),
      opt.minMax || 0,
    );
    const X = (v) => x0 + clamp(v / scale.max, 0, 1) * (x1 - x0);
    const plotH = rows.length * rowH;
    const height = padTop + plotH + axisH;
    const svg = s('svg', {
      class: 'svg-chart',
      viewBox: `0 0 ${width} ${height}`,
      width: '100%',
      height,
      role: 'group',
      'aria-label': opt.ariaLabel || null,
    });

    const grid = s('g', { class: 'grid', 'aria-hidden': 'true' });
    const tf = opt.tickFmt || fmtTick;
    const tickLabels = [];
    scale.ticks.forEach((t, i) => {
      const x = Math.round(X(t)) + 0.5;
      grid.appendChild(
        s('line', {
          class: t === 0 ? 'axis-line' : 'grid-line',
          x1: x,
          x2: x,
          y1: padTop,
          y2: padTop + plotH,
        }),
      );
      const last = i === scale.ticks.length - 1;
      tickLabels.push({
        x,
        text: tf(t, scale.step) + (last && opt.unit ? ` ${opt.unit}` : ''),
        anchor: 'middle',
      });
    });
    // Narrow plots: label every other gridline (counting back from the unit-bearing last one).
    const tickGapPx = scale.ticks.length > 1 ? X(scale.ticks[1]) - X(scale.ticks[0]) : Infinity;
    const widestTick = Math.max(0, ...tickLabels.map((tl) => textWidth(tl.text, `11px ${FONT}`)));
    const every = widestTick + 6 <= tickGapPx ? 1 : 2;
    placeAxisLabels(
      grid,
      tickLabels.filter((_, i) => (tickLabels.length - 1 - i) % every === 0),
      padTop + plotH + 17,
    );
    svg.appendChild(grid);

    if (opt.threshold && isNum(opt.threshold) && opt.threshold <= scale.max) {
      const x = Math.round(X(opt.threshold)) + 0.5;
      svg.appendChild(
        s('line', {
          class: 'threshold-line',
          x1: x,
          x2: x,
          y1: padTop,
          y2: padTop + plotH,
          'aria-hidden': 'true',
        }),
      );
    }

    rows.forEach((r, i) => {
      const yRow = padTop + i * rowH;
      const cy = yRow + rowH / 2;
      const y = cy - barH / 2;
      const g = s('g', {
        class: `bar-row${r.onClick ? ' is-clickable' : ''}${r.selected ? ' is-selected' : ''}`,
        tabindex: opt.focusable ? '0' : null,
        role: opt.focusable ? (r.onClick ? 'button' : 'img') : null,
        'aria-label': opt.focusable ? r.aria || r.label : null,
      });
      g.appendChild(s('rect', { class: 'hit', x: 0, y: yRow + 1, width, height: rowH - 2, rx: 6 }));
      g.appendChild(
        s(
          'text',
          { class: 'bar-label', x: x0 - 10, y: cy, 'text-anchor': 'end', 'dominant-baseline': 'central' },
          fitText(r.label, x0 - 14, labelFont),
        ),
      );
      if (isNum(r.value)) {
        const xv = X(r.value);
        let end = x0;
        if (!(r.value === 0 && opt.zeroNoBar)) {
          const w = Math.max(2, xv - x0);
          g.appendChild(
            s('path', {
              class: `bar${r.status ? ` bar-${r.status}` : ''}`,
              d: hbarPath(x0, y, w, barH),
              style: r.status ? null : { fill: r.color },
            }),
          );
          end = x0 + w;
        }
        if (isNum(r.p95)) {
          const xp = X(r.p95);
          g.appendChild(s('line', { class: 'whisker', x1: r2(xv), x2: r2(xp), y1: cy, y2: cy }));
          g.appendChild(s('line', { class: 'whisker', x1: r2(xp), x2: r2(xp), y1: cy - 5, y2: cy + 5 }));
          end = Math.max(end, xp);
        }
        if (isNum(r.median)) {
          const xm = r2(X(r.median));
          g.appendChild(s('line', { class: 'median-halo', x1: xm, x2: xm, y1: y - 3, y2: y + barH + 3 }));
          g.appendChild(s('line', { class: 'median-tick', x1: xm, x2: xm, y1: y - 2.5, y2: y + barH + 2.5 }));
        }
        const t = s('text', { class: 'value-label', x: r2(end + 8), y: cy, 'dominant-baseline': 'central' });
        t.appendChild(
          s('tspan', { class: 'value-num' }, r.valueText !== undefined ? r.valueText : fmtMs(r.value)),
        );
        if (r.valueUnit) t.appendChild(s('tspan', { class: 'value-unit' }, ` ${r.valueUnit}`));
        if (r.extra)
          t.appendChild(
            s('tspan', { class: `value-extra${r.extraClass ? ` ${r.extraClass}` : ''}` }, `  ${r.extra}`),
          );
        g.appendChild(t);
      } else {
        g.appendChild(
          s(
            'text',
            {
              class: `value-missing${r.missingMuted ? ' is-muted' : ''}`,
              x: x0 + 8,
              y: cy,
              'dominant-baseline': 'central',
            },
            r.missingText || '× no successful answers',
          ),
        );
      }
      if (r.tip) bindTip(g, r.tip);
      if (r.onClick) {
        g.addEventListener('click', r.onClick);
        g.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            r.onClick();
          }
        });
      }
      svg.appendChild(g);
    });
    if (opt.focusable) roving(svg, Array.from(svg.querySelectorAll('.bar-row')), ['ArrowUp'], ['ArrowDown']);
    return svg;
  }

  /**
   * Draw x-axis labels [{x, text, anchor}] (left to right) from the right: the last one
   * (it carries the unit) is always drawn, any other that would touch the label to its
   * right is skipped. Font matches the .tick CSS.
   */
  function placeAxisLabels(grid, labels, y, gap = 4) {
    const font = `11px ${FONT}`;
    let left = Infinity;
    for (let k = labels.length - 1; k >= 0; k--) {
      const lb = labels[k];
      const w = textWidth(lb.text, font);
      const a = lb.anchor || 'middle';
      const b0 = a === 'end' ? lb.x - w : a === 'start' ? lb.x : lb.x - w / 2;
      const b1 = a === 'end' ? lb.x : a === 'start' ? lb.x + w : lb.x + w / 2;
      if (k < labels.length - 1 && b1 + gap > left) continue;
      grid.appendChild(s('text', { class: 'tick', x: r2(lb.x), y, 'text-anchor': a }, lb.text));
      left = b0;
    }
  }

  /** Line chart: series [{name, color, values:[number|null]}], xs [{label, title}]. */
  function lineChart(width, series, xs, opt = {}) {
    const height = opt.height || (width < 520 ? 220 : 260);
    const L = 46;
    const R = 18;
    const T = 12;
    const B = 30;
    const plotW = Math.max(10, width - L - R);
    const plotH = height - T - B;
    const all = [];
    for (const sr of series) for (const v of sr.values) if (isNum(v)) all.push(v);
    const scale = niceTicks(all.length ? Math.max(...all) : 0, 4);
    const n = xs.length;
    const X = (i) => (n <= 1 ? L + plotW / 2 : L + (i * plotW) / (n - 1));
    const Y = (v) => T + plotH - (clamp(v, 0, scale.max) / scale.max) * plotH;
    // With onPick the plot holds a focusable control, so it must not be role="img" (which hides children).
    const svg = s('svg', {
      class: 'svg-chart',
      viewBox: `0 0 ${width} ${height}`,
      width: '100%',
      height,
      role: opt.onPick ? 'group' : 'img',
      'aria-label': opt.ariaLabel || 'Line chart',
    });

    if (isNum(opt.highlightIndex) && opt.highlightIndex >= 0 && opt.highlightIndex < n) {
      const bw = n <= 1 ? 40 : Math.max(10, Math.min(40, plotW / (n - 1)));
      const bx0 = Math.max(L - 8, X(opt.highlightIndex) - bw / 2);
      const bx1 = Math.min(width - R + 8, X(opt.highlightIndex) + bw / 2);
      svg.appendChild(
        s('rect', { class: 'highlight-band', x: r2(bx0), y: T, width: r2(bx1 - bx0), height: plotH, rx: 4 }),
      );
    }
    const grid = s('g', { class: 'grid', 'aria-hidden': 'true' });
    scale.ticks.forEach((t, i) => {
      const y = Math.round(Y(t)) + 0.5;
      grid.appendChild(
        s('line', { class: t === 0 ? 'axis-line' : 'grid-line', x1: L, x2: width - R, y1: y, y2: y }),
      );
      grid.appendChild(
        s(
          'text',
          { class: 'tick', x: L - 8, y, 'text-anchor': 'end', 'dominant-baseline': 'central' },
          fmtTick(t, scale.step) + (i === scale.ticks.length - 1 && opt.unit ? ` ${opt.unit}` : ''),
        ),
      );
    });
    // x labels: evenly thinned from the newest backwards, measured so no two overlap.
    // The newest label is always kept (end-anchored), the oldest is start-anchored.
    const tickFont = `11px ${FONT}`;
    const labelW = xs.map((x) => textWidth(x.label, tickFont));
    const anchorOf = (i) => (n > 1 && i === 0 ? 'start' : n > 1 && i === n - 1 ? 'end' : 'middle');
    const labelBox = (i) => {
      const x = X(i);
      const a = anchorOf(i);
      return a === 'end'
        ? [x - labelW[i], x]
        : a === 'start'
          ? [x, x + labelW[i]]
          : [x - labelW[i] / 2, x + labelW[i] / 2];
    };
    const fitLabels = Math.max(1, Math.floor(plotW / (Math.max(0, ...labelW) + 12)));
    const stepL = Math.max(1, Math.ceil(n / fitLabels));
    let leftEdge = Infinity;
    for (let i = n - 1; i >= 0; ) {
      const [b0, b1] = labelBox(i);
      if (i === n - 1 || (b1 + 8 <= leftEdge && b0 >= 0)) {
        grid.appendChild(
          s('text', { class: 'tick', x: r2(X(i)), y: height - 9, 'text-anchor': anchorOf(i) }, xs[i].label),
        );
        leftEdge = b0;
        i -= stepL;
      } else i -= 1; // would collide: try the next older point instead
    }
    svg.appendChild(grid);

    const marks = [];
    for (const sr of series) {
      let d = '';
      let pen = false;
      sr.values.forEach((v, i) => {
        if (isNum(v)) {
          d += `${(pen ? 'L' : 'M') + r2(X(i))},${r2(Y(v))}`;
          pen = true;
        } else pen = false;
      });
      if (d) svg.appendChild(s('path', { class: 'series-line', d, style: { stroke: sr.color } }));
    }
    for (const sr of series) {
      const col = [];
      sr.values.forEach((v, i) => {
        if (!isNum(v)) {
          col.push(null);
          return;
        }
        const c = s('circle', {
          class: 'series-dot',
          cx: r2(X(i)),
          cy: r2(Y(v)),
          r: 4,
          style: { fill: sr.color },
        });
        svg.appendChild(c);
        col.push(c);
      });
      marks.push(col);
    }
    const cross = s('line', { class: 'crosshair', x1: 0, x2: 0, y1: T, y2: T + plotH, visibility: 'hidden' });
    svg.appendChild(cross);
    // Clear of the y-axis labels (which end at L - 8) so its focus ring never crosses them.
    const overlay = s('rect', {
      class: `overlay${opt.onPick ? ' is-clickable' : ''}`,
      x: L - 6,
      y: T,
      width: plotW + 12,
      height: plotH,
      rx: 6,
      fill: 'transparent',
    });
    let active = -1;
    const setActive = (i) => {
      if (active >= 0)
        marks.forEach((col) => {
          if (col[active]) col[active].setAttribute('r', 4);
        });
      active = i;
      if (i < 0) {
        cross.setAttribute('visibility', 'hidden');
        return;
      }
      marks.forEach((col) => {
        if (col[i]) col[i].setAttribute('r', 5.5);
      });
      const x = r2(X(i));
      cross.setAttribute('x1', x);
      cross.setAttribute('x2', x);
      cross.setAttribute('visibility', 'visible');
    };
    const indexAt = (e) => {
      const rect = svg.getBoundingClientRect();
      const px = ((e.clientX - rect.left) * width) / rect.width;
      if (n <= 1) return 0;
      return clamp(Math.round(((px - L) / plotW) * (n - 1)), 0, n - 1);
    };
    const pointRows = (i) =>
      series.map((sr) => ({ sr, v: sr.values[i] })).sort((a, b) => cmpNullLast(a.v, b.v));
    overlay.addEventListener('pointermove', (e) => {
      const i = indexAt(e);
      if (i !== active) {
        setActive(i);
        const rows = pointRows(i).map(({ sr, v }) => [isNum(v) ? fmtMsU(v) : '—', sr.name, sr.color]);
        showTip(e, tipContent(xs[i].title || xs[i].label, rows));
      } else positionTip(e);
    });
    overlay.addEventListener('pointerleave', () => {
      setActive(-1);
      hideTip();
    });
    if (opt.onPick) {
      overlay.addEventListener('click', (e) => opt.onPick(indexAt(e)));
      // Keyboard: the plot is one focusable slider; arrows pick a point, Enter opens it.
      const describe = (i) =>
        `${xs[i].title || xs[i].label}: ${pointRows(i)
          .map(({ sr, v }) => `${sr.name} ${isNum(v) ? fmtMsU(v) : 'no data'}`)
          .join(', ')}`;
      const keyPick = (i) => {
        setActive(i);
        overlay.setAttribute('aria-valuenow', String(i + 1));
        overlay.setAttribute('aria-valuetext', describe(i));
        const rows = pointRows(i).map(({ sr, v }) => [isNum(v) ? fmtMsU(v) : '—', sr.name, sr.color]);
        // Anchor the tooltip to the crosshair so it sits on the chosen point.
        showTip({ type: 'focus', currentTarget: cross }, tipContent(xs[i].title || xs[i].label, rows));
      };
      setAttrs(overlay, {
        tabindex: '0',
        role: 'slider',
        'aria-valuemin': '1',
        'aria-valuemax': String(Math.max(1, n)),
        'aria-valuenow': String(Math.max(1, n)),
        'aria-label':
          opt.pickLabel || 'Choose a point with the Left and Right arrow keys, press Enter to open it',
      });
      overlay.addEventListener('focus', () => {
        if (n && isFocusVisible(overlay)) keyPick(active >= 0 ? active : n - 1);
      });
      overlay.addEventListener('blur', () => {
        setActive(-1);
        hideTip();
      });
      overlay.addEventListener('keydown', (e) => {
        if (!n) return;
        const cur = active >= 0 ? active : n - 1;
        let next = null;
        if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') next = Math.max(0, cur - 1);
        else if (e.key === 'ArrowRight' || e.key === 'ArrowUp') next = Math.min(n - 1, cur + 1);
        else if (e.key === 'Home') next = 0;
        else if (e.key === 'End') next = n - 1;
        else if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          opt.onPick(cur);
          return;
        }
        if (next === null) return;
        e.preventDefault();
        keyPick(next);
      });
    }
    svg.appendChild(overlay);
    return svg;
  }

  /** Histogram of values into 10 bins (last bin collects the long tail). */
  function histogram(width, values, opt = {}) {
    const vals = values.filter(isNum).sort((a, b) => a - b);
    if (!vals.length)
      return h('div', { class: 'chart-placeholder' }, opt.emptyText || 'No successful answers to plot.');
    const height = opt.height || 220;
    const L = 42;
    const R = 14;
    const T = 10;
    const B = 34;
    const plotW = Math.max(40, width - L - R);
    const plotH = height - T - B;
    const min = vals[0];
    const max = vals[vals.length - 1];
    const hiRef = nearestRank(vals, 98);
    const span = Math.max(hiRef - min, Math.max(1, min * 0.1));
    const bw = niceCeil(span / 10);
    const lo = Math.floor(min / bw) * bw;
    const bins = new Array(10).fill(0);
    for (const v of vals) bins[clamp(Math.floor((v - lo) / bw), 0, 9)] += 1;
    const overflow = max >= lo + 10 * bw;
    const scale = niceTicks(Math.max(...bins), 4, 0, true);
    const Y = (c) => T + plotH - (c / scale.max) * plotH;
    const slot = plotW / 10;
    const svg = s('svg', {
      class: 'svg-chart',
      viewBox: `0 0 ${width} ${height}`,
      width: '100%',
      height,
      role: 'group',
      'aria-label': opt.ariaLabel || 'Latency histogram',
    });
    const grid = s('g', { class: 'grid', 'aria-hidden': 'true' });
    for (const t of scale.ticks) {
      const y = Math.round(Y(t)) + 0.5;
      grid.appendChild(
        s('line', { class: t === 0 ? 'axis-line' : 'grid-line', x1: L, x2: width - R, y1: y, y2: y }),
      );
      grid.appendChild(
        s(
          'text',
          { class: 'tick', x: L - 8, y, 'text-anchor': 'end', 'dominant-baseline': 'central' },
          fmtTick(t, scale.step),
        ),
      );
    }
    // Bin-edge labels. Thin them by measured width (3- and 4-digit edges need more room
    // than 2-digit ones), then place right to left so the unit-bearing last label always
    // stays and any label that would touch its right-hand neighbour is dropped.
    const tickFont = `11px ${FONT}`;
    const edgeText = (i) => fmtTick(lo + i * bw, bw);
    const widest = Math.max(
      ...Array.from({ length: 11 }, (_, i) =>
        textWidth((i === 9 && overflow ? '≥' : '') + edgeText(i), tickFont),
      ),
    );
    const labelEvery = [1, 2, 5].find((k) => widest + 8 <= slot * k) || 10;
    const xLabels = [];
    for (let i = 0; i <= 10; i += labelEvery) {
      if (i === 10 && overflow) continue; // the tail bin has no upper edge
      let text = edgeText(i);
      if (i === 10) text += ' ms';
      if (i === 9 && overflow) text = `≥${text}`;
      xLabels.push({ text, x: L + i * slot, anchor: i === 10 ? 'end' : 'middle' });
    }
    if (overflow) xLabels.push({ text: 'ms', x: width - R, anchor: 'end' });
    placeAxisLabels(grid, xLabels, height - 14);
    svg.appendChild(grid);
    const unitLabel = opt.countLabel || 'queries';
    bins.forEach((c, i) => {
      const x = L + i * slot + 1;
      const w = Math.max(1, slot - 2);
      const g = s('g', { class: 'bar-col', role: 'img', 'aria-label': '' });
      g.appendChild(s('rect', { class: 'hit', x: r2(L + i * slot), y: T, width: r2(slot), height: plotH }));
      if (c > 0) {
        const y = Y(c);
        g.appendChild(
          s('path', {
            class: 'bar',
            d: vbarPath(x, y, w, T + plotH - y),
            style: { fill: opt.color || 'var(--s1)' },
          }),
        );
      }
      const a = lo + i * bw;
      const range =
        i === 9 && overflow ? `≥ ${fmtTick(a, bw)} ms` : `${fmtTick(a, bw)}–${fmtTick(a + bw, bw)} ms`;
      g.setAttribute('aria-label', `${range}: ${c} ${unitLabel}`);
      bindTip(g, () =>
        tipContent(range, [
          [fmtInt(c), unitLabel],
          [fmtPct(c / vals.length), 'of all'],
        ]),
      );
      svg.appendChild(g);
    });
    roving(svg, Array.from(svg.querySelectorAll('.bar-col')), ['ArrowLeft'], ['ArrowRight']);
    return svg;
  }

  // ---------------------------------------------------------------- heat scale
  function heatScale(values) {
    const v = values.filter((x) => isNum(x) && x > 0).sort((a, b) => a - b);
    if (!v.length) return null;
    const lo = Math.max(0.1, nearestRank(v, 5));
    let hi = nearestRank(v, 95);
    if (hi < lo * 1.5) hi = lo * 1.5;
    const llo = Math.log(lo);
    const lhi = Math.log(hi);
    return {
      lo,
      hi,
      mid: Math.exp((llo + lhi) / 2),
      t: (x) => clamp((Math.log(Math.max(x, 0.01)) - llo) / (lhi - llo), 0, 1),
    };
  }
  function heatBg(t) {
    if (t <= 0.5) return `color-mix(in oklab, var(--heat-mid) ${Math.round(t * 200)}%, var(--heat-lo))`;
    return `color-mix(in oklab, var(--heat-hi) ${Math.round((t - 0.5) * 200)}%, var(--heat-mid))`;
  }

  // ================================================================ generic pieces
  function card(opts, ...body) {
    const head =
      opts.title || opts.right
        ? h(
            'div',
            { class: 'card-head' },
            h(
              'div',
              { class: 'card-titles' },
              opts.title ? h(opts.level || 'h2', { class: 'card-title' }, opts.title) : null,
              opts.sub ? h('p', { class: 'card-sub' }, opts.sub) : null,
            ),
            opts.right ? h('div', { class: 'card-right' }, opts.right) : null,
          )
        : null;
    return h('section', { class: `card${opts.cls ? ` ${opts.cls}` : ''}` }, head, ...body);
  }

  function kpi(label, value, unit, sub, cls) {
    return h(
      'div',
      { class: `kpi${cls ? ` ${cls}` : ''}` },
      h('div', { class: 'kpi-label' }, label),
      h('div', { class: 'kpi-value' }, value, unit ? h('span', { class: 'kpi-unit' }, ` ${unit}`) : null),
      sub ? h('div', { class: 'kpi-sub' }, sub) : null,
    );
  }
  function msKpi(label, v, sub) {
    return kpi(label, isNum(v) ? fmtMs(v) : '—', isNum(v) ? 'ms' : '', sub);
  }

  /** A 95 % interval [lo, hi] as "95% CI lo–hi ms"; an unbounded side is "?". */
  function ciText(ci) {
    if (!Array.isArray(ci) || ci.length !== 2) return null;
    const side = (v) => (isNum(v) ? fmtMs(v) : '?');
    return `95% CI ${side(ci[0])}–${side(ci[1])} ms`;
  }

  function failBadge(st) {
    if (!st || !isNum(st.failure_rate)) return '—';
    // every query failed on this computer: there is no failure rate to show
    if (st.n > 0 && st.n === st.local_errors) return '—';
    if (st.failure_rate === 0) return h('span', { class: 'muted' }, '0%');
    if (st.failure_rate > 0.02)
      return h('span', { class: 'status status-critical' }, icon('alert', 13), fmtPct(st.failure_rate));
    return h('span', { class: 'status status-warn' }, fmtPct(st.failure_rate));
  }

  function emptyPanel(title, text, action) {
    return h(
      'section',
      { class: 'card empty' },
      h('h2', { class: 'empty-title' }, title),
      text ? h('p', { class: 'empty-text' }, text) : null,
      action || null,
    );
  }

  function loadingPanel(text) {
    return h(
      'div',
      { class: 'loading-panel', role: 'status' },
      h('span', { class: 'spinner' }),
      h('span', null, text || 'Loading…'),
    );
  }

  function copyButton(text, label, withText = true, caption = 'Copy') {
    const txt = h('span', { class: 'copy-text' }, caption);
    const btn = h(
      'button',
      { type: 'button', class: 'btn btn-sm copy-btn', 'aria-label': label, title: label },
      icon('copy', 14),
      withText ? txt : null,
    );
    btn.addEventListener('click', async () => {
      const ok = await copyText(text);
      btn.classList.toggle('is-copied', ok);
      setKids(
        btn,
        icon(ok ? 'check' : 'x', 14),
        withText ? h('span', { class: 'copy-text' }, ok ? 'Copied' : 'Copy failed') : null,
      );
      setTimeout(() => {
        btn.classList.remove('is-copied');
        setKids(btn, icon('copy', 14), withText ? h('span', { class: 'copy-text' }, caption) : null);
      }, 1600);
    });
    return btn;
  }
  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (_) {
      /* fall through */
    }
    try {
      const ta = h('textarea', { class: 'offscreen', 'aria-hidden': 'true' });
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      return ok;
    } catch (_) {
      return false;
    }
  }

  /**
   * Sortable table. columns: {key, label, num?, head?, sortable?, get(row), render?(row), defaultDir?}
   * Sort state persists per table id in state.sorts. With opts.limit, only the first `limit` rows
   * (after sorting, so sorting still covers them all) are drawn, and opts.more(shown, total) adds a
   * line under the table saying so.
   */
  function dataTable(id, columns, rows, opts = {}) {
    const wrap = h('div', { class: `table-wrap${opts.wrapClass ? ` ${opts.wrapClass}` : ''}` });
    const draw = () => {
      const sort = state.sorts[id] || opts.defaultSort || null;
      let sorted = rows.slice();
      if (sort) {
        const col = columns.find((c) => c.key === sort.key);
        if (col) sorted.sort((a, b) => cmpNullLast(col.get(a), col.get(b), sort.dir));
      }
      const total = sorted.length;
      if (opts.limit && total > opts.limit) sorted = sorted.slice(0, opts.limit);
      const thead = h(
        'thead',
        null,
        h(
          'tr',
          null,
          columns.map((c) => {
            const active = sort && sort.key === c.key;
            const sortable = c.sortable !== false && c.get;
            return h(
              'th',
              {
                scope: 'col',
                class: (c.num ? 'num' : '') + (c.cls ? ` ${c.cls}` : ''),
                'aria-sort': sortable
                  ? active
                    ? sort.dir > 0
                      ? 'ascending'
                      : 'descending'
                    : 'none'
                  : null,
              },
              sortable
                ? h(
                    'button',
                    {
                      type: 'button',
                      class: `th-sort${active ? ' is-active' : ''}`,
                      dataset: { sortKey: c.key },
                      onClick: () => {
                        state.sorts[id] = { key: c.key, dir: active ? -sort.dir : c.defaultDir || 1 };
                        draw();
                        // The redraw replaced the button; keep the keyboard where it was.
                        wrap.querySelector(`.th-sort[data-sort-key="${CSS.escape(c.key)}"]`)?.focus();
                      },
                    },
                    c.label,
                    h(
                      'span',
                      { class: 'sort-ind', 'aria-hidden': 'true' },
                      active ? (sort.dir > 0 ? '▲' : '▼') : '↕',
                    ),
                  )
                : c.label,
            );
          }),
        ),
      );
      const tbody = h(
        'tbody',
        null,
        sorted.map((r) => {
          const attrs = opts.rowAttrs ? opts.rowAttrs(r) : null;
          return h(
            'tr',
            attrs,
            columns.map((c) =>
              h(
                c.head ? 'th' : 'td',
                {
                  scope: c.head ? 'row' : null,
                  class: (c.num ? 'num' : '') + (c.cls ? ` ${c.cls}` : ''),
                },
                c.render
                  ? c.render(r)
                  : c.num
                    ? fmtMs(c.get(r))
                    : String(c.get(r) === null || c.get(r) === undefined ? '—' : c.get(r)),
              ),
            ),
          );
        }),
      );
      if (!sorted.length && opts.emptyText) {
        tbody.appendChild(
          h('tr', null, h('td', { colspan: columns.length, class: 'table-empty' }, opts.emptyText)),
        );
      }
      setKids(
        wrap,
        h(
          'table',
          { class: `data-table${opts.tableClass ? ` ${opts.tableClass}` : ''}` },
          opts.caption ? h('caption', { class: 'sr-only' }, opts.caption) : null,
          thead,
          tbody,
        ),
        sorted.length < total && opts.more ? opts.more(sorted.length, total) : null,
      );
    };
    draw();
    return wrap;
  }

  // ================================================================ dataset helpers
  function resolverNames(sum) {
    if (sum && Array.isArray(sum.resolvers) && sum.resolvers.length) return sum.resolvers.map(String);
    return Object.keys(sum?.by_resolver || {});
  }
  function domainNames(sum) {
    if (sum && Array.isArray(sum.domains) && sum.domains.length) return sum.domains.map(String);
    return Object.keys(sum?.by_domain || {});
  }
  function rankMap(rec) {
    const m = new Map();
    for (const r of rec && Array.isArray(rec.ranking) ? rec.ranking : []) m.set(r.resolver, r);
    return m;
  }
  function dsSettings(ds) {
    const defaults = Object.fromEntries(settingFields().map((f) => [f.key, f.default]));
    return { ...defaults, ...(ds?.config?.settings || state.config?.settings || {}) };
  }
  /** Slow answers of one resolver, slowest first: {rows, total, cap}. */
  function slowQueriesFor(ds, sel, thr) {
    const sum = ds.summary || {};
    const byDesc = (a, b) => b.ms - a.ms;
    if (Array.isArray(ds.results)) {
      // A single run has every row: never rely on the summary's capped list.
      const all = ds.results
        .filter((r) => r && r.resolver === sel && r.status === 'ok' && isNum(r.ms) && r.ms > thr)
        .sort(byDesc);
      return { rows: all.slice(0, SLOW_TABLE_MAX), total: all.length, cap: SLOW_TABLE_MAX };
    }
    const per = sum.slow_by_resolver || {};
    const rows = (Array.isArray(per[sel]) ? per[sel] : []).filter((r) => r && isNum(r.ms)).sort(byDesc);
    const count = sum.slow_count_by_resolver?.[sel];
    return { rows, total: isNum(count) ? Math.max(count, rows.length) : rows.length, cap: rows.length };
  }
  /** " · measured in 1 of 5 runs" for a resolver missing from some combined runs. */
  function coverageNote(ds, name) {
    const c = ds && ds.kind === 'aggregate' && ds.coverage ? ds.coverage[name] : null;
    if (!c || !isNum(c.runs) || !isNum(c.of) || c.runs >= c.of) return null;
    return h(
      'span',
      null,
      ' · ',
      h(
        'span',
        { class: 'status status-warn' },
        `measured in ${fmtInt(c.runs)} of ${plural(c.of, 'combined run')}`,
      ),
    );
  }
  function runRow(id) {
    return state.runs.find((r) => r.id === id) || null;
  }
  function runsStamp() {
    return state.runs.map((r) => r.id).join(',');
  }
  /** "All runs combined" only recommends resolvers enabled now, so it also depends on the config. */
  function aggStamp() {
    const res = state.config && Array.isArray(state.config.resolvers) ? state.config.resolvers : [];
    return (
      runsStamp() +
      '|' +
      res
        .filter((r) => r && r.enabled !== false)
        .map((r) => String(r?.name))
        .join('\n')
    );
  }

  function datasetLabel(ds) {
    if (!ds) return '';
    if (ds.kind === 'aggregate') return `All ${plural(ds.runIds.length, 'run')} combined`;
    const when = fmtDate(ds.run?.started_at);
    return (
      (ds.key === 'latest' || (state.runs[0] && state.runs[0].id === ds.id) ? 'Latest run · ' : 'Run of ') +
      when
    );
  }

  // ================================================================ estimate
  // The server estimates every run (config.estimate); the page only picks which one to show.
  /** The rounds the header's input asks for, within the schema's bounds. */
  function roundsValue() {
    const cfgRounds = state.config?.settings?.rounds || 1;
    const v = parseInt(els.rounds.value, 10);
    if (!Number.isFinite(v)) return cfgRounds;
    const b = settingSchema('rounds');
    return b ? clamp(v, b.min, b.max) : Math.max(1, v);
  }
  let runEstimateSeq = 0;
  const fetchRunEstimate = debounce(async (rounds) => {
    const seq = ++runEstimateSeq;
    try {
      const est = await api('/api/estimate', { method: 'POST', body: { rounds } });
      if (seq !== runEstimateSeq) return;
      state.runEstimate = est;
      scheduleRender({ controls: true });
    } catch (err) {
      console.warn('estimate unavailable:', err); // the header keeps the last one; a run reports real errors
    }
  }, 200);
  /** The estimate for the header's rounds: the saved config's, or a fetched one (the last until it arrives). */
  function runEstimate() {
    const est = state.estimate;
    const rounds = roundsValue();
    if (!est || est.rounds === rounds) return est;
    if (state.runEstimate?.rounds === rounds) return state.runEstimate;
    fetchRunEstimate(rounds);
    return state.runEstimate || est;
  }

  // ================================================================ run controls & progress
  function updateRunControls() {
    const running = !!state.job.running;
    const cfg = state.config;
    if (cfg && !state.roundsTouched) els.rounds.value = String(cfg.settings?.rounds || 1);
    els.runBtn.disabled = running || !cfg;
    setKids(
      els.runBtn,
      running ? h('span', { class: 'spinner spinner-sm', 'aria-hidden': 'true' }) : icon('play', 14),
      h('span', null, running ? 'Running…' : 'Run benchmark'),
    );
    els.rounds.disabled = running;
    if (cfg && state.configErrors.length) {
      // Don't advertise a duration or load for a config the server will refuse to run.
      setKids(
        els.estimate,
        h(
          'a',
          { class: 'est-problem', href: '#settings' },
          icon('alert', 13),
          `The saved configuration has ${plural(state.configErrors.length, 'problem')}. Fix it in Settings.`,
        ),
      );
      els.estimate.title = '';
    } else if (cfg && runEstimate()) {
      const est = runEstimate();
      setKids(
        els.estimate,
        h('span', { class: 'est-main' }, icon('clock', 13), `≈ ${fmtDuration(est.est_seconds)}`),
        h('span', { class: 'est-sep', 'aria-hidden': 'true' }, '·'),
        h('span', null, `${fmtInt(est.queries)} queries`),
        h('span', { class: 'est-sep', 'aria-hidden': 'true' }, '·'),
        h('span', null, `≤ ${fmtQps(est.max_qps_per_server)} q/s per server`),
      );
      els.estimate.title =
        `${plural(est.resolvers, 'resolver')} (${plural(est.servers, 'server')}) × ${plural(est.domains, 'domain')} × ${plural(est.rounds, 'round')}. ` +
        `At most ${fmtQps(est.max_qps_per_server)} queries per second to any one server, ${fmtQps(est.max_qps_total)} in total.`;
    } else {
      setKids(els.estimate);
    }
    for (const b of document.querySelectorAll('[data-run-button]')) b.disabled = running || !cfg;
  }

  async function startRun() {
    if (state.job.running || !state.config) return;
    if (
      state.dirty &&
      !window.confirm(
        'Benchmarks use the saved settings. Your unsaved changes in Settings will not be used. Run anyway?',
      )
    )
      return;
    const rounds = roundsValue();
    els.runBtn.disabled = true;
    try {
      const res = await api('/api/run', { method: 'POST', body: { rounds } });
      state.job = { running: true, done: 0, total: res?.total || 0, elapsed_s: 0, eta_s: null };
      state.cancelling = false;
      resetLive();
      scheduleRender({ progress: true });
      schedulePoll(POLL_MS);
    } catch (e) {
      if (e.status === 409) {
        toast('A benchmark is already running — showing its progress.', 'info');
        state.job = { running: true, done: 0, total: 0 };
        resetLive();
        scheduleRender({ progress: true });
        schedulePoll(0);
      } else if (e.status === 400 && e.details && e.details.length && /config/i.test(e.message)) {
        showBanner(
          `Could not start the benchmark: ${e.message}. Fix the configuration in Settings and save it.`,
          'error',
          e.details,
          { href: '#settings', label: 'Open Settings' },
        );
      } else showBanner(`Could not start the benchmark: ${e.message}`, 'error', e.details);
    } finally {
      scheduleRender({ controls: true });
    }
  }

  async function cancelRun() {
    if (!state.job.running || state.cancelling) return;
    state.cancelling = true;
    scheduleRender({ progress: true });
    try {
      await api('/api/run/cancel', { method: 'POST', body: {} });
    } catch (e) {
      if (e.status !== 409) showBanner(`Could not cancel: ${e.message}`);
      state.cancelling = false;
      scheduleRender({ progress: true });
    }
  }

  function resetLive() {
    state.live = { seen: new Set(), slow: 0, failed: 0, last: null };
  }

  function ingestRecent(rows) {
    if (!Array.isArray(rows)) return;
    const thr = settingOf(state.config?.settings, 'slow_threshold_ms');
    for (const r of rows) {
      if (!r) continue;
      const key = `${r.resolver}|${r.server}|${r.domain}|${r.round}`;
      if (state.live.seen.has(key)) continue;
      state.live.seen.add(key);
      if (r.status !== 'ok') state.live.failed += 1;
      else if (isNum(r.ms) && r.ms > thr) state.live.slow += 1;
    }
    if (rows.length) state.live.last = rows[rows.length - 1];
  }

  function schedulePoll(delay = POLL_MS) {
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(pollStatus, delay);
  }

  async function pollStatus() {
    let st;
    try {
      st = await api('/api/status');
    } catch (e) {
      // Keep trying, more slowly each time, and pick up where the job is once the server answers
      // again (FE-7). The run itself goes on, and is saved, whatever happens to this page.
      state.pollFailures += 1;
      if (state.pollFailures === POLL_WARN_AFTER)
        showBanner(
          `Lost contact with the server while a benchmark was running (${e.message}) Still trying…`,
          'info',
        );
      schedulePoll(Math.min(POLL_BACKOFF_MAX_MS, 1000 * 2 ** (state.pollFailures - 1)));
      return;
    }
    if (state.pollFailures >= POLL_WARN_AFTER) {
      clearBanners('Lost contact with the server');
      toast('Back in touch with the server.');
    }
    state.pollFailures = 0;
    const wasRunning = !!state.job.running;
    ingestRecent(st?.recent);
    state.job = { ...(st || {}), running: !!st?.running };
    scheduleRender({ progress: true });
    if (state.job.running) {
      schedulePoll(POLL_MS);
    } else {
      state.cancelling = false;
      scheduleRender({ controls: true });
      if (wasRunning) await onJobFinished(st || {});
    }
  }

  async function onJobFinished(st) {
    if (st.error) showBanner(`The benchmark stopped with an error: ${st.error}`);
    await loadRuns();
    // After an error, last_run_id still names the previous run: don't announce that one.
    const row = !st.error && st.last_run_id ? runRow(st.last_run_id) : null;
    if (row) {
      toast(
        row.status === 'cancelled'
          ? `Benchmark cancelled — ${fmtInt(row.n_queries)} partial results saved.`
          : `Benchmark complete — ${fmtInt(row.n_queries)} queries in ${fmtDuration(row.duration_s)}.`,
      );
      await selectDataset(state.runs[0] && state.runs[0].id === row.id ? 'latest' : row.id, {
        rerender: 'auto',
      });
    } else if (state.route.tab === 'settings') {
      scheduleRender({ tabs: true }); // never rebuild the Settings form under the user
    } else {
      scheduleRender();
    }
  }

  // ---------------------------------------------------------------- runs saved elsewhere
  // Runs made with the CLI or from another tab land in runs/ without this page
  // knowing. Re-read the (cheap, mtime-cached) run list when the page regains focus,
  // on tab changes and every so often while visible; redraw only if it changed.
  let runsRefreshing = false;
  let runsRefreshedAt = 0;
  async function refreshRuns(force) {
    if (!state.bootstrapped || state.job.running || runsRefreshing) return;
    if (!force && Date.now() - runsRefreshedAt < 2000) return;
    runsRefreshing = true;
    runsRefreshedAt = Date.now();
    try {
      const before = runsStamp();
      const newestBefore = state.runs[0] ? state.runs[0].id : null;
      let res;
      try {
        res = await api('/api/runs');
      } catch (_) {
        return; // background refresh: fail quietly, the next one will retry
      }
      if (state.job.running || !res || !Array.isArray(res.runs)) return; // a job's own finish handles it
      state.runs = res.runs;
      if (runsStamp() === before) return;
      const key = state.datasetKey;
      const newest = state.runs[0] ? state.runs[0].id : null;
      const datasetStale =
        !before ||
        key === 'all' ||
        (key === 'latest' && newest !== newestBefore) ||
        (key !== 'latest' && key !== 'all' && !runRow(key));
      if (datasetStale) await selectDataset(key, { rerender: 'auto' });
      else scheduleRender(state.route.tab === 'settings' ? { tabs: true } : { view: true });
    } finally {
      runsRefreshing = false;
    }
  }

  function buildProgress() {
    const p = {};
    p.title = h('span', { class: 'progress-title' }, 'Benchmark running');
    p.count = h('span', { class: 'progress-count' });
    p.fill = h('div', { class: 'progress-fill' });
    p.track = h(
      'div',
      {
        class: 'progress-track',
        role: 'progressbar',
        'aria-valuemin': '0',
        'aria-valuemax': '100',
        'aria-label': 'Benchmark progress',
      },
      p.fill,
    );
    p.elapsed = h('span', { class: 'pstat-val' });
    p.eta = h('span', { class: 'pstat-val' });
    p.slow = h('span', { class: 'pstat-val' });
    p.failed = h('span', { class: 'pstat-val' });
    p.last = h('div', { class: 'progress-last' });
    p.cancel = h(
      'button',
      { type: 'button', class: 'btn btn-sm', onClick: cancelRun },
      icon('stop', 12),
      h('span', null, 'Cancel'),
    );
    const stat = (label, val) =>
      h('span', { class: 'pstat' }, h('span', { class: 'pstat-label' }, label), val);
    const node = h(
      'div',
      { class: 'progress-inner' },
      h(
        'div',
        { class: 'progress-head' },
        h(
          'div',
          { class: 'progress-headline' },
          h('span', { class: 'spinner', 'aria-hidden': 'true' }),
          p.title,
          p.count,
        ),
        p.cancel,
      ),
      p.track,
      h(
        'div',
        { class: 'progress-stats' },
        stat('Elapsed', p.elapsed),
        stat('Remaining', p.eta),
        stat('Slow', p.slow),
        stat('Failed', p.failed),
      ),
      p.last,
    );
    setKids(els.progress, node);
    els.prog = p;
  }

  function renderProgress() {
    const j = state.job;
    if (!j.running) {
      els.progress.hidden = true;
      return;
    }
    if (!els.prog || !els.progress.firstChild) buildProgress();
    els.progress.hidden = false;
    const p = els.prog;
    const total = isNum(j.total) ? j.total : 0;
    const done = isNum(j.done) ? j.done : 0;
    const pct = total ? clamp((done / total) * 100, 0, 100) : 0;
    p.title.textContent = state.cancelling ? 'Cancelling…' : 'Benchmark running';
    p.count.textContent = total
      ? `${fmtInt(done)} of ${fmtInt(total)} queries · ${Math.floor(pct)}%`
      : 'Starting…';
    p.fill.style.width = `${pct.toFixed(1)}%`;
    p.track.setAttribute('aria-valuenow', String(Math.floor(pct)));
    p.elapsed.textContent = fmtDuration(j.elapsed_s || 0);
    p.eta.textContent = isNum(j.eta_s) ? `≈ ${fmtDuration(j.eta_s)}` : '—';
    // The server counts every result; `recent` only carries the last 20 rows, so
    // the client-side tally is just a fallback until the first status poll.
    const slowN = isNum(j.slow) ? j.slow : state.live.slow;
    const failedN = isNum(j.failed) ? j.failed : state.live.failed;
    p.slow.textContent = fmtInt(slowN);
    p.failed.textContent = fmtInt(failedN);
    p.failed.classList.toggle('is-bad', failedN > 0);
    p.cancel.disabled = state.cancelling;
    const last = state.live.last;
    if (last) {
      setKids(
        p.last,
        h('span', { class: 'muted' }, 'Latest '),
        dot(last.resolver),
        h('span', null, `${last.resolver} `),
        h('code', null, String(last.server)),
        h('span', null, ` ${last.domain} → `),
        h(
          'strong',
          { class: last.status === 'ok' ? '' : 'text-critical' },
          last.status === 'ok' ? fmtMsU(last.ms) : String(last.rcode || last.status),
        ),
      );
    } else setKids(p.last);
  }

  // ================================================================ dataset loading
  /**
   * Load a dataset and redraw. opts.rerender === 'auto' is for background reloads
   * (a run finished, new runs appeared): if the Settings tab is open when the data
   * arrives, only the tabs and dataset bar are refreshed, so a form being edited is
   * never rebuilt under the user. The data views pick the new dataset up on the
   * next navigation.
   */
  async function selectDataset(key, opts = {}) {
    const background = opts.rerender === 'auto';
    const finish = () =>
      scheduleRender(background && state.route.tab === 'settings' ? { tabs: true } : { view: true });
    if (key !== 'latest' && key !== 'all' && !runRow(key)) key = 'latest';
    state.datasetKey = key;
    state.datasetError = null;
    if (!state.runs.length) {
      state.dataset = null;
      finish();
      return;
    }
    const token = ++state.dsToken;
    if (!(background && state.route.tab === 'settings')) els.main.classList.add('is-refreshing');
    try {
      let ds;
      if (key === 'all') {
        const stamp = aggStamp();
        let agg = state.aggCache && state.aggCache.stamp === stamp ? state.aggCache.data : null;
        if (!agg) {
          agg = await api('/api/aggregate?runs=all');
          state.aggCache = { stamp, data: agg };
        }
        const newest = state.runCache.get(state.runs[0].id);
        ds = {
          kind: 'aggregate',
          key,
          id: null,
          run: null,
          summary: agg?.summary || {},
          recommendation: agg?.recommendation || {},
          runIds: agg && Array.isArray(agg.run_ids) ? agg.run_ids : state.runs.map((r) => r.id),
          config: agg?.config || newest?.config || state.config,
          results: null,
          // {name: {runs, of, last_run}}: how many of the combined runs measured each resolver.
          coverage:
            agg?.coverage && typeof agg.coverage === 'object' && !Array.isArray(agg.coverage)
              ? agg.coverage
              : null,
        };
      } else {
        const id = key === 'latest' ? state.runs[0].id : key;
        const run = await loadRun(id);
        ds = {
          kind: 'run',
          key,
          id,
          run,
          summary: run.summary || {},
          recommendation: run.recommendation || {},
          runIds: [id],
          config: run.config || state.config,
          results: Array.isArray(run.results) ? run.results : null,
        };
      }
      if (token !== state.dsToken) return;
      state.dataset = ds;
    } catch (e) {
      if (token !== state.dsToken) return;
      state.datasetError = e.message;
      showBanner(`Could not load results: ${e.message}`);
    } finally {
      if (token === state.dsToken) {
        els.main.classList.remove('is-refreshing');
        finish();
      }
    }
  }

  function viewRun(id) {
    selectDataset(id);
    go('overview');
  }

  // ================================================================ trend
  // The /api/runs rows carry each run's per-resolver medians, which is all the trend needs.
  function trendData() {
    return state.runs
      .slice(0, TREND_MAX_RUNS)
      .reverse()
      .map((r) => ({
        id: r.id,
        started_at: r.started_at,
        status: r.status,
        med: r.medians && typeof r.medians === 'object' ? r.medians : {},
        names: Array.isArray(r.resolvers) ? r.resolvers.map(String) : [],
      }));
  }

  function trendCard(ds) {
    if (state.runs.length < 2) return null;
    const slot = h('div', { class: 'trend-slot' });
    viewHooks.trendSlot = slot;
    fillTrend(slot, ds);
    const n = Math.min(state.runs.length, TREND_MAX_RUNS);
    return card(
      {
        title: 'Median latency over time',
        sub: `One point per run for the last ${plural(n, 'run')}. Click the chart (or use the arrow keys and Enter) to open that run.`,
      },
      slot,
    );
  }

  function fillTrend(slot) {
    const runs = trendData();
    const nameSet = new Set();
    for (const r of runs) for (const n of r.names) nameSet.add(n);
    const names = Array.from(nameSet).sort((a, b) => colorIndex(a) - colorIndex(b));
    const series = names.map((n) => ({
      name: n,
      color: colorOf(n),
      values: runs.map((r) => (r.med && isNum(r.med[n]) ? r.med[n] : null)),
    }));
    const xs = runs.map((r) => ({
      label: fmtDate(r.started_at, 'short'),
      title: fmtDate(r.started_at) + (r.status && r.status !== 'complete' ? ` (${r.status})` : ''),
    }));
    const ds = state.dataset;
    const hi = ds && ds.kind === 'run' ? runs.findIndex((r) => r.id === ds.id) : -1;
    setKids(
      slot,
      legend(series.map((sr) => ({ kind: 'line', color: sr.color, label: sr.name }))),
      chartBox((w) =>
        lineChart(w, series, xs, {
          unit: 'ms',
          highlightIndex: hi,
          onPick: (i) => viewRun(runs[i].id),
          ariaLabel: 'Median latency per resolver over time',
          pickLabel: 'Run history: use the Left and Right arrow keys to choose a run, Enter to open it',
        }),
      ),
    );
  }

  // ================================================================ views: overview
  function recommendationCard(ds) {
    const rec = ds.recommendation || {};
    const ranking = Array.isArray(rec.ranking) ? rec.ranking : [];
    const eyebrow = h(
      'div',
      { class: 'eyebrow' },
      h('span', { class: 'eyebrow-strong' }, 'Recommendation'),
      h('span', { class: 'eyebrow-sep', 'aria-hidden': 'true' }, '·'),
      h('span', null, datasetLabel(ds)),
    );
    if (!rec.best) {
      return h(
        'section',
        { class: 'card rec-card rec-none' },
        eyebrow,
        h('h2', { class: 'rec-headline' }, icon('alert', 22), 'No recommendation'),
        h(
          'p',
          { class: 'rec-summary' },
          rec.summary ||
            'None of the resolvers returned a successful answer, so there is nothing to compare. Check your network connection and try again.',
        ),
        notesList(rec.notes),
      );
    }
    const best = ranking.find((r) => r.resolver === rec.best) || {};
    const owner = (ip) => {
      const bs = ds.summary?.by_server || {};
      // Suggested IPs come from the best and backup resolvers; ask them first, since in
      // combined runs a renamed resolver can list the same IP under its old name.
      for (const name of [rec.best, rec.backup])
        if (name && bs[name] && Object.hasOwn(bs[name], ip)) return name;
      for (const r of ranking) if (r.fastest_server === ip) return r.resolver;
      for (const name of Object.keys(bs)) if (bs[name] && Object.hasOwn(bs[name], ip)) return name;
      return null;
    };
    const roles = ['Primary DNS', 'Secondary DNS', 'Third DNS', 'Fourth DNS'];
    const servers = (Array.isArray(rec.suggested_servers) ? rec.suggested_servers : [])
      .filter(Boolean)
      .map(String);
    const serverBoxes = servers.map((ip, i) => {
      const o = owner(ip);
      return h(
        'div',
        { class: 'server-box' },
        h('div', { class: 'server-role' }, roles[i] || `DNS ${i + 1}`),
        h(
          'div',
          { class: 'server-ip-row' },
          h('code', { class: 'server-ip' }, ip),
          copyButton(ip, `Copy ${ip}`, false),
        ),
        o ? h('div', { class: 'server-owner' }, dot(o), h('span', null, o)) : null,
      );
    });
    const tied = (Array.isArray(rec.tied_with) ? rec.tied_with : []).filter(Boolean);
    return h(
      'section',
      { class: 'card rec-card', 'aria-labelledby': 'rec-headline' },
      h(
        'div',
        { class: 'rec-grid' },
        h(
          'div',
          { class: 'rec-main' },
          eyebrow,
          h(
            'h2',
            { class: 'rec-headline', id: 'rec-headline' },
            dot(rec.best, 'dot-lg'),
            h('span', null, rec.best),
          ),
          rec.summary ? h('p', { class: 'rec-summary' }, rec.summary) : null,
          servers.length
            ? h(
                'div',
                { class: 'server-boxes' },
                serverBoxes,
                servers.length > 1
                  ? h(
                      'div',
                      { class: 'server-copy-all' },
                      copyButton(servers.join(', '), 'Copy both addresses', true, 'Copy both'),
                    )
                  : null,
              )
            : null,
          tied.length
            ? h(
                'div',
                { class: 'tied' },
                h('span', { class: 'tied-label' }, 'Statistically tied with'),
                tied.map((n) =>
                  h('a', { class: 'chip chip-sm', href: hashFor('resolver', n) }, dot(n), h('span', null, n)),
                ),
                h(
                  'span',
                  { class: 'muted small' },
                  `Medians within 2 ms or with overlapping intervals, and no significant difference in slow answers, failures or retries; ${tied.length > 1 ? 'any of them' : 'either'} is a good choice.`,
                ),
              )
            : null,
        ),
        h(
          'div',
          { class: 'rec-side' },
          h(
            'div',
            { class: 'kpis kpis-2' },
            msKpi('Median', best.median),
            msKpi('95th percentile', best.p95),
            msKpi('Mean', best.mean),
            kpi(
              'Failure rate',
              fmtPct(best.failure_rate),
              '',
              isNum(best.n) ? `${fmtInt(best.ok)} of ${fmtInt(best.n)} answered` : null,
            ),
          ),
        ),
      ),
      notesList(rec.notes),
    );
  }

  /** A recommendation's notes: {code, params, text}. The server writes the text; this only shows it. */
  function notesList(notes) {
    const list = (Array.isArray(notes) ? notes : []).filter((n) => n && typeof n.text === 'string');
    if (!list.length) return null;
    return h(
      'ul',
      { class: 'notes' },
      list.map((n) =>
        h('li', { dataset: { code: String(n.code) } }, icon('info', 14), h('span', null, n.text)),
      ),
    );
  }

  function viewOverview(ds) {
    const sum = ds.summary || {};
    const rec = ds.recommendation || {};
    const names = resolverNames(sum);
    const byRes = sum.by_resolver || {};
    const ranks = rankMap(rec);
    const items = names.map((n) => ({ name: n, st: byRes[n] || {}, rank: ranks.get(n) || null }));
    const sorted = items
      .slice()
      .sort((a, b) => cmpNullLast(a.st.mean, b.st.mean) || cmpNullLast(a.st.median, b.st.median));
    if (!items.length)
      return h(
        'div',
        { class: 'view' },
        recommendationCard(ds),
        emptyPanel('No resolver data', 'This dataset does not contain any resolver results.'),
      );

    const latency = card(
      {
        title: 'Average latency by resolver',
        sub: 'Mean time to answer, fastest first. Timeouts and errors are left out here and counted as failures instead.',
        right: rangeLegend(),
      },
      chartBox((w) =>
        barChart(
          w,
          sorted.map((it) => ({
            label: it.name,
            color: colorOf(it.name),
            value: it.st.mean,
            median: it.st.median,
            p95: it.st.p95,
            valueUnit: 'ms',
            tip: () => statTip(it.name, it.st, it.rank, colorOf(it.name)),
            onClick: () => go('resolver', it.name),
            aria: `${it.name}: mean ${fmtMsU(it.st.mean)}, median ${fmtMsU(it.st.median)}, 95th percentile ${fmtMsU(it.st.p95)}`,
          })),
          { unit: 'ms', focusable: true, ariaLabel: 'Average latency by resolver' },
        ),
      ),
    );

    const failures = card(
      {
        title: 'Failure rate',
        sub: 'Timeouts and error answers as a share of all queries. Each failure costs a full timeout before your device tries the next server.',
      },
      chartBox((w) =>
        barChart(
          w,
          sorted.map((it) => ({
            label: it.name,
            color: colorOf(it.name),
            value: isNum(it.st.n) && it.st.n > 0 ? it.st.failure_rate || 0 : null,
            valueText: fmtPct(it.st.failure_rate),
            extra: isNum(it.st.n) && it.st.n > 0 ? `${fmtInt(it.st.failures)} of ${fmtInt(it.st.n)}` : '',
            extraClass: 'muted-fill',
            missingText: 'No queries',
            tip: () =>
              tipContent(
                it.name,
                [
                  [fmtPct(it.st.failure_rate), 'failed'],
                  [fmtInt(it.st.timeouts), 'timeouts'],
                  [fmtInt(it.st.errors), 'error answers'],
                  [fmtInt(it.st.n), 'queries'],
                ],
                colorOf(it.name),
              ),
            onClick: () => go('resolver', it.name),
            aria:
              isNum(it.st.n) && it.st.n > 0
                ? `${it.name}: ${fmtPct(it.st.failure_rate)} failed, ${fmtInt(it.st.failures)} of ${fmtInt(it.st.n)} queries`
                : `${it.name}: no queries`,
          })),
          {
            tickFmt: fmtPctTick,
            minMax: 0.05,
            valueW: 128,
            zeroNoBar: true,
            focusable: true,
            ariaLabel: 'Failure rate by resolver',
          },
        ),
      ),
    );

    const num = (key, label) => ({
      key,
      label,
      num: true,
      get: (r) => r.st[key],
      render: (r) => fmtMs(r.st[key]),
    });
    // Combined view: say how many of the combined runs measured each resolver, since a
    // resolver seen in only some runs is not directly comparable with the others.
    const cov = ds.kind === 'aggregate' && ds.coverage ? ds.coverage : null;
    const covOf = (name) => {
      const c = cov?.[name];
      return c && isNum(c.runs) && isNum(c.of) ? c : null;
    };
    const runsCol = cov
      ? [
          {
            key: 'runs',
            label: 'Runs',
            num: true,
            get: (r) => {
              const c = covOf(r.name);
              return c ? c.runs : null;
            },
            render: (r) => {
              const c = covOf(r.name);
              if (!c) return '—';
              const last = c.last_run ? runRow(c.last_run) : null;
              const title =
                `Measured in ${fmtInt(c.runs)} of ${plural(c.of, 'combined run')}` +
                (c.last_run ? `; last in the run of ${last ? fmtDate(last.started_at) : c.last_run}` : '');
              return h(
                'span',
                { class: c.runs < c.of ? 'status status-warn' : null, title },
                `${fmtInt(c.runs)} / ${fmtInt(c.of)}`,
              );
            },
          },
        ]
      : [];
    const table = card(
      {
        title: 'Resolver statistics',
        sub:
          "Milliseconds over the first successful answer of each domain from each resolver (repeats are mostly cache hits). Score combines median, p95, mean and failures, counting a failure rate only when it is significantly higher than another resolver's; lower is better. = marks a rank within noise of the one above. Click a column to sort." +
          (cov ? ' Runs shows how many of the combined runs measured each resolver.' : ''),
      },
      dataTable(
        'overview-stats',
        [
          {
            key: 'resolver',
            label: 'Resolver',
            head: true,
            get: (r) => r.name,
            render: (r) =>
              h(
                'a',
                { class: 'res-link', href: hashFor('resolver', r.name) },
                dot(r.name),
                h('span', { class: 'res-link-name' }, r.name),
                r.name === rec.best ? h('span', { class: 'badge badge-good' }, 'Best') : null,
              ),
          },
          num('mean', 'Mean'),
          num('median', 'Median'),
          num('p80', 'p80'),
          num('p95', 'p95'),
          num('p98', 'p98'),
          num('min', 'Min'),
          num('max', 'Max'),
          {
            key: 'ok',
            label: 'OK / n',
            num: true,
            get: (r) => r.st.ok,
            render: (r) => `${fmtInt(r.st.ok)} / ${fmtInt(r.st.n)}`,
          },
          {
            key: 'fail',
            label: 'Fail %',
            num: true,
            get: (r) => r.st.failure_rate,
            render: (r) => failBadge(r.st),
          },
          ...runsCol,
          {
            key: 'score',
            label: 'Score',
            num: true,
            get: (r) => (r.rank ? r.rank.score : null),
            render: (r) => (r.rank ? fmtNum(r.rank.score, 1) : '—'),
          },
          {
            key: 'rank',
            label: 'Rank',
            num: true,
            get: (r) => (r.rank ? r.rank.rank : null),
            render: (r) => {
              if (!r.rank) return '—';
              const ties = Array.isArray(r.rank.ties) ? r.rank.ties : [];
              const above = [...ranks.values()].find((e) => e.rank === r.rank.rank - 1);
              const tiedAbove = !!above && ties.includes(above.resolver);
              return h(
                'span',
                { title: ties.length ? `Within noise of ${ties.join(', ')}` : null },
                `#${r.rank.rank}${tiedAbove ? ' =' : ''}`,
              );
            },
          },
        ],
        items,
        {
          defaultSort: { key: 'rank', dir: 1 },
          caption: 'Latency statistics per resolver',
          rowAttrs: (r) => ({ class: r.name === rec.best ? 'is-best' : null }),
        },
      ),
    );

    const hint =
      ds.kind === 'aggregate' && state.runs.length < 2
        ? h(
            'p',
            { class: 'muted small center' },
            'Run more benchmarks to see how resolvers change over time.',
          )
        : null;

    return h(
      'div',
      { class: 'view' },
      recommendationCard(ds),
      h('div', { class: 'grid grid-main-side' }, latency, failures),
      table,
      trendCard(ds),
      hint,
    );
  }

  // ================================================================ views: by resolver
  /** The resolver the By resolver view shows: the route's, else the last one shown, else the best. */
  function selectedResolver(ds) {
    const names = resolverNames(ds.summary || {});
    const arg = state.route.arg;
    if (arg && names.includes(arg)) return arg;
    if (state.ui.lastResolver && names.includes(state.ui.lastResolver)) return state.ui.lastResolver;
    const best = ds.recommendation?.best;
    return best && names.includes(best) ? best : names[0] || null;
  }

  function viewResolver(ds) {
    const sum = ds.summary || {};
    const rec = ds.recommendation || {};
    const names = resolverNames(sum);
    if (!names.length)
      return emptyPanel('No resolver data', 'This dataset does not contain any resolver results.');
    const sel = selectedResolver(ds);
    const byRes = sum.by_resolver || {};
    const st = byRes[sel] || {};
    const rank = rankMap(rec).get(sel) || null;
    const color = colorOf(sel);
    const settings = dsSettings(ds);
    const thr = settings.slow_threshold_ms;

    const chips = h(
      'nav',
      { class: 'chips', 'aria-label': 'Choose a resolver' },
      names.map((n) =>
        h(
          'a',
          {
            class: 'chip',
            href: hashFor('resolver', n),
            'aria-current': n === sel ? 'true' : null,
            dataset: { name: n },
            onClick: () => {
              if (n !== sel) focusAfterRender(`.chips .chip[data-name="${CSS.escape(n)}"]`);
            },
          },
          dot(n),
          h('span', { class: 'chip-name' }, n),
          h('span', { class: 'chip-meta' }, fmtMsU(byRes[n]?.median)),
        ),
      ),
    );

    // servers
    const servMap = sum.by_server?.[sel] || {};
    const cfgRes = (ds.config?.resolvers || []).find((r) => r.name === sel);
    const servIps = (cfgRes?.servers || []).filter((ip) => servMap[ip]);
    for (const ip of Object.keys(servMap)) if (!servIps.includes(ip)) servIps.push(ip);
    // The server to put first is the recommendation's pick (recommend.py). A resolver without a
    // ranking entry has no server that answered, so there is nothing to pick.
    const fastest = rank?.fastest_server;
    const serverItems = servIps.map((ip) => ({ ip, st: servMap[ip] || {} }));
    const head = h(
      'div',
      { class: 'res-header' },
      h(
        'div',
        { class: 'res-title' },
        h('span', { class: 'dot dot-lg', style: { background: color } }),
        h('h2', null, sel),
        rank
          ? h(
              'span',
              { class: `badge${sel === rec.best ? ' badge-good' : ''}` },
              sel === rec.best ? 'Best' : `Rank #${rank.rank}`,
            )
          : h('span', { class: 'badge badge-bad' }, 'Unranked'),
      ),
      h(
        'div',
        { class: 'res-sub' },
        servIps.length
          ? `${plural(servIps.length, 'server')}: ${servIps.join(', ')}`
          : 'No servers in this dataset',
        coverageNote(ds, sel),
      ),
    );

    const kpis = h(
      'div',
      { class: 'kpis kpis-6' },
      msKpi(
        'Mean',
        st.mean,
        st.repeat_n > 0
          ? `${plural(st.repeat_n, 'repeat')} left out (may be cached): median ${fmtMsU(st.repeat_median)}`
          : null,
      ),
      msKpi('Median', st.median, ciText(st.median_ci)),
      msKpi('95th percentile', st.p95, isNum(st.first_n) ? `of ${plural(st.first_n, 'first answer')}` : null),
      msKpi('98th percentile', st.p98),
      kpi(
        'Failure rate',
        fmtPct(st.failure_rate),
        '',
        isNum(st.n)
          ? `${fmtInt(st.timeouts)} timeouts · ${fmtInt(st.errors)} errors` +
              (st.retried > 0 ? ` · ${fmtInt(st.retried)} retried` : '') +
              (st.local_errors > 0 ? ` · ${fmtInt(st.local_errors)} local, not counted` : '')
          : null,
        st.failure_rate > 0.02 ? 'kpi-bad' : '',
      ),
      kpi('Answered', fmtInt(st.ok), '', `of ${fmtInt(st.n)} queries`),
    );

    const num = (key, label) => ({
      key,
      label,
      num: true,
      get: (r) => r.st[key],
      render: (r) => fmtMs(r.st[key]),
    });
    const serversCard = card(
      {
        title: 'Servers',
        sub: 'List the server marked "Put first" first in your network settings: it had the lowest median once failures and retries are counted (servers within noise of each other go by config order).',
        right: rangeLegend(),
      },
      h(
        'div',
        { class: 'split' },
        chartBox((w) =>
          barChart(
            w,
            serverItems.map((it) => ({
              label: it.ip,
              color,
              value: it.st.mean,
              median: it.st.median,
              p95: it.st.p95,
              valueUnit: 'ms',
              tip: () => statTip(it.ip, it.st, null, color),
            })),
            {
              unit: 'ms',
              ariaLabel: `Latency per server for ${sel}`,
              emptyText: 'None of these servers answered, so there is no latency to chart.',
            },
          ),
        ),
        dataTable(
          'resolver-servers',
          [
            {
              key: 'ip',
              label: 'Server',
              head: true,
              get: (r) => r.ip,
              render: (r) =>
                h(
                  'span',
                  { class: 'nowrap' },
                  h('code', null, r.ip),
                  r.ip === fastest
                    ? h(
                        'span',
                        {
                          class: 'badge badge-good',
                          title: 'Put this server first in your network settings',
                        },
                        'Put first',
                      )
                    : null,
                ),
            },
            num('mean', 'Mean'),
            num('median', 'Median'),
            num('p80', 'p80'),
            num('p95', 'p95'),
            num('p98', 'p98'),
            num('min', 'Min'),
            num('max', 'Max'),
            {
              key: 'ok',
              label: 'OK / n',
              num: true,
              get: (r) => r.st.ok,
              render: (r) => `${fmtInt(r.st.ok)} / ${fmtInt(r.st.n)}`,
            },
            {
              key: 'fail',
              label: 'Fail %',
              num: true,
              get: (r) => r.st.failure_rate,
              render: (r) => failBadge(r.st),
            },
          ],
          serverItems,
          { caption: `Servers of ${sel}` },
        ),
      ),
    );

    // histogram
    let histValues;
    let histSub;
    let countLabel = 'queries';
    if (ds.results) {
      histValues = ds.results
        .filter((r) => r.resolver === sel && r.status === 'ok' && isNum(r.ms))
        .map((r) => r.ms);
      histSub = histValues.length
        ? `All ${plural(histValues.length, 'successful answer')}, repeats included (the figures above use first answers only), 10 bins. The last bin also holds anything slower.`
        : null;
    } else {
      const bd = sum.by_domain || {};
      histValues = domainNames(sum)
        .map((d) => bd[d]?.[sel]?.median)
        .filter(isNum);
      histSub = 'Per-domain medians across all runs (raw samples are not kept in combined view), 10 bins.';
      countLabel = 'domains';
    }
    const histCard = card(
      { title: 'Latency distribution', sub: histSub },
      chartBox((w) =>
        histogram(w, histValues, { color, countLabel, ariaLabel: `Latency distribution for ${sel}` }),
      ),
    );

    // per-domain
    const bd = sum.by_domain || {};
    const domainItems = domainNames(sum)
      .map((d) => ({ d, st: bd[d]?.[sel] || null }))
      .filter((x) => x.st);
    domainItems.sort((a, b) => {
      const an = !isNum(a.st.median);
      const bn = !isNum(b.st.median);
      if (an !== bn) return an ? -1 : 1;
      return cmpNullLast(a.st.median, b.st.median, -1);
    });
    const slowCount = domainItems.filter((x) => isNum(x.st.median) && x.st.median > thr).length;
    const showAll = state.ui.showAllDomains;
    const shownDomains = showAll ? domainItems : domainItems.slice(0, DOMAIN_CHART_LIMIT);
    const toggle =
      domainItems.length > DOMAIN_CHART_LIMIT
        ? h(
            'button',
            {
              type: 'button',
              class: 'btn btn-sm',
              dataset: { focus: 'show-all' },
              onClick: () => {
                state.ui.showAllDomains = !state.ui.showAllDomains;
                focusAfterRender('[data-focus="show-all"]');
                scheduleRender();
              },
            },
            showAll ? `Show slowest ${DOMAIN_CHART_LIMIT}` : `Show all ${fmtInt(domainItems.length)}`,
          )
        : null;
    const domainCard = card(
      {
        title: 'Median by domain',
        sub: `Slowest first${domainItems.length > shownDomains.length ? `, top ${shownDomains.length} of ${fmtInt(domainItems.length)}` : ''}. ${slowCount ? `${plural(slowCount, 'domain')} above the ${fmtInt(thr)} ms slow threshold.` : `No domain above the ${fmtInt(thr)} ms slow threshold.`} Click a bar for details.`,
        right: h(
          'div',
          { class: 'card-tools' },
          legend(
            [{ kind: 'bar', color, label: 'Median' }].concat(
              slowCount
                ? [
                    { kind: 'slow', label: `Slow (> ${fmtInt(thr)} ms)` },
                    { kind: 'threshold', label: 'Slow threshold' },
                  ]
                : [],
            ),
          ),
          toggle,
        ),
      },
      h(
        'div',
        { class: 'scroll-y' },
        chartBox((w) =>
          barChart(
            w,
            shownDomains.map((x) => {
              const slow = isNum(x.st.median) && x.st.median > thr;
              return {
                label: x.d,
                color,
                value: x.st.median,
                valueUnit: 'ms',
                status: slow ? 'slow' : null,
                extra: slow ? '▲ slow' : x.st.failures ? `${x.st.failures} failed` : '',
                extraClass: slow ? 'text-serious' : 'text-critical',
                missingText: `× all ${fmtInt(x.st.n)} failed`,
                tip: () => statTip(x.d, x.st, null, color),
                onClick: () => go('domain', x.d),
                aria: isNum(x.st.median)
                  ? `${x.d}: median ${fmtMsU(x.st.median)}${slow ? ', slow' : ''}${x.st.failures ? `, ${fmtInt(x.st.failures)} failed` : ''}`
                  : `${x.d}: all ${fmtInt(x.st.n)} failed`,
              };
            }),
            {
              unit: 'ms',
              rowH: 26,
              barH: 12,
              valueW: 118,
              threshold: thr,
              maxLabelW: 200,
              focusable: true,
              ariaLabel: `Median latency per domain for ${sel}`,
            },
          ),
        ),
      ),
    );

    // slow & failed
    const slowInfo = slowQueriesFor(ds, sel, thr);
    const slowRows = slowInfo.rows;
    let slowSub = null;
    if (slowInfo.total !== null && slowInfo.total > slowRows.length) {
      slowSub = `${plural(slowInfo.total, 'answer')} slower than ${fmtInt(thr)} ms; showing the slowest ${fmtInt(slowRows.length)}.`;
    } else if (slowInfo.total === null && slowRows.length) {
      slowSub = `At least ${plural(slowRows.length, 'answer')} slower than ${fmtInt(thr)} ms. The combined list keeps only the slowest ${fmtInt(slowInfo.cap)} answers across all resolvers, so some of this resolver's may be missing.`;
    } else if (slowRows.length) {
      slowSub = `${plural(slowRows.length, 'answer')} slower than ${fmtInt(thr)} ms, slowest first.`;
    }
    const slowEmpty =
      slowInfo.total === null
        ? `None of this resolver's slow answers made the combined list, which keeps only the slowest ${fmtInt(slowInfo.cap)} answers across all resolvers. Open a single run to see them all.`
        : `No answers slower than ${fmtInt(thr)} ms.`;
    const slowCard = card(
      { title: 'Slow queries', sub: slowSub },
      slowRows.length
        ? h(
            'div',
            { class: 'scroll-y scroll-y-sm' },
            dataTable(
              'resolver-slow',
              [
                {
                  key: 'domain',
                  label: 'Domain',
                  head: true,
                  get: (r) => r.domain,
                  render: (r) => h('a', { href: hashFor('domain', r.domain) }, r.domain),
                },
                {
                  key: 'server',
                  label: 'Server',
                  get: (r) => r.server,
                  render: (r) => h('code', null, r.server),
                },
                {
                  key: 'ms',
                  label: 'Time',
                  num: true,
                  get: (r) => r.ms,
                  render: (r) => fmtMsU(r.ms),
                  defaultDir: -1,
                },
                ds.kind === 'aggregate'
                  ? // Combined view: rows come from different runs, so say which one.
                    {
                      key: 'run',
                      label: 'Run',
                      get: (r) => r.run_id || '',
                      render: (r) => {
                        const rr = r.run_id ? runRow(r.run_id) : null;
                        return h(
                          'span',
                          { class: 'nowrap' },
                          rr ? fmtDate(rr.started_at, 'short') : String(r.run_id || '—'),
                        );
                      },
                    }
                  : {
                      key: 'round',
                      label: 'Round',
                      num: true,
                      get: (r) => r.round,
                      render: (r) => fmtInt(r.round),
                    },
              ],
              slowRows,
              { defaultSort: { key: 'ms', dir: -1 } },
            ),
          )
        : h('p', { class: 'muted' }, slowEmpty),
    );

    let failCard;
    if (ds.results) {
      const failed = ds.results.filter((r) => r.resolver === sel && r.status !== 'ok');
      failCard = card(
        {
          title: 'Failed queries',
          sub: failed.length
            ? `${plural(failed.length, 'query', 'queries')} timed out or got an error answer.`
            : null,
        },
        failed.length
          ? h(
              'div',
              { class: 'scroll-y scroll-y-sm' },
              dataTable(
                'resolver-failed',
                [
                  {
                    key: 'domain',
                    label: 'Domain',
                    head: true,
                    get: (r) => r.domain,
                    render: (r) => h('a', { href: hashFor('domain', r.domain) }, r.domain),
                  },
                  {
                    key: 'server',
                    label: 'Server',
                    get: (r) => r.server,
                    render: (r) => h('code', null, r.server),
                  },
                  {
                    key: 'status',
                    label: 'Result',
                    get: (r) => r.rcode || r.status,
                    render: (r) =>
                      h(
                        'span',
                        { class: 'status status-critical' },
                        icon('alert', 13),
                        r.status === 'timeout' ? 'Timeout' : String(r.rcode || r.error || 'Error'),
                      ),
                  },
                  {
                    key: 'round',
                    label: 'Round',
                    num: true,
                    get: (r) => r.round,
                    render: (r) => fmtInt(r.round),
                  },
                ],
                failed,
                {
                  defaultSort: { key: 'domain', dir: 1 },
                  limit: FAILED_TABLE_MAX,
                  more: (shown, total) =>
                    h(
                      'p',
                      { class: 'muted small table-more' },
                      `Showing ${fmtInt(shown)} of ${fmtInt(total)} failed queries, in the order above. `,
                      h(
                        'a',
                        {
                          href: `/api/runs/${encodeURIComponent(ds.id)}/csv`,
                          download: `dns-bench-${ds.id}.csv`,
                        },
                        'Download every result as CSV',
                      ),
                      '.',
                    ),
                },
              ),
            )
          : h('p', { class: 'muted' }, 'Every query was answered.'),
      );
    } else {
      failCard = card(
        { title: 'Failures by server', sub: 'Totals across all runs.' },
        dataTable(
          'resolver-failagg',
          [
            {
              key: 'ip',
              label: 'Server',
              head: true,
              get: (r) => r.ip,
              render: (r) => h('code', null, r.ip),
            },
            {
              key: 'timeouts',
              label: 'Timeouts',
              num: true,
              get: (r) => r.st.timeouts,
              render: (r) => fmtInt(r.st.timeouts),
            },
            {
              key: 'errors',
              label: 'Errors',
              num: true,
              get: (r) => r.st.errors,
              render: (r) => fmtInt(r.st.errors),
            },
            ...(serverItems.some((r) => r.st.local_errors > 0)
              ? [
                  {
                    key: 'local',
                    label: 'Local',
                    num: true,
                    get: (r) => r.st.local_errors || 0,
                    render: (r) =>
                      h(
                        'span',
                        { title: 'Failed on this computer; not counted' },
                        fmtInt(r.st.local_errors || 0),
                      ),
                  },
                ]
              : []),
            {
              key: 'fail',
              label: 'Fail %',
              num: true,
              get: (r) => r.st.failure_rate,
              render: (r) => failBadge(r.st),
            },
          ],
          serverItems,
          {},
        ),
      );
    }

    return h(
      'div',
      { class: 'view' },
      chips,
      h('section', { class: 'card' }, head, kpis),
      serversCard,
      h('div', { class: 'grid grid-2' }, histCard, slowCard),
      domainCard,
      failCard,
    );
  }

  // ================================================================ views: by domain
  function viewDomain(ds) {
    const sum = ds.summary || {};
    const names = resolverNames(sum);
    const byDomain = sum.by_domain || {};
    const byRes = sum.by_resolver || {};
    const domains = domainNames(sum);
    if (!domains.length || !names.length)
      return emptyPanel('No per-domain data', 'This dataset does not contain per-domain results.');

    const rows = domains.map((d, idx) => {
      const cells = names.map((n) => byDomain[d]?.[n] || null);
      const meds = cells.map((c) => (c && isNum(c.median) ? c.median : null));
      const vals = meds.filter(isNum);
      const allFailed = !vals.length && cells.some((c) => c && c.n > 0);
      return {
        domain: d,
        lower: d.toLowerCase(),
        idx,
        cells,
        avg: vals.length ? mean(vals) : allFailed ? Infinity : null,
        spread: vals.length >= 2 ? Math.max(...vals) - Math.min(...vals) : null,
        meds,
      };
    });
    const scale = heatScale(rows.flatMap((r) => r.meds));
    const cellInfo = new WeakMap();

    const count = h('span', { class: 'muted small' });
    const search = h('input', {
      type: 'search',
      class: 'input input-search',
      placeholder: 'Filter domains…',
      'aria-label': 'Filter domains',
      value: state.ui.domainQuery,
      autocomplete: 'off',
      spellcheck: 'false',
    });
    search.addEventListener(
      'input',
      debounce(() => {
        state.ui.domainQuery = search.value;
        drawTable();
      }, 120),
    );
    const sortSel = h(
      'select',
      { class: 'input select', 'aria-label': 'Sort domains' },
      h('option', { value: 'list' }, 'List order'),
      h('option', { value: 'name' }, 'Domain name (A–Z)'),
      h('option', { value: 'slowest' }, 'Slowest first'),
      h('option', { value: 'spread' }, 'Biggest spread between resolvers'),
    );
    sortSel.value = state.ui.domainSort;
    sortSel.addEventListener('change', () => {
      state.ui.domainSort = sortSel.value;
      drawTable();
    });

    const heatLegend = scale
      ? h(
          'div',
          { class: 'heat-legend' },
          h('span', { class: 'muted small' }, 'Median'),
          h(
            'div',
            { class: 'heat-scale' },
            h('div', { class: 'heat-bar', 'aria-hidden': 'true' }),
            h(
              'div',
              { class: 'heat-ticks' },
              h('span', null, `≤ ${fmtMsU(scale.lo)}`),
              h('span', null, fmtMsU(scale.mid)),
              h('span', null, `≥ ${fmtMsU(scale.hi)}`),
            ),
          ),
          h(
            'span',
            { class: 'heat-key' },
            h('span', { class: 'heat-sw heat-sw-fail', 'aria-hidden': 'true' }, '×'),
            'all failed',
          ),
          h(
            'span',
            { class: 'heat-key' },
            h('span', { class: 'heat-sw heat-sw-partial', 'aria-hidden': 'true' }, '!'),
            'some failed',
          ),
        )
      : null;

    const tableWrap = h('div', { class: 'table-wrap heat-wrap' });
    const detail = h('aside', { class: 'card domain-detail', hidden: true, 'aria-live': 'polite' });
    const layout = h(
      'div',
      { class: 'domain-layout' },
      h('div', { class: 'domain-main' }, tableWrap),
      detail,
    );

    function sortedRows() {
      const q = state.ui.domainQuery.trim().toLowerCase();
      const list = q ? rows.filter((r) => r.lower.includes(q)) : rows.slice();
      const mode = state.ui.domainSort;
      if (mode === 'name') list.sort((a, b) => a.domain.localeCompare(b.domain));
      // avg === Infinity means every resolver failed that domain: rank it as the slowest.
      // (cmpNullLast would treat Infinity as missing and push it to the bottom.)
      else if (mode === 'slowest')
        list.sort((a, b) => (b.avg === Infinity) - (a.avg === Infinity) || cmpNullLast(a.avg, b.avg, -1));
      else if (mode === 'spread') list.sort((a, b) => cmpNullLast(a.spread, b.spread, -1));
      return list;
    }

    function heatCell(c, name, domain) {
      if (!c?.n) return h('td', { class: 'heat heat-none' }, '–');
      let td;
      if (!c.ok || !isNum(c.median)) {
        td = h(
          'td',
          { class: 'heat heat-fail' },
          h('span', { 'aria-hidden': 'true' }, '×'),
          h('span', { class: 'sr-only' }, 'all failed'),
        );
      } else {
        td = h(
          'td',
          {
            class: `heat${c.failures ? ' heat-partial' : ''}`,
            style: { background: scale ? heatBg(scale.t(c.median)) : null },
          },
          fmtMs(c.median),
        );
        if (c.failures) td.appendChild(h('span', { class: 'fail-mark', title: `${c.failures} failed` }, '!'));
      }
      cellInfo.set(td, { c, name, domain });
      return td;
    }

    function drawTable() {
      const list = sortedRows();
      const sel = state.route.arg;
      count.textContent =
        list.length === rows.length
          ? `${plural(rows.length, 'domain')}`
          : `${fmtInt(list.length)} of ${plural(rows.length, 'domain')}`;
      const thead = h(
        'thead',
        null,
        h(
          'tr',
          null,
          h('th', { scope: 'col', class: 'corner' }, 'Domain'),
          names.map((n) =>
            h(
              'th',
              { scope: 'col', class: 'num res-col' },
              h('div', { class: 'res-head' }, dot(n), h('span', { class: 'res-head-name', title: n }, n)),
              h('div', { class: 'res-head-sub' }, `avg ${fmtMsU(byRes[n]?.mean)}`),
            ),
          ),
          h(
            'th',
            { scope: 'col', class: 'num' },
            h('div', null, 'Spread'),
            h('div', { class: 'res-head-sub' }, 'max − min'),
          ),
        ),
      );
      const tbody = h('tbody');
      for (const r of list) {
        const tr = h(
          'tr',
          { class: r.domain === sel ? 'is-selected' : null, dataset: { domain: r.domain } },
          h(
            'th',
            { scope: 'row', class: 'domain-cell' },
            h('button', { type: 'button', class: 'link-btn', title: r.domain }, r.domain),
          ),
          r.cells.map((c, j) => heatCell(c, names[j], r.domain)),
          h('td', { class: 'num spread' }, isNum(r.spread) ? fmtMs(r.spread) : '—'),
        );
        tbody.appendChild(tr);
      }
      if (!list.length)
        tbody.appendChild(
          h(
            'tr',
            null,
            h('td', { colspan: names.length + 2, class: 'table-empty' }, 'No domains match your filter.'),
          ),
        );
      tbody.addEventListener('click', (e) => {
        const tr = e.target.closest('tr[data-domain]');
        if (tr) go('domain', tr.dataset.domain);
      });
      tbody.addEventListener('pointerover', (e) => {
        const td = e.target.closest('td.heat');
        const info = td && cellInfo.get(td);
        if (!info) {
          hideTip();
          return;
        }
        const c = info.c;
        showTip(
          e,
          tipContent(
            info.domain,
            [
              [fmtMsU(c.median), 'median'],
              [fmtMsU(c.mean), 'mean'],
              [fmtMsU(c.p95), '95th percentile'],
              [`${fmtInt(c.ok)} of ${fmtInt(c.n)}`, 'answered'],
              c.failures ? [`${fmtInt(c.timeouts)} / ${fmtInt(c.errors)}`, 'timeouts / errors'] : null,
            ].concat([[info.name, 'resolver', colorOf(info.name)]]),
          ),
        );
      });
      tbody.addEventListener('pointermove', positionTip);
      tbody.addEventListener('pointerleave', hideTip);
      setKids(
        tableWrap,
        h(
          'table',
          { class: 'data-table heat-table' },
          h('caption', { class: 'sr-only' }, 'Median latency per domain and resolver'),
          thead,
          tbody,
        ),
      );
    }

    function drawDetail() {
      const d = state.route.arg;
      for (const tr of tableWrap.querySelectorAll('tr.is-selected')) tr.classList.remove('is-selected');
      if (!d || !domains.includes(d)) {
        detail.hidden = true;
        layout.classList.remove('has-detail');
        setKids(detail);
        return;
      }
      for (const tr of tableWrap.querySelectorAll('tr[data-domain]'))
        if (tr.dataset.domain === d) tr.classList.add('is-selected');
      detail.hidden = false;
      layout.classList.add('has-detail');
      const cells = byDomain[d] || {};
      const chartRows = names.map((n) => {
        const c = cells[n] || null;
        return {
          label: n,
          color: colorOf(n),
          value: c ? c.mean : null,
          median: c ? c.median : null,
          p95: c ? c.p95 : null,
          valueUnit: 'ms',
          extra: c?.failures && c.ok ? `${c.failures} failed` : '',
          extraClass: 'text-critical',
          missingText: c?.n ? `× all ${fmtInt(c.n)} failed` : 'not queried in this dataset',
          missingMuted: !c?.n,
          tip: () => statTip(`${n} · ${d}`, c || {}, null, colorOf(n)),
        };
      });
      const samples = ds.results ? ds.results.filter((r) => r.domain === d) : null;
      setKids(
        detail,
        h(
          'div',
          { class: 'detail-head' },
          h('div', null, h('div', { class: 'eyebrow' }, 'Domain'), h('h2', { class: 'detail-title' }, d)),
          h(
            'button',
            {
              type: 'button',
              class: 'btn btn-icon btn-ghost',
              'aria-label': 'Close domain details',
              onClick: () => {
                focusAfterRender(`tr[data-domain="${CSS.escape(d)}"] .link-btn`);
                go('domain');
              },
            },
            icon('x'),
          ),
        ),
        rangeLegend(),
        chartBox((w) =>
          barChart(w, chartRows, { unit: 'ms', valueW: 96, ariaLabel: `Latency for ${d} by resolver` }),
        ),
        dataTable(
          'domain-detail',
          [
            {
              key: 'r',
              label: 'Resolver',
              head: true,
              get: (r) => r.n,
              render: (r) => h('span', { class: 'nowrap' }, dot(r.n), ' ', r.n),
            },
            {
              key: 'median',
              label: 'Median',
              num: true,
              get: (r) => r.c?.median,
              render: (r) => fmtMs(r.c?.median),
            },
            {
              key: 'mean',
              label: 'Mean',
              num: true,
              get: (r) => r.c?.mean,
              render: (r) => fmtMs(r.c?.mean),
            },
            {
              key: 'p95',
              label: 'p95',
              num: true,
              get: (r) => r.c?.p95,
              render: (r) => fmtMs(r.c?.p95),
            },
            {
              key: 'ok',
              label: 'OK / n',
              num: true,
              get: (r) => r.c?.ok,
              render: (r) => (r.c ? `${fmtInt(r.c.ok)} / ${fmtInt(r.c.n)}` : '—'),
            },
          ],
          names.map((n) => ({ n, c: cells[n] || null })),
          { defaultSort: { key: 'median', dir: 1 }, wrapClass: 'mt' },
        ),
        samples?.length
          ? h(
              'details',
              { class: 'samples' },
              h('summary', null, `Every query for this domain (${fmtInt(samples.length)})`),
              dataTable(
                'domain-samples',
                [
                  {
                    key: 'resolver',
                    label: 'Resolver',
                    get: (r) => r.resolver,
                    render: (r) => h('span', { class: 'nowrap' }, dot(r.resolver), ' ', r.resolver),
                  },
                  {
                    key: 'server',
                    label: 'Server',
                    get: (r) => r.server,
                    render: (r) => h('code', null, r.server),
                  },
                  {
                    key: 'round',
                    label: 'Rd',
                    num: true,
                    get: (r) => r.round,
                    render: (r) => fmtInt(r.round),
                  },
                  {
                    key: 'ms',
                    label: 'Result',
                    num: true,
                    get: (r) => (r.status === 'ok' ? r.ms : null),
                    render: (r) =>
                      r.status === 'ok'
                        ? fmtMsU(r.ms)
                        : h(
                            'span',
                            { class: 'status status-critical' },
                            r.status === 'timeout' ? 'timeout' : String(r.rcode || 'error'),
                          ),
                  },
                ],
                samples,
                { defaultSort: { key: 'ms', dir: 1 }, wrapClass: 'mt' },
              ),
            )
          : null,
      );
      flushCharts();
    }

    drawTable();
    drawDetail();
    viewHooks.update = (arg) => {
      drawDetail();
      if (arg && window.matchMedia('(max-width: 1080px)').matches)
        detail.scrollIntoView({ behavior: scrollBehavior(), block: 'start' });
      return true;
    };

    return h(
      'div',
      { class: 'view' },
      h(
        'section',
        { class: 'card' },
        h(
          'div',
          { class: 'card-head' },
          h(
            'div',
            { class: 'card-titles' },
            h('h2', { class: 'card-title' }, 'Median latency by domain'),
            h(
              'p',
              { class: 'card-sub' },
              'Each cell is the median time for one domain on one resolver. Colours are relative to this dataset: green is fast, red is slow. Click a row for details.',
            ),
          ),
          heatLegend,
        ),
        h(
          'div',
          { class: 'toolbar' },
          h('label', { class: 'search-field' }, icon('search', 15), search),
          sortSel,
          count,
        ),
        layout,
      ),
    );
  }

  // ================================================================ views: history
  function viewHistory() {
    if (!state.runs.length) return emptyRunsPanel();
    const current = state.dataset && state.dataset.kind === 'run' ? state.dataset.id : null;
    const allBtn = h(
      'button',
      {
        type: 'button',
        class: 'btn btn-sm',
        onClick: () => {
          selectDataset('all');
          go('overview');
        },
      },
      icon('layers', 14),
      h('span', null, 'View all runs combined'),
    );
    const table = dataTable(
      'history',
      [
        {
          key: 'date',
          label: 'Started',
          head: true,
          get: (r) => r.started_at,
          defaultDir: -1,
          render: (r) =>
            h(
              'span',
              { class: 'nowrap' },
              fmtDate(r.started_at),
              r.id === current ? h('span', { class: 'badge' }, 'Viewing') : null,
            ),
        },
        {
          key: 'status',
          label: 'Status',
          get: (r) => r.status,
          render: (r) =>
            r.status === 'complete'
              ? h('span', { class: 'status status-good' }, icon('check', 13), 'Complete')
              : h(
                  'span',
                  { class: 'status status-warn' },
                  icon('alert', 13),
                  capitalize(r.status || 'unknown'),
                ),
        },
        {
          key: 'duration',
          label: 'Duration',
          num: true,
          get: (r) => r.duration_s,
          render: (r) => fmtDuration(r.duration_s),
        },
        {
          key: 'queries',
          label: 'Queries',
          num: true,
          get: (r) => r.n_queries,
          render: (r) => fmtInt(r.n_queries),
        },
        {
          key: 'domains',
          label: 'Domains',
          num: true,
          get: (r) => r.n_domains,
          render: (r) => fmtInt(r.n_domains),
        },
        {
          key: 'resolvers',
          label: 'Resolvers',
          cls: 'wrap',
          get: (r) => (r.resolvers || []).length,
          render: (r) =>
            h(
              'span',
              { class: 'res-inline-list' },
              (r.resolvers || []).map((n) =>
                h('span', { class: 'res-inline', title: n }, dot(n), h('span', null, n)),
              ),
            ),
        },
        {
          key: 'best',
          label: 'Best',
          get: (r) => r.best,
          render: (r) =>
            r.best
              ? h('span', { class: 'nowrap' }, dot(r.best), ' ', r.best)
              : h('span', { class: 'muted' }, '—'),
        },
        {
          key: 'best_median',
          label: 'Best median',
          num: true,
          get: (r) => r.best_median,
          render: (r) => fmtMsU(r.best_median),
        },
        {
          key: 'actions',
          label: 'Files',
          sortable: false,
          render: (r) =>
            h(
              'span',
              { class: 'row-actions' },
              h('button', { type: 'button', class: 'btn btn-sm', onClick: () => viewRun(r.id) }, 'View'),
              h(
                'a',
                {
                  class: 'btn btn-sm btn-ghost',
                  href: `/api/runs/${encodeURIComponent(r.id)}/csv`,
                  download: `dns-bench-${r.id}.csv`,
                  title: 'Download raw results as CSV',
                },
                icon('download', 14),
                'CSV',
              ),
            ),
        },
      ],
      state.runs,
      {
        defaultSort: { key: 'date', dir: -1 },
        caption: 'Saved benchmark runs',
        tableClass: 'history-table',
        rowAttrs: (r) => ({
          class: `is-clickable${r.id === current ? ' is-current' : ''}`,
          onClick: (e) => {
            if (!e.target.closest('a, button')) viewRun(r.id);
          },
        }),
      },
    );
    return h(
      'div',
      { class: 'view' },
      card(
        {
          title: 'Saved runs',
          sub: `${plural(state.runs.length, 'run')} saved. Every run is kept in the runs/ folder as JSON and a text report; nothing is ever deleted.`,
          right: allBtn,
        },
        table,
      ),
    );
  }

  // ================================================================ views: settings
  // The server owns every rule: it normalises and validates the draft (POST /api/config/validate,
  // debounced), and a save is checked again there. The page keeps the form's text as typed.

  function draftFromConfig(cfg) {
    // GET /api/config returns even an invalid (hand-edited) config, so tolerate odd shapes. Rows stay
    // in the same order, so the server's error paths (resolvers[i]) line up with them.
    const text = (v, sep) => (Array.isArray(v) ? v.join(sep) : v == null ? '' : String(v));
    const saved = cfg?.settings && typeof cfg.settings === 'object' ? cfg.settings : {};
    return {
      resolvers: (Array.isArray(cfg?.resolvers) ? cfg.resolvers : []).map((r) => ({
        name: typeof r?.name === 'string' ? r.name : text(r?.name, ''),
        serversText: text(r?.servers, ', '),
        enabled: r?.enabled !== false,
      })),
      domainsText: text(cfg?.domains, '\n'),
      settings: Object.fromEntries(
        settingFields().map((f) => {
          const v = settingOf(saved, f.key);
          return [f.key, f.type === 'int' ? text(v, '') : v];
        }),
      ),
    };
  }
  /** The draft as the server takes it: raw text for servers, domains and numbers. */
  function draftToRaw(d) {
    return {
      resolvers: d.resolvers.map((r) => ({ name: r.name, servers: r.serversText, enabled: !!r.enabled })),
      domains: d.domainsText,
      settings: { ...d.settings },
    };
  }
  /** Apply a {config, errors, estimate} response from the config endpoints. */
  function setConfig(res) {
    state.config = res.config;
    state.configErrors = Array.isArray(res.errors) ? res.errors : [];
    state.estimate = res.estimate || null;
    state.runEstimate = null;
    // Enabling or disabling a resolver changes what the combined view may recommend.
    if (
      state.dataset &&
      state.dataset.kind === 'aggregate' &&
      state.aggCache &&
      state.aggCache.stamp !== aggStamp()
    ) {
      state.aggCache = null;
      selectDataset('all', { rerender: 'auto' });
    }
  }
  function computeDirty() {
    if (!state.draft || !state.config) return false;
    return JSON.stringify(state.draft) !== JSON.stringify(draftFromConfig(state.config));
  }

  /** Where a server error belongs in the form, from its path (see README "JSON API"). */
  function errorTarget(e) {
    const path = String(e?.path ?? '');
    const msg = String(e?.message ?? e);
    let m = /^resolvers\[(\d+)\](?:\.(name|servers))?/.exec(path);
    if (m) return { scope: 'resolvers', index: Number(m[1]), part: m[2] || 'name', msg };
    if (path === 'resolvers') return { scope: 'resolvers', msg };
    if (path === 'domains' || path.startsWith('domains[')) return { scope: 'domains', msg };
    m = /^settings\.(\w+)$/.exec(path);
    if (m) {
      const f = settingFields().find((x) => x.key === m[1]);
      // A range error reads better with the field's own name than as settings.<key>.
      if (f && e.code === 'out_of_range')
        return {
          scope: 'settings',
          key: f.key,
          msg: `${f.label} must be a whole number from ${fmtInt(f.min)} to ${fmtInt(f.max)}.`,
        };
      return { scope: 'settings', key: m[1], msg };
    }
    return { scope: 'general', msg };
  }
  /** A field in the Settings form, for the error summary to take the keyboard to. */
  function errorField(e) {
    if (e.scope === 'resolvers' && Number.isInteger(e.index))
      return `.res-row[data-row="${e.index}"] input[data-field="${e.part === 'servers' ? 'servers' : 'name'}"]`;
    if (e.scope === 'domains') return '#domains-input';
    if (e.scope === 'settings' && e.key) return `#set-${CSS.escape(e.key)}`;
    return null;
  }
  function groupErrors(list) {
    const g = {
      all: Array.from(new Set(list.map((e) => e.msg))),
      fields: {}, // message -> selector of the field it is about
      resolvers: {},
      resolversGeneral: [],
      domains: [],
      settings: {},
      general: [],
    };
    for (const e of list) {
      const field = errorField(e);
      if (field && !g.fields[e.msg]) g.fields[e.msg] = field;
      if (e.scope === 'resolvers') {
        if (Number.isInteger(e.index)) {
          g.resolvers[e.index] ||= [];
          const arr = g.resolvers[e.index];
          if (!arr.includes(e.msg)) arr.push(e.msg);
        } else g.resolversGeneral.push(e.msg);
      } else if (e.scope === 'domains') g.domains.push(e.msg);
      else if (e.scope === 'settings' && e.key) {
        g.settings[e.key] ||= [];
        g.settings[e.key].push(e.msg);
      } else g.general.push(e.msg);
    }
    return g;
  }

  let draftCheckSeq = 0;
  const checkDraft = debounce(async () => {
    const d = state.draft;
    if (!d) return;
    const seq = ++draftCheckSeq;
    try {
      const res = await api('/api/config/validate', { method: 'POST', body: draftToRaw(d) });
      if (seq !== draftCheckSeq || state.draft !== d) return; // a newer edit or another draft
      state.draftCheck = res;
      if (viewHooks.settings) viewHooks.settings.refresh();
    } catch (err) {
      console.warn('draft check unavailable:', err); // the form keeps the last check; Save re-validates
    }
  }, 250);

  function onDraftChange() {
    state.dirty = computeDirty();
    checkDraft();
    if (viewHooks.settings) viewHooks.settings.refresh();
    scheduleRender({ tabs: true });
  }
  function discardDraft() {
    state.draft = null;
    state.draftCheck = null;
    state.dirty = false;
    state.saveErrors = null;
    state.configInvalid = false;
    scheduleRender({ tabs: true });
  }
  /**
   * Forget the errors of a field the user has just edited (scope 'settings' + key,
   * 'domains', 'resolver' + index, or 'resolvers' for all rows), so a later redraw
   * does not bring back an error that no longer applies.
   */
  function pruneSaveErrors(scope, key) {
    const se = state.saveErrors;
    if (!se) return;
    if (scope === 'settings') delete se.settings[key];
    else if (scope === 'domains') se.domains = [];
    else if (scope === 'resolver') delete se.resolvers[key];
    else if (scope === 'resolvers') se.resolvers = {};
    const live = new Set([
      ...se.general,
      ...se.resolversGeneral,
      ...se.domains,
      ...Object.values(se.resolvers).flat(),
      ...Object.values(se.settings).flat(),
    ]);
    se.all = se.all.filter((m) => live.has(m));
    if (!se.all.length) state.saveErrors = null;
  }

  async function saveSettings() {
    if (!state.draft || state.saving) return;
    state.saving = true;
    if (viewHooks.settings) viewHooks.settings.refresh();
    try {
      setConfig(await api('/api/config', { method: 'PUT', body: draftToRaw(state.draft) }));
      if (state.info) state.info.config_exists = true;
      discardDraft();
      resetColors();
      state.roundsTouched = false;
      clearBanners('Could not start the benchmark');
      toast('Settings saved.');
      scheduleRender({ view: true, controls: true });
    } catch (e) {
      if (e.status === 400 && e.details && e.details.length) {
        state.saveErrors = groupErrors(e.details.map(errorTarget));
        scheduleRender({ view: true }, focusFirstError);
      } else showBanner(`Could not save settings: ${e.message}`);
    } finally {
      state.saving = false;
      if (viewHooks.settings) viewHooks.settings.refresh();
    }
  }
  /** After a failed save (once the form is redrawn): move the keyboard and the screen to the problems. */
  function focusFirstError() {
    const el = document.querySelector('.error-summary') || document.querySelector('.has-error');
    if (!el) return;
    el.focus({ preventScroll: true });
    el.scrollIntoView({ behavior: scrollBehavior(), block: 'center' });
  }
  async function revertSettings() {
    if (state.dirty && !window.confirm('Discard your unsaved changes?')) return;
    try {
      setConfig(await api('/api/config'));
    } catch (e) {
      showBanner(`Could not reload the configuration: ${e.message}`);
    }
    discardDraft();
    scheduleRender({ view: true, controls: true });
  }
  async function resetSettings() {
    if (
      !window.confirm(
        "Reset resolvers, domains and settings to the built-in defaults?\n\nThis overwrites config.json and adds this computer's own resolvers as 'System'. Your saved runs are not touched.",
      )
    )
      return;
    try {
      setConfig(await api('/api/config/reset', { method: 'POST', body: {} }));
      if (state.info) state.info.config_exists = true;
      discardDraft();
      resetColors();
      state.roundsTouched = false;
      clearBanners('Could not start the benchmark');
      toast('Configuration reset to defaults.');
      scheduleRender({ view: true, controls: true });
    } catch (e) {
      showBanner(`Could not reset the configuration: ${e.message}`);
    }
  }

  /** Start editing Settings: a draft of the saved config, showing its problems if it has any. */
  function ensureDraft() {
    if (state.draft || !state.config || !state.schema) return;
    state.draft = draftFromConfig(state.config);
    state.draftCheck = {
      config: state.config,
      errors: state.configErrors,
      estimate: state.estimate,
      duplicate_domains: 0,
    };
    // A hand-edited config.json can be invalid (the server still returns it so it
    // can be fixed here). Show its problems inline and let the user save a fix.
    state.configInvalid = state.configErrors.length > 0;
    if (state.configInvalid && !state.saveErrors)
      state.saveErrors = { ...groupErrors(state.configErrors.map(errorTarget)), fromLoad: true };
  }

  function viewSettings() {
    if (!state.config || !state.schema) {
      return emptyPanel(
        'Configuration unavailable',
        state.configError || 'The configuration could not be loaded.',
        h(
          'button',
          { type: 'button', class: 'btn', onClick: () => bootstrap() },
          icon('refresh', 14),
          'Try again',
        ),
      );
    }
    const d = state.draft;
    const limits = state.schema.limits || {};
    const presets = Array.isArray(state.schema.presets) ? state.schema.presets : [];
    const errs = state.saveErrors || groupErrors([]);
    // Editing a field clears its error both on screen and in state.saveErrors;
    // once none are left, the summary at the top goes too.
    let summaryEl = null;
    const dropSummaryIfFixed = () => {
      if (!state.saveErrors && summaryEl) {
        summaryEl.remove();
        summaryEl = null;
      }
    };
    const clearErr = (el, scope, key) => {
      pruneSaveErrors(scope, key);
      dropSummaryIfFixed();
      const box = el?.closest('.has-error');
      if (!box) return;
      box.classList.remove('has-error');
      for (const m of box.querySelectorAll(':scope > .field-error')) m.remove();
      for (const input of box.querySelectorAll('[aria-invalid]')) {
        input.removeAttribute('aria-invalid');
        const ids = (input.getAttribute('aria-describedby') || '')
          .split(' ')
          .filter((x) => !x.endsWith('-err'));
        if (ids.length) input.setAttribute('aria-describedby', ids.join(' '));
        else input.removeAttribute('aria-describedby');
      }
    };

    // ---- resolvers
    const resList = h('div', { class: 'res-list' });
    const resHeader = h(
      'div',
      { class: 'res-row res-row-head', 'aria-hidden': 'true' },
      h('span', null, 'On'),
      h('span', null, ''),
      h('span', null, 'Name'),
      h('span', null, 'Server IPs (comma or space separated)'),
      h('span', null, ''),
    );
    function resolverRow(r, i) {
      const rowErrs = errs.resolvers[i] || [];
      const errId = `res-${i}-err`;
      const invalid = rowErrs.length ? { 'aria-invalid': 'true', 'aria-describedby': errId } : {};
      const row = h('div', {
        class: `res-row${r.enabled ? '' : ' is-off'}${rowErrs.length ? ' has-error' : ''}`,
        dataset: { row: String(i) },
      });
      const en = h('input', {
        type: 'checkbox',
        checked: r.enabled,
        'aria-label': `Include ${r.name || `resolver ${i + 1}`} in benchmarks`,
      });
      en.addEventListener('change', () => {
        r.enabled = en.checked;
        row.classList.toggle('is-off', !r.enabled);
        onDraftChange();
      });
      const nameIn = h('input', {
        class: 'input',
        type: 'text',
        value: r.name,
        maxlength: String(limits.name_length),
        placeholder: 'Name',
        'aria-label': `Resolver ${i + 1} name`,
        autocomplete: 'off',
        spellcheck: 'false',
        dataset: { field: 'name' },
        ...invalid,
      });
      // The resolver's colour everywhere else; neutral while a new name hasn't been saved.
      const dotEl = h('span', {
        class: 'dot',
        style: { background: knownColor(r.name.trim()) },
        'aria-hidden': 'true',
      });
      nameIn.addEventListener('input', () => {
        r.name = nameIn.value;
        dotEl.style.background = knownColor(r.name.trim());
        clearErr(nameIn, 'resolver', i);
        onDraftChange();
      });
      const servIn = h('input', {
        class: 'input mono',
        type: 'text',
        value: r.serversText,
        placeholder: 'e.g. 1.1.1.1, 1.0.0.1',
        'aria-label': `Resolver ${i + 1} server IPs`,
        autocomplete: 'off',
        spellcheck: 'false',
        autocapitalize: 'off',
        dataset: { field: 'servers' },
        ...invalid,
      });
      servIn.addEventListener('input', () => {
        r.serversText = servIn.value;
        clearErr(servIn, 'resolver', i);
        onDraftChange();
      });
      const rm = h(
        'button',
        {
          type: 'button',
          class: 'btn btn-icon btn-ghost',
          'aria-label': `Remove ${r.name || `resolver ${i + 1}`}`,
          title: 'Remove',
        },
        icon('x'),
      );
      rm.addEventListener('click', () => {
        d.resolvers.splice(i, 1);
        pruneSaveErrors('resolvers'); // row indices shift, so per-row errors no longer line up
        dropSummaryIfFixed();
        drawResolvers();
        onDraftChange();
      });
      row.append(h('label', { class: 'res-on' }, en), dotEl, nameIn, servIn, rm);
      if (rowErrs.length)
        row.appendChild(
          h(
            'div',
            { class: 'field-error res-error', id: errId },
            rowErrs.map((m) => h('div', null, m)),
          ),
        );
      return row;
    }
    function drawResolvers() {
      setKids(resList, resHeader, ...d.resolvers.map(resolverRow));
      setKids(
        presetSel,
        h('option', { value: '' }, 'Add a preset…'),
        presets.map((p) => {
          const exists = d.resolvers.some((r) => r.name.trim().toLowerCase() === p.name.toLowerCase());
          return h(
            'option',
            { value: p.name, disabled: exists },
            `${p.name} — ${p.servers.join(', ')}${exists ? ' (added)' : ''}`,
          );
        }),
      );
      presetSel.value = '';
    }
    const addBtn = h(
      'button',
      { type: 'button', class: 'btn btn-sm' },
      icon('plus', 14),
      h('span', null, 'Add resolver'),
    );
    addBtn.addEventListener('click', () => {
      d.resolvers.push({ name: '', serversText: '', enabled: true });
      drawResolvers();
      onDraftChange();
      const inputs = resList.querySelectorAll('.res-row:last-child input[type="text"]');
      if (inputs[0]) inputs[0].focus();
    });
    // The server finds this computer's resolvers and leaves out any that another row already has.
    const sysBtn = h(
      'button',
      {
        type: 'button',
        class: 'btn btn-sm',
        title: "Add or update a 'System' row with this computer's own resolvers",
      },
      icon('plus', 14),
      h('span', null, 'Add system resolvers'),
    );
    sysBtn.addEventListener('click', async () => {
      sysBtn.disabled = true;
      try {
        const res = await api('/api/config/system-resolver', {
          method: 'POST',
          body: { resolvers: draftToRaw(d).resolvers },
        });
        if (state.draft !== d) return; // the draft was saved or discarded meanwhile
        if (res?.resolver) {
          const serversText = res.resolver.servers.join(', ');
          const existing = d.resolvers.find((r) => r.name.trim().toLowerCase() === 'system');
          if (existing?.serversText === serversText) {
            toast(`${res.message} The System row already has them.`, 'info');
            return;
          }
          if (existing) existing.serversText = serversText;
          else d.resolvers.push({ name: res.resolver.name, serversText, enabled: true });
          pruneSaveErrors('resolvers');
          dropSummaryIfFixed();
          drawResolvers();
          onDraftChange();
          toast(`${res.message} Save to keep it.`);
        } else toast(res?.message || 'No system resolvers found.', 'info');
      } catch (e) {
        showBanner(`Could not look up this computer's resolvers: ${e.message}`);
      } finally {
        sysBtn.disabled = false;
      }
    });
    const presetSel = h('select', { class: 'input select select-sm', 'aria-label': 'Add a preset resolver' });
    presetSel.addEventListener('change', () => {
      const p = presets.find((x) => x.name === presetSel.value);
      if (!p) return;
      d.resolvers.push({ name: p.name, serversText: p.servers.join(', '), enabled: true });
      drawResolvers();
      onDraftChange();
    });
    drawResolvers();
    const resCard = card(
      {
        title: 'Resolvers',
        sub: 'Each resolver is a DNS provider with one to four server addresses. Unticked resolvers stay in the list but are skipped.',
        right: h('div', { class: 'card-tools' }, sysBtn, presetSel, addBtn),
      },
      errs.resolversGeneral.length
        ? h(
            'div',
            { class: 'field-error' },
            errs.resolversGeneral.map((m) => h('div', null, m)),
          )
        : null,
      resList,
    );

    // ---- domains
    const ta = h('textarea', {
      class: 'input textarea mono',
      rows: '16',
      spellcheck: 'false',
      autocapitalize: 'off',
      autocomplete: 'off',
      id: 'domains-input',
      'aria-label': 'Domains, one per line',
      'aria-describedby': errs.domains.length ? 'domains-info domains-err' : 'domains-info',
      'aria-invalid': errs.domains.length ? 'true' : null,
    });
    ta.value = d.domainsText;
    const info = h('div', { class: 'domain-info', id: 'domains-info' });
    // From the server's last check of the draft (it lags the typing by a moment).
    const updateDomainInfo = () => {
      const check = state.draftCheck || {};
      const list = Array.isArray(check.config?.domains) ? check.config.domains : [];
      const errors = Array.isArray(check.errors) ? check.errors : [];
      const invalid = errors
        .map((e) => /^domains\[(\d+)\]$/.exec(String(e.path)))
        .filter(Boolean)
        .map((m) => String(list[Number(m[1])]));
      const more = errors.some((e) => e.path === 'domains' && e.code === 'more_errors');
      const dupes = isNum(check.duplicate_domains) ? check.duplicate_domains : 0;
      setKids(
        info,
        h(
          'strong',
          { class: list.length > limits.domains ? 'text-critical' : '' },
          plural(list.length, 'domain'),
        ),
        h('span', { class: 'muted' }, ` of ${fmtInt(limits.domains)} max`),
        dupes ? h('span', { class: 'muted' }, ` · ${plural(dupes, 'duplicate')} will be removed`) : null,
        invalid.length
          ? h(
              'span',
              { class: 'text-critical' },
              ` · ${fmtInt(invalid.length)}${more ? '+' : ''} invalid: ${invalid.slice(0, 3).join(', ')}${invalid.length > 3 || more ? '…' : ''}`,
            )
          : null,
      );
    };
    ta.addEventListener('input', () => {
      d.domainsText = ta.value;
      clearErr(ta, 'domains');
      updateDomainInfo();
      onDraftChange();
    });
    const resetDomains = h(
      'button',
      { type: 'button', class: 'btn btn-sm', disabled: !state.schema.defaults?.domains },
      icon('refresh', 14),
      h('span', null, 'Reset domains to defaults'),
    );
    resetDomains.addEventListener('click', () => {
      if (!state.schema.defaults?.domains) return;
      d.domainsText = state.schema.defaults.domains.join('\n');
      ta.value = d.domainsText;
      clearErr(ta, 'domains');
      updateDomainInfo();
      onDraftChange();
    });
    updateDomainInfo();
    const domCard = card(
      {
        title: 'Domains',
        sub: 'One per line. Duplicates, blank lines and trailing dots are cleaned up when you save.',
        right: resetDomains,
      },
      h(
        'div',
        { class: `field${errs.domains.length ? ' has-error' : ''}` },
        ta,
        info,
        errs.domains.length
          ? h(
              'div',
              { class: 'field-error', id: 'domains-err' },
              errs.domains.map((m) => h('div', null, m)),
            )
          : null,
      ),
    );

    // ---- benchmark settings
    const fields = settingFields().map((f) => {
      const id = `set-${f.key}`;
      const fe = errs.settings[f.key] || [];
      let input;
      if (f.type === 'choice') {
        input = h(
          'select',
          { class: 'input select', id },
          (f.choices || []).map((v) => h('option', { value: v }, f.options?.[v] || v)),
        );
        input.value = d.settings[f.key];
        input.addEventListener('change', () => {
          d.settings[f.key] = input.value;
          clearErr(input, 'settings', f.key);
          onDraftChange();
        });
      } else if (f.type === 'bool') {
        input = h('input', { type: 'checkbox', id, checked: !!d.settings[f.key] });
        input.addEventListener('change', () => {
          d.settings[f.key] = input.checked;
          clearErr(input, 'settings', f.key);
          onDraftChange();
        });
      } else {
        input = h('input', {
          class: 'input input-num',
          type: 'number',
          id,
          min: String(f.min),
          max: String(f.max),
          step: String(f.step || 1),
          inputmode: 'numeric',
          value: String(d.settings[f.key]),
          'aria-describedby': `${id}-help`,
        });
        input.addEventListener('input', () => {
          d.settings[f.key] = input.value;
          clearErr(input, 'settings', f.key);
          onDraftChange();
        });
      }
      input.setAttribute('aria-describedby', fe.length ? `${id}-help ${id}-err` : `${id}-help`);
      if (fe.length) input.setAttribute('aria-invalid', 'true');
      const errNode = fe.length
        ? h(
            'div',
            { class: 'field-error', id: `${id}-err` },
            fe.map((m) => h('div', null, m)),
          )
        : null;
      if (f.type === 'bool') {
        return h(
          'div',
          { class: `field field-check${fe.length ? ' has-error' : ''}` },
          h('label', { class: 'check-label', for: id }, input, h('span', null, f.label)),
          h('div', { class: 'help', id: `${id}-help` }, f.help),
          errNode,
        );
      }
      return h(
        'div',
        { class: `field${fe.length ? ' has-error' : ''}` },
        h(
          'label',
          { class: 'field-label', for: id },
          h('span', null, f.label),
          f.type === 'int'
            ? h(
                'span',
                { class: 'field-range' },
                `${fmtInt(f.min)}–${fmtInt(f.max)}${f.unit ? ` ${f.unit}` : ''}`,
              )
            : null,
        ),
        f.unit ? h('div', { class: 'input-unit' }, input, h('span', { class: 'unit' }, f.unit)) : input,
        h('div', { class: 'help', id: `${id}-help` }, f.help),
        errNode,
      );
    });

    const estBox = h('div', { class: 'estimate' });
    const updateEstimate = () => {
      const est = state.draftCheck?.estimate || state.estimate;
      if (!est) return;
      const row = (label, value, sub) =>
        h(
          'div',
          { class: 'est-row' },
          h('div', { class: 'est-label' }, label),
          h('div', { class: 'est-value' }, value),
          sub ? h('div', { class: 'est-sub' }, sub) : null,
        );
      setKids(
        estBox,
        row(
          'Estimated duration',
          `≈ ${fmtDuration(est.est_seconds)}`,
          est.worst_seconds > est.est_seconds * 1.5
            ? `Up to ${fmtDuration(est.worst_seconds)} if servers keep timing out`
            : null,
        ),
        row(
          'Queries per run',
          fmtInt(est.queries),
          `${plural(est.resolvers, 'resolver')} · ${plural(est.servers, 'server')} · ${plural(est.domains, 'domain')} × ${plural(est.rounds, 'round')}`,
        ),
        row(
          'Load on each server',
          `≤ ${fmtQps(est.max_qps_per_server)} queries/s`,
          'Never more than one query in flight per server',
        ),
        row(
          'Total load',
          `≤ ${fmtQps(est.max_qps_total)} queries/s`,
          `${plural(est.servers, 'server')} at a time`,
        ),
      );
    };
    updateEstimate();
    const setCard = card(
      {
        title: 'Benchmark settings',
        sub: 'Defaults are safe for public resolvers. Shorter intervals finish sooner but put more load on each server.',
      },
      estBox,
      h('div', { class: 'fields' }, fields),
    );

    // ---- action bar
    const status = h('div', { class: 'save-status', role: 'status' });
    const saveBtn = h(
      'button',
      { type: 'button', class: 'btn btn-primary' },
      icon('check', 14),
      h('span', null, 'Save changes'),
    );
    saveBtn.addEventListener('click', saveSettings);
    const revertBtn = h('button', { type: 'button', class: 'btn' }, 'Revert');
    revertBtn.addEventListener('click', revertSettings);
    const resetBtn = h('button', { type: 'button', class: 'btn btn-ghost btn-danger' }, 'Reset to defaults');
    resetBtn.addEventListener('click', resetSettings);
    const refresh = () => {
      if (state.saving)
        setKids(status, h('span', { class: 'spinner spinner-sm' }), h('span', null, 'Saving…'));
      else if (state.dirty)
        setKids(
          status,
          h('span', { class: 'dirty-dot', 'aria-hidden': 'true' }),
          h('span', null, 'Unsaved changes'),
        );
      else if (state.configInvalid)
        setKids(
          status,
          h('span', { class: 'text-critical' }, icon('alert', 14)),
          h('span', { class: 'text-critical' }, 'The saved configuration has problems'),
        );
      else setKids(status, icon('check', 14), h('span', { class: 'muted' }, 'All changes saved'));
      // An invalid saved config stays savable even before any edit (Save then re-validates).
      saveBtn.disabled = (!state.dirty && !state.configInvalid) || state.saving;
      revertBtn.disabled = !state.dirty || state.saving;
      resetBtn.disabled = state.saving;
      updateEstimate();
      updateDomainInfo();
    };
    viewHooks.settings = { refresh };
    refresh();

    summaryEl = state.saveErrors?.all.length
      ? h(
          'div',
          { class: 'error-summary', role: 'alert', tabindex: '-1' },
          h(
            'div',
            { class: 'error-summary-title' },
            icon('alert', 16),
            state.saveErrors.fromLoad
              ? `The saved configuration has ${plural(state.saveErrors.all.length, 'problem')} to fix. Benchmarks cannot run until it is fixed and saved.`
              : `Not saved: ${plural(state.saveErrors.all.length, 'problem')} to fix`,
          ),
          h(
            'ul',
            null,
            state.saveErrors.all.map((m) => {
              const field = state.saveErrors.fields?.[m];
              if (!field) return h('li', null, m);
              const go = h('button', { type: 'button', class: 'link-btn' }, m);
              go.addEventListener('click', () => document.querySelector(field)?.focus());
              return h('li', null, go);
            }),
          ),
        )
      : null;

    const files = state.info;
    const where =
      files && typeof files.config_path === 'string'
        ? h(
            'p',
            { class: 'muted small settings-paths' },
            `Settings are saved in ${files.config_path}${files.config_exists ? '' : ' (not created yet)'}. Runs are saved in ${files.runs_dir}.`,
          )
        : null;
    return h(
      'div',
      { class: 'view settings-view' },
      summaryEl,
      resCard,
      h('div', { class: 'grid grid-settings' }, domCard, setCard),
      where,
      h(
        'div',
        { class: 'action-bar' },
        status,
        h('div', { class: 'action-buttons' }, resetBtn, revertBtn, saveBtn),
      ),
    );
  }

  // ================================================================ empty & shell
  function emptyRunsPanel() {
    const est = state.estimate;
    const btn = h(
      'button',
      {
        type: 'button',
        class: 'btn btn-primary btn-lg',
        'data-run-button': '',
        disabled: !!state.job.running || !state.config,
      },
      icon('play', 16),
      h('span', null, 'Run your first benchmark'),
    );
    btn.addEventListener('click', startRun);
    return h(
      'section',
      { class: 'card empty empty-hero' },
      h('div', { class: 'empty-art', 'aria-hidden': 'true' }, emptyArt()),
      h('h2', { class: 'empty-title' }, 'No results yet'),
      h(
        'p',
        { class: 'empty-text' },
        state.config && est
          ? `DNS Bench will look up ${plural(est.domains, 'domain')} on ${plural(est.resolvers, 'resolver')} (${plural(est.servers, 'server')}). That takes about ${fmtDuration(est.est_seconds)}, and no server ever gets more than ${fmtQps(est.max_qps_per_server)} queries per second.`
          : 'Waiting for the configuration…',
      ),
      btn,
      h('p', { class: 'muted small' }, 'Every run is saved and shows up under History.'),
    );
  }
  function emptyArt() {
    const svg = s('svg', { viewBox: '0 0 120 72', width: 120, height: 72 });
    const bars = [
      [10, 44, 'var(--s1)'],
      [22, 30, 'var(--s2)'],
      [34, 58, 'var(--s3)'],
      [46, 20, 'var(--s4)'],
    ];
    for (const [y, w, c] of bars) {
      svg.appendChild(s('path', { d: hbarPath(20, y, w + 30, 8), style: { fill: c } }));
    }
    svg.appendChild(s('line', { x1: 20.5, x2: 20.5, y1: 4, y2: 64, class: 'axis-line' }));
    return svg;
  }

  function renderDatasetBar() {
    const bar = els.datasetBar;
    const show = DATA_TABS.has(state.route.tab) && state.runs.length > 0;
    bar.hidden = !show;
    if (!show) {
      setKids(bar);
      return;
    }
    const sel = h('select', { class: 'input select', id: 'dataset-select' });
    sel.appendChild(h('option', { value: 'latest' }, `Latest run — ${fmtDate(state.runs[0].started_at)}`));
    sel.appendChild(h('option', { value: 'all' }, `All runs combined (${fmtInt(state.runs.length)})`));
    const og = h('optgroup', { label: 'Individual runs' });
    for (const r of state.runs) {
      og.appendChild(
        h(
          'option',
          { value: r.id },
          `${fmtDate(r.started_at)} · ${fmtInt(r.n_queries)} queries${r.status && r.status !== 'complete' ? ` · ${r.status}` : ''}`,
        ),
      );
    }
    sel.appendChild(og);
    sel.value = state.datasetKey;
    if (sel.value !== state.datasetKey) sel.value = 'latest';
    sel.addEventListener('change', () => selectDataset(sel.value));
    const ds = state.dataset;
    const meta = [];
    if (ds && ds.kind === 'run' && ds.run) {
      const run = ds.run;
      meta.push(`${fmtInt(Array.isArray(run.results) ? run.results.length : ds.summary.overall?.n)} queries`);
      meta.push(plural(resolverNames(ds.summary).length, 'resolver'));
      meta.push(plural(domainNames(ds.summary).length, 'domain'));
      meta.push(`took ${fmtDuration(run.duration_s)}`);
      if (run.host) meta.push(`on ${run.host}`);
    } else if (ds && ds.kind === 'aggregate') {
      meta.push(plural(ds.runIds.length, 'run'));
      if (ds.summary.overall) meta.push(`${fmtInt(ds.summary.overall.n)} queries`);
      meta.push(plural(resolverNames(ds.summary).length, 'resolver'));
      meta.push(plural(domainNames(ds.summary).length, 'domain'));
    }
    const cancelled = ds && ds.kind === 'run' && ds.run?.status && ds.run.status !== 'complete';
    setKids(
      bar,
      h('label', { class: 'dataset-label', for: 'dataset-select' }, 'Showing'),
      sel,
      h(
        'div',
        { class: 'dataset-meta' },
        cancelled
          ? h(
              'span',
              { class: 'status status-warn' },
              icon('alert', 13),
              `${capitalize(ds.run.status)} run — partial results`,
            )
          : null,
        meta.map((m, i) => [
          i || cancelled ? h('span', { class: 'est-sep', 'aria-hidden': 'true' }, '·') : null,
          h('span', null, m),
        ]),
      ),
      ds && ds.kind === 'run'
        ? h(
            'a',
            {
              class: 'btn btn-sm btn-ghost dataset-csv',
              href: `/api/runs/${encodeURIComponent(ds.id)}/csv`,
              download: `dns-bench-${ds.id}.csv`,
            },
            icon('download', 14),
            'CSV',
          )
        : null,
    );
  }

  function updateTabs() {
    for (const a of els.tabs.querySelectorAll('[role="tab"]')) {
      const on = a.dataset.tab === state.route.tab;
      a.setAttribute('aria-selected', on ? 'true' : 'false');
      a.tabIndex = on ? 0 : -1;
      a.classList.toggle('is-dirty', a.dataset.tab === 'settings' && state.dirty);
    }
    const tab = TABS.find((t) => t.id === state.route.tab);
    document.title = tab && tab.id !== 'overview' ? `${tab.label} · DNS Bench` : 'DNS Bench';
  }

  /** Remember the focused form control in the view so a redraw can put the caret back. */
  function captureFocus() {
    const el = document.activeElement;
    if (!el || !els.main.contains(el) || !el.matches('input, textarea, select')) return null;
    const label = el.getAttribute('aria-label');
    let sel = null;
    try {
      if (el.id) sel = `#${CSS.escape(el.id)}`;
      else if (label) sel = `${el.tagName.toLowerCase()}[aria-label="${CSS.escape(label)}"]`;
    } catch (_) {
      return null;
    }
    if (!sel) return null;
    let range = null;
    try {
      if (typeof el.selectionStart === 'number')
        range = [el.selectionStart, el.selectionEnd, el.selectionDirection];
    } catch (_) {
      /* not a text control */
    }
    return { sel, tab: state.route.tab, range, scrollTop: el.scrollTop };
  }
  function restoreFocus(f) {
    if (
      !f ||
      f.tab !== state.route.tab ||
      (document.activeElement && document.activeElement !== document.body)
    )
      return;
    let el = null;
    try {
      el = els.main.querySelector(f.sel);
    } catch (_) {
      return;
    }
    if (!el) return;
    el.focus({ preventScroll: true });
    if (f.range) {
      try {
        el.setSelectionRange(f.range[0], f.range[1], f.range[2] || 'none');
      } catch (_) {
        /* not a text control */
      }
    }
    el.scrollTop = f.scrollTop;
  }

  // ================================================================ drawing
  // Anything that changes what is on screen asks for it here. Requests made in the same tick are drawn
  // once, in the next animation frame, in a fixed order: the view (with the tabs and the dataset bar),
  // the progress card, the run controls, then any `after` callbacks (focus, scrolling) (FE-1).
  const pendingDraw = { view: false, tabs: false, progress: false, controls: false, after: [] };
  let drawFrame = 0;
  function scheduleRender(parts = { view: true }, after) {
    for (const k of ['view', 'tabs', 'progress', 'controls']) if (parts[k]) pendingDraw[k] = true;
    if (after) pendingDraw.after.push(after);
    if (!drawFrame) drawFrame = requestAnimationFrame(drawNow);
  }
  function drawNow() {
    drawFrame = 0;
    const p = { ...pendingDraw, after: pendingDraw.after.splice(0) };
    pendingDraw.view = pendingDraw.tabs = pendingDraw.progress = pendingDraw.controls = false;
    if (p.view) render();
    else if (p.tabs) {
      updateTabs();
      renderDatasetBar();
    }
    if (p.progress) renderProgress();
    if (p.controls) updateRunControls();
    for (const fn of p.after) fn();
  }

  function render() {
    const focus = captureFocus();
    hideTip();
    updateTabs();
    renderDatasetBar();
    if (chartRO) chartRO.disconnect();
    pendingCharts = [];
    viewHooks.update = null;
    viewHooks.trendSlot = null;
    if (state.route.tab !== 'settings') viewHooks.settings = null;
    let view;
    try {
      view = renderView();
    } catch (err) {
      console.error(err);
      view = emptyPanel(
        'Something went wrong',
        `This view could not be drawn: ${err?.message ? err.message : err}`,
      );
    }
    els.view.setAttribute('aria-labelledby', `tab-${state.route.tab}`);
    setKids(els.view, view);
    flushCharts();
    restoreFocus(focus);
    applyFocusTarget();
  }

  /** Ask for `sel` to be focused once the current tab's view is next drawn. */
  function focusAfterRender(sel) {
    state.focusAfterRender = { sel, tab: state.route.tab };
  }
  /** Focus what the last action asked for, now that the view is drawn, if it is still that tab. */
  function applyFocusTarget() {
    const target = state.focusAfterRender;
    state.focusAfterRender = null;
    if (!target || target.tab !== state.route.tab) return;
    try {
      els.main.querySelector(target.sel)?.focus();
    } catch (err) {
      console.warn('focus target not found:', target.sel, err); // a stale selector: leave focus where it is
    }
  }

  function renderView() {
    const tab = state.route.tab;
    if (!state.bootstrapped) return loadingPanel('Loading DNS Bench…');
    // The actions a view needs run first; the view builders below only read state.
    if (tab === 'settings') {
      ensureDraft();
      return viewSettings();
    }
    if (tab === 'history') return viewHistory();
    if (!state.runs.length) return emptyRunsPanel();
    const ds = state.dataset;
    if (!ds) {
      if (state.datasetError) {
        return emptyPanel(
          'Could not load results',
          state.datasetError,
          h(
            'button',
            { type: 'button', class: 'btn', onClick: () => selectDataset('latest') },
            icon('refresh', 14),
            'Try again',
          ),
        );
      }
      return loadingPanel('Loading results…');
    }
    if (tab === 'overview') return viewOverview(ds);
    if (tab === 'resolver') {
      state.ui.lastResolver = selectedResolver(ds) ?? state.ui.lastResolver;
      return viewResolver(ds);
    }
    return viewDomain(ds);
  }

  // ================================================================ routing
  function parseHash() {
    const raw = location.hash.replace(/^#\/?/, '');
    const slash = raw.indexOf('/');
    const tab = slash < 0 ? raw : raw.slice(0, slash);
    let arg = slash < 0 ? null : raw.slice(slash + 1);
    if (arg) {
      try {
        arg = decodeURIComponent(arg);
      } catch (_) {
        /* keep raw */
      }
    }
    return TAB_IDS.has(tab) ? { tab, arg: arg || null } : { tab: 'overview', arg: null };
  }

  function onHashChange() {
    const next = parseHash();
    const prev = state.route;
    if (prev.tab === next.tab && prev.arg === next.arg) return;
    if (prev.tab === 'settings' && next.tab !== 'settings' && state.dirty) {
      if (!window.confirm('You have unsaved changes in Settings. Leave and discard them?')) {
        history.replaceState(null, '', '#settings');
        return;
      }
      discardDraft();
    }
    state.route = next;
    const byKeys = state.tabKeyNav;
    state.tabKeyNav = false;
    if (prev.tab === next.tab && viewHooks.update && viewHooks.update(next.arg)) {
      scheduleRender({ tabs: true }, applyFocusTarget);
      return;
    }
    scheduleRender({ view: true }, () => {
      if (prev.tab === next.tab) return;
      window.scrollTo(0, 0);
      // A new view: take the keyboard to it, unless the arrow keys are moving along the tabs.
      if (!byKeys && !document.activeElement?.closest?.('#view')) els.view.focus({ preventScroll: true });
      refreshRuns();
    });
  }

  // ================================================================ bootstrap
  async function bootstrap() {
    const [cfgR, schemaR, runsR, stR, infoR] = await Promise.allSettled([
      api('/api/config'),
      api('/api/schema'),
      api('/api/runs'),
      api('/api/status'),
      api('/api/info'),
    ]);
    if (schemaR.status === 'fulfilled' && schemaR.value) {
      state.schema = schemaR.value;
      // Every run is analysed afresh with the server's current analysis; say which one.
      els.footer.textContent = `DNS Bench ${state.schema.version} · analysis v${state.schema.analysis_version} · queries are rate-limited per server so public resolvers are never flooded.`;
      const rounds = settingSchema('rounds');
      if (rounds) {
        els.rounds.min = String(rounds.min);
        els.rounds.max = String(rounds.max);
      }
    } else showBanner(`Could not load the settings' rules: ${schemaR.reason?.message || 'Unknown error'}`);
    if (cfgR.status === 'fulfilled' && cfgR.value?.config) {
      setConfig(cfgR.value);
      state.configError = null;
    } else {
      state.configError = cfgR.reason ? cfgR.reason.message : 'Unknown error';
      showBanner(`Could not load the configuration: ${state.configError}`, 'error', cfgR.reason?.details);
    }
    if (infoR.status === 'fulfilled') state.info = infoR.value;
    if (runsR.status === 'fulfilled') state.runs = Array.isArray(runsR.value?.runs) ? runsR.value.runs : [];
    else showBanner(`Could not load saved runs: ${runsR.reason.message}`);
    state.bootstrapped = true;
    resetColors();
    scheduleRender({ controls: true });
    if (state.runs.length) await selectDataset('latest');
    else scheduleRender();
    if (stR.status === 'fulfilled' && stR.value && stR.value.running) {
      state.job = { ...stR.value, running: true };
      resetLive();
      ingestRecent(stR.value.recent);
      scheduleRender({ progress: true, controls: true });
      schedulePoll(POLL_MS);
    }
  }

  function init() {
    els.main = document.getElementById('main');
    els.view = document.getElementById('view');
    els.tabs = document.getElementById('tabs');
    els.banners = document.getElementById('banners');
    els.progress = document.getElementById('progress');
    els.datasetBar = document.getElementById('dataset-bar');
    els.runBtn = document.getElementById('run-btn');
    els.rounds = document.getElementById('rounds-input');
    els.estimate = document.getElementById('run-estimate');
    els.tooltip = document.getElementById('tooltip');
    els.toasts = document.getElementById('toasts');
    els.footer = document.getElementById('page-footer');
    try {
      const fam = getComputedStyle(document.body).fontFamily;
      if (fam) FONT = fam;
    } catch (_) {
      /* keep default */
    }

    els.runBtn.addEventListener('click', startRun);
    els.rounds.addEventListener('input', () => {
      state.roundsTouched = true;
      scheduleRender({ controls: true });
    });
    els.tabs.addEventListener('keydown', (e) => {
      const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End'];
      if (!keys.includes(e.key)) return;
      const tabs = Array.from(els.tabs.querySelectorAll('[role="tab"]'));
      const i = tabs.indexOf(document.activeElement);
      if (i < 0) return;
      e.preventDefault();
      let j = i;
      if (e.key === 'ArrowLeft') j = (i - 1 + tabs.length) % tabs.length;
      else if (e.key === 'ArrowRight') j = (i + 1) % tabs.length;
      else if (e.key === 'Home') j = 0;
      else j = tabs.length - 1;
      tabs[j].focus();
      state.tabKeyNav = j !== i; // no hash change (and so no route change) when it is the same tab
      location.hash = tabs[j].getAttribute('href');
    });
    window.addEventListener('hashchange', onHashChange);
    window.addEventListener('beforeunload', (e) => {
      if (state.dirty) {
        e.preventDefault();
        e.returnValue = '';
      }
    });
    window.addEventListener('scroll', onScrollTip, { passive: true });
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') refreshRuns();
    });
    window.addEventListener('focus', () => refreshRuns());
    setInterval(() => {
      if (document.visibilityState === 'visible') refreshRuns(true);
    }, 30000);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') hideTip();
    });

    state.route = parseHash();
    if (!location.hash) history.replaceState(null, '', '#overview');
    scheduleRender();
    bootstrap().catch((err) => {
      console.error(err);
      showBanner(`DNS Bench failed to start: ${err?.message ? err.message : err}`);
      state.bootstrapped = true;
      scheduleRender();
    });
  }

  init();
})();
