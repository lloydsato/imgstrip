/* imgstrip web app: scan first, clean on request, prove the result. */
'use strict';

(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  const CATEGORY = {
    metadata: 'Identifying metadata',
    hidden: 'Hidden data',
    color: 'Colour profile',
    structure: 'Structure',
  };
  const CATEGORY_ORDER = ['metadata', 'hidden', 'color', 'structure'];
  const REVEAL_NOUN = {
    location: 'location', camera: 'camera', lens: 'lens', time: 'time taken', serial: 'serial number',
    person: 'a name', software: 'software', preview: 'a hidden image', trailer: 'appended data',
    comment: 'a comment', gps: 'GPS tags',
  };
  const OPTION_DEFAULTS = {
    format: '', quality: '', icc: 'convert',
    deep: false, jpeg_lossless: false, scrub_lsb: false, scrub_dct: false, jpeg_canonical: false, scrub_prnu: false, auto_orient: true, optimize: false, random_name: false,
    c2pa: false, c2pa_date: 'day', c2pa_detail: 'minimal', c2pa_commit: false,
  };
  // Never carried from one launch to the next: signing needs an identity loaded in this very run,
  // and committing to the original is a deliberate act each time.
  const VOLATILE = ['c2pa', 'c2pa_commit'];
  const PIXELS = {
    identical: 'The pixels are identical to the file you received.',
    equivalent: 'The picture is the same, but it was re-encoded, so pixel values are not bit-identical.',
    altered: 'The pixels were deliberately altered to protect the source.',
  };
  const S = window.ImgstripSupport;
  // What each option is called in the panel, for the notes that say it cannot be applied to an image.
  const OPTION_LABEL = {
    deep: 'Deep scan', jpeg_lossless: 'Keep JPEG data bit-exact', scrub_lsb: 'Scrub least-significant bits',
    scrub_dct: 'Scrub JPEG coefficients', jpeg_canonical: 'Rebuild JPEG data canonically', scrub_prnu: 'Disrupt camera fingerprint',
    c2pa: 'Sign with Content Credentials', quality: 'Quality', icc: 'Colour profile', auto_orient: 'Apply orientation',
    optimize: 'Smaller PNGs', random_name: 'Random file names',
  };
  const OPTIONS_KEY = 'imgstrip.options.v1';
  const TOKEN_KEY = 'imgstrip.token';
  const FOLDER_KEY = 'imgstrip.folder';
  const GONE = new Error('removed');
  const GATE = {
    missing: ['Open imgstrip from your terminal', 'For your privacy this page only works when it is opened with the link imgstrip-web prints. Each link opens one tab.'],
    used: ['This link was already used', 'Each link opens imgstrip once, and the terminal has printed a fresh one. If you did not open it in another tab, someone else on this computer may have: stop imgstrip-web with Ctrl+C and start it again with --no-browser.'],
    replaced: ['imgstrip is open in another tab', 'Only the newest tab can use imgstrip. Switch to that tab, or open the latest link from the terminal here.'],
    expired: ['imgstrip was restarted', 'Open the new link printed in the terminal. Files cleaned in the earlier run are gone.'],
    down: ['imgstrip is not running', 'Start it again with imgstrip-web, then open the link it prints.'],
  };

  let TOKEN = null;
  const state = {
    info: null,
    items: [],
    nextId: 1,
    epoch: 0, // bumped by Clear: work started before it is ignored
    options: loadOptions(),
    run: null, // {total, done} while a clean batch runs
    packing: false,
    saving: false,
    gate: null,
    lastFolder: null,
    identity: null, // what the server says about the loaded signing identity (never the key)
    testRoot: null, // PEM of the test CA when a test identity was made here
    receipts: 0,
    loadingIdentity: false,
    review: { open: false, expanded: new Set(), signature: '', announced: '' },
  };

  const els = {
    window: $('#window'), well: $('#well'), list: $('#list'), files: $('#files'), listCount: $('#listCount'),
    foot: $('#foot'), summary: $('#summary'), primary: $('#primary'), downloadSome: $('#downloadSome'),
    saveToggle: $('#saveToggle'), save: $('#save'), saveForm: $('#saveForm'), saveSubmit: $('#saveSubmit'),
    folder: $('#folder'), footNote: $('#footNote'), picker: $('#picker'), options: $('#options'),
    optionsToggle: $('#optionsToggle'), optionsForm: $('#optionsForm'), limits: $('#limits'),
    limitsToggle: $('#limitsToggle'), clear: $('#clear'), choose: $('#choose'), announce: $('#announce'),
    gate: $('#gate'), gateTitle: $('#gateTitle'), gateText: $('#gateText'),
    credentials: $('#credentials'), credIdentity: $('#credIdentity'), identityForm: $('#identityForm'),
    identityError: $('#identityError'), identityLoad: $('#identityLoad'), credCert: $('#cred-cert'),
    credKey: $('#cred-key'), credPass: $('#cred-pass'), credAnchors: $('#cred-anchors'),
    credDate: $('#cred-date'), credDetail: $('#cred-detail'), credCommit: $('#cred-commit'),
    credHints: $('#credHints'), credReceipts: $('#credReceipts'), receiptText: $('#receiptText'),
    c2paSwitch: $('#c2paSwitch'), c2paHint: $('#c2paHint'),
    review: $('#review'), reviewToggle: $('#reviewToggle'), reviewTitle: $('#reviewTitle'), reviewPanel: $('#reviewPanel'),
    reviewGroups: $('#reviewGroups'), reviewLive: $('#reviewLive'),
  };

  // ---------------------------------------------------------------- helpers

  function h(tag, attrs, ...children) {
    const el = tag === 'svg' || tag === 'use' || tag === 'path' || tag === 'circle'
      ? document.createElementNS('http://www.w3.org/2000/svg', tag)
      : document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.setAttribute('class', v);
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat(Infinity)) {
      if (c === null || c === undefined || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  function icon(name, cls = 'icon') {
    return h('svg', { class: cls, 'aria-hidden': 'true', focusable: 'false' }, h('use', { href: `#i-${name}` }));
  }

  function bytes(n) {
    if (n < 1024) return `${n} B`;
    const units = ['KB', 'MB', 'GB'];
    let v = n / 1024, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`;
  }

  const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
  const formatName = (f) => ({ WEBP: 'WebP' }[f] || f);
  const capital = (s) => s.charAt(0).toUpperCase() + s.slice(1);

  function extOf(name) {
    const m = /\.([^.]+)$/.exec(name);
    return m ? m[1].toUpperCase() : '';
  }

  function announce(text, { force = false } = {}) {
    if (state.gate && !force) return;
    els.announce.textContent = '';
    window.setTimeout(() => { els.announce.textContent = text; }, 30);
  }

  function setOpen(reveal, toggle, open) {
    reveal.dataset.open = String(open);
    if (toggle) toggle.setAttribute('aria-expanded', String(open));
  }

  function setBusy(button, busy) {
    // aria-disabled, never `disabled`: a disabled button drops keyboard focus to <body>.
    if (busy) button.setAttribute('aria-disabled', 'true');
    else button.removeAttribute('aria-disabled');
  }
  const isBusy = (button) => button.getAttribute('aria-disabled') === 'true';

  function storage(kind) {
    return {
      get(key) { try { return window[kind].getItem(key); } catch { return null; } },
      set(key, value) { try { window[kind].setItem(key, value); } catch { /* private window or blocked */ } },
    };
  }
  const local = storage('localStorage');
  const session = storage('sessionStorage');

  function loadOptions() {
    try {
      const saved = JSON.parse(storage('localStorage').get(OPTIONS_KEY) || '{}');
      const out = { ...OPTION_DEFAULTS };
      for (const k of Object.keys(OPTION_DEFAULTS)) {
        if (!VOLATILE.includes(k) && saved && typeof saved[k] === typeof OPTION_DEFAULTS[k]) out[k] = saved[k];
      }
      return out;
    } catch {
      return { ...OPTION_DEFAULTS };
    }
  }

  // ---- which options apply to which image (the rules live in support.js) ----
  const supportEnv = () => ({
    limits: state.info && state.info.limits,
    formats: state.info && state.info.formats,
    signable: state.info && state.info.credentials && state.info.credentials.formats,
  });
  const factsOf = (item) => (item && item.report && item.report.facts) || null;
  const supportOf = (item) => S.evaluate(factsOf(item), state.options, supportEnv());
  // What this image is cleaned with: the chosen options, minus the ones that cannot apply to it. They are never
  // sent, so a PNG in a list with a JPEG never fails because a JPEG-only switch is on.
  const optionsFor = (item) => (item ? S.effective(state.options, supportOf(item)) : state.options);
  const signingFor = (item) => !!optionsFor(item).c2pa;
  // Signing is wanted when the switch is on and at least one image in the list can take it.
  const signing = () => !!state.options.c2pa && (!state.items.length || state.items.some((i) => signingFor(i) || !factsOf(i)));

  function cleanOptions(o) {
    const out = {
      format: o.format || null,
      quality: o.quality ? Number(o.quality) : null,
      icc: o.icc,
      jpeg_lossless: !!o.jpeg_lossless,
      scrub_lsb: !!o.scrub_lsb,
      scrub_dct: !!o.scrub_dct,
      jpeg_canonical: !!o.jpeg_canonical,
      scrub_prnu: !!o.scrub_prnu,
      auto_orient: !!o.auto_orient,
      optimize: !!o.optimize,
      random_name: !!o.random_name,
    };
    if (o.c2pa) out.credentials = { sign: true, date: o.c2pa_date, detail: o.c2pa_detail, commit: !!o.c2pa_commit };
    return out;
  }
  // A different identity is a different result, so it is part of the key.
  const optionsKey = (item) => JSON.stringify([cleanOptions(optionsFor(item)), item && signingFor(item) && state.identity ? state.identity.fingerprint : null]);
  const keysSignature = () => state.items.map(optionsKey).join('|');

  // ---------------------------------------------------------------- API

  class HttpError extends Error {
    constructor(message, status, code = null) { super(message); this.status = status; this.code = code; }
  }

  async function api(path, { method = 'POST', body, json, headers = {}, auth = true } = {}) {
    const hdrs = { ...headers };
    if (auth) hdrs['X-Imgstrip-Token'] = TOKEN || '';
    if (json !== undefined) {
      hdrs['Content-Type'] = 'application/json';
      body = JSON.stringify(json);
    } else if (body) {
      hdrs['Content-Type'] = 'application/octet-stream';
    }
    let res;
    try {
      res = await fetch(path, { method, body, headers: hdrs, credentials: 'omit', cache: 'no-store' });
    } catch {
      if (body instanceof Blob) {
        try { await body.slice(0, 1).arrayBuffer(); } catch {
          throw new HttpError('This file can no longer be read: it was moved, renamed or changed after you added it. Remove it and add it again.', 0);
        }
      }
      throw new HttpError('imgstrip is not running any more. Start it again with imgstrip-web.', 0);
    }
    const type = res.headers.get('Content-Type') || '';
    if (!res.ok) {
      let message = `Something went wrong (HTTP ${res.status}).`;
      let code = null;
      if (type.includes('application/json')) {
        try { const j = await res.json(); message = j.error || message; code = j.code || null; } catch { /* keep default */ }
      }
      if (res.status === 403 && auth) showGate(code === 'replaced' ? 'replaced' : 'expired');
      throw new HttpError(message, res.status, code);
    }
    return type.includes('application/json') ? res.json() : res.blob();
  }

  function limiter(n) {
    let active = 0;
    const queue = [];
    const next = () => {
      if (active >= n || !queue.length) return;
      active++;
      const { fn, resolve, reject } = queue.shift();
      Promise.resolve().then(fn).then(resolve, reject).finally(() => { active--; next(); });
    };
    return (fn) => new Promise((resolve, reject) => { queue.push({ fn, resolve, reject }); next(); });
  }
  const scanQueue = limiter(2);
  const cleanQueue = limiter(2);

  const alive = (item, epoch) => state.epoch === epoch && state.items.includes(item);
  const discard = (id) => api(`/api/result/${id}`, { method: 'DELETE' }).catch(() => {});

  // ---------------------------------------------------------------- seal

  // A guilloché rosette engraved from the clean file's SHA-256: the outer
  // braided band stands for the structure check, the inner rosette for the
  // pixel check. Same file, same seal.
  function hashBytes(hex) {
    const out = [];
    for (let i = 0; i + 1 < hex.length; i += 2) out.push(parseInt(hex.slice(i, i + 2), 16));
    while (out.length < 32) out.push(out.length * 37 % 256);
    return out;
  }

  function polar(fn, steps) {
    let d = '';
    for (let s = 0; s <= steps; s++) {
      const t = (s / steps) * Math.PI * 2;
      const r = fn(t);
      const x = 50 + r * Math.cos(t), y = 50 + r * Math.sin(t);
      d += `${s ? 'L' : 'M'}${x.toFixed(2)} ${y.toFixed(2)}`;
    }
    return `${d}Z`;
  }

  function sealSVG(hex, { detail = true, check = true } = {}) {
    const b = hashBytes(hex);
    const k1 = 14 + (b[0] % 9);           // lobes in the outer band
    const a1 = 2.6 + (b[1] % 4) * 0.45;   // band amplitude
    const n1 = detail ? 5 + (b[2] % 3) : 2;
    const p1 = (b[3] / 255) * Math.PI * 2;
    const k2 = 5 + (b[4] % 5);            // petals in the inner rosette
    const a2 = 6 + (b[5] % 4);
    const n2 = detail ? 4 + (b[6] % 3) : 2;
    const p2 = (b[7] / 255) * Math.PI * 2;
    const wob = (b[8] % 3) + 2;
    const svg = h('svg', { viewBox: '0 0 100 100', 'aria-hidden': 'true', focusable: 'false' });
    let i = 0;
    const add = (el, cls) => {
      el.setAttribute('class', `${cls} s-draw`);
      el.setAttribute('pathLength', '1');
      el.style.setProperty('--i', String(i++));
      svg.append(el);
    };
    add(h('circle', { cx: 50, cy: 50, r: 47 }), 's-line');
    const steps1 = Math.max(240, k1 * 18);
    for (let j = 0; j < n1; j++) {
      const ph = p1 + (j * Math.PI) / n1;
      add(h('path', { d: polar((t) => 39.5 + a1 * Math.sin(k1 * t + ph) + 0.6 * Math.sin(wob * t), steps1) }), 's-hair');
    }
    add(h('circle', { cx: 50, cy: 50, r: 32 }), 's-hair');
    const steps2 = Math.max(200, k2 * 30);
    for (let j = 0; j < n2; j++) {
      const ph = p2 + (j * Math.PI * 2) / (k2 * n2);
      add(h('path', { d: polar((t) => 20 + a2 * Math.cos(k2 * t + ph), steps2) }), 's-hair');
    }
    if (check) add(h('path', { d: 'M42.5 50.5l5 5 10-11' }), 's-bold');
    else add(h('circle', { cx: 50, cy: 50, r: 3 }), 's-line');
    return svg;
  }

  function seal(hex, opts = {}) {
    const wrap = h('span', { class: `seal${opts.animate && !reduceMotion.matches ? ' is-drawing' : ''}` });
    wrap.append(sealSVG(hex, opts));
    return wrap;
  }

  // ---------------------------------------------------------------- the access gate

  function showGate(kind) {
    if (state.gate) return;
    state.gate = kind;
    const [title, text] = GATE[kind];
    els.gateTitle.textContent = title;
    els.gateText.textContent = text;
    els.gate.hidden = false;
    els.well.hidden = true;
    $('#formats').hidden = true;
    els.list.hidden = true;
    els.foot.hidden = true;
    els.optionsToggle.hidden = true;
    setOpen(els.options, els.optionsToggle, false);
    els.gateTitle.tabIndex = -1;
    els.gateTitle.focus();
    announce(`${title}. ${text}`, { force: true });
  }

  async function authenticate() {
    const m = /[#&]k=([A-Za-z0-9_-]{16,})/.exec(window.location.hash);
    if (m) {
      // Take the key out of the address bar and history before anything else.
      window.history.replaceState(null, '', window.location.pathname);
      try {
        const res = await api('/api/session', { json: { key: m[1] }, auth: false });
        TOKEN = res.token;
        session.set(TOKEN_KEY, TOKEN);
        return true;
      } catch (err) {
        showGate(err.status === 403 ? 'used' : 'down');
        return false;
      }
    }
    TOKEN = session.get(TOKEN_KEY);
    if (!TOKEN) {
      showGate('missing');
      return false;
    }
    return true;
  }

  // ---------------------------------------------------------------- items

  const findingCount = (r) => (r ? r.findings.length : 0);
  const isDamaged = (r) => !!(r && r.format && r.error);
  const unreadable = (r) => !!(r && (!r.decodable || r.raw));
  const wantDeep = (item) => !!(state.options.deep || item.deepOnce);
  // What can honestly be said about data hidden in the pixel values: one of four states (detected,
  // scrubbed, not_detected, not_scanned) worded by the server, so this page cannot drift from the
  // command line. "not_detected" is never proof of absence, and the caveat always travels with it.
  const scanVerdict = (r) => (r && r.steganography) || null;
  const hasStego = (r) => { const v = scanVerdict(r); return !!(v && v.state === 'detected'); };
  // The engine's own account if it scrubbed (never the option that was asked for), else the scan of the original.
  const stegoOf = (item) => {
    const scan = scanVerdict(item.report);
    const out = item.result && item.result.steganography;
    if (out && out.state === 'scrubbed') return out;
    if (scan && (scan.state === 'detected' || scan.state === 'not_detected')) return scan;
    return out || scan;
  };
  // A clean file whose pixels still carry a detected payload (nothing scrubbed it) earns no seal.
  const keptPayload = (item) => !!(item.result && (stegoOf(item) || {}).state === 'detected');
  // A scrub was asked for but this image could not take it, so its pixels were neither scanned nor scrubbed.
  const unappliedScrub = (item) => !!(item.result && item.result.scrub_requested && item.result.lsb_scrub === 'not_applied');
  const refused = (item) => !!(item.error && item.state === 'clean' && item.failedKey === optionsKey(item));

  function newItem(file) {
    const item = {
      id: state.nextId++,
      file,
      name: file.name || 'pasted image',
      size: file.size,
      state: 'scanning',
      report: null,
      result: null,
      error: null,
      failedKey: null,
      blocked: null, // why this file cannot be cleaned at all (its size or pixel count is over a limit)
      open: false,
      deepOnce: false,
      scanSeq: 0,
      thumbUrl: null,
      justCleaned: false,
      sealDrawn: false,
      el: null,
      ui: null,
    };
    // A file over the upload limit is never sent: the server would only refuse it after reading it.
    item.blocked = S.blocker(null, item.size, supportEnv());
    if (item.blocked) item.state = 'unsupported';
    state.items.push(item);
    return item;
  }

  function addFiles(fileList) {
    if (state.gate) return;
    const files = Array.from(fileList || []).filter((f) => f && f.size !== undefined);
    if (!files.length) return;
    disarmClear();
    const added = files.map(newItem);
    for (const item of added) {
      mountItem(item);
      loadThumb(item);
      scan(item);
    }
    render();
    announce(`Scanning ${plural(added.length, 'image')}.`);
  }

  async function scan(item) {
    if (item.blocked && !item.report) return;
    const epoch = state.epoch;
    const seq = ++item.scanSeq;
    item.state = 'scanning';
    renderItem(item);
    render();
    try {
      const report = await scanQueue(() => {
        if (!alive(item, epoch) || seq !== item.scanSeq) return Promise.reject(GONE);
        return api(`/api/inspect?deep=${wantDeep(item) ? 1 : 0}`, {
          body: item.file, headers: { 'X-Filename': encodeURIComponent(item.name) },
        });
      });
      if (!alive(item, epoch) || seq !== item.scanSeq) return;
      item.report = report;
      item.error = null;
      if (!!report.deep !== wantDeep(item) && report.decodable && !report.raw) {
        scan(item); // Deep scan was switched while this file waited: scan it again the new way
        return;
      }
      item.blocked = S.blocker(report.facts, item.size, supportEnv());
      item.state = unreadable(report) || item.blocked ? 'unsupported' : 'scanned';
    } catch (err) {
      if (err === GONE || !alive(item, epoch) || seq !== item.scanSeq) return;
      item.state = 'scanfailed';
      item.error = err.message;
    }
    renderItem(item);
    render();
    if (!state.items.some((i) => i.state === 'scanning')) {
      const found = state.items.reduce((n, i) => n + (i.state === 'clean' || i.state === 'unsupported' ? 0 : findingCount(i.report)), 0);
      const failed = state.items.filter((i) => i.state === 'scanfailed').length;
      announce(`Scan finished. ${plural(found, 'revealing item')} found${failed ? `; ${failed} could not be scanned` : ''}.`);
    }
  }

  function cleanable() {
    return state.items.filter((i) => {
      const key = optionsKey(i);
      return (i.state === 'scanned') ||
        (i.state === 'failed' && i.failedKey !== key) ||
        (i.state === 'clean' && i.result.key !== key && i.failedKey !== key);
    });
  }

  function cleanHeaders(item, options) {
    const headers = { 'X-Filename': encodeURIComponent(item.name), 'X-Options': options };
    const scan = scanVerdict(item.report);
    if (scan && (scan.state === 'detected' || scan.state === 'not_detected')) headers['X-Scan'] = scan.state;
    return headers;
  }

  async function cleanAll() {
    const todo = cleanable();
    if (!todo.length || state.run) return;
    if (todo.some(signingFor) && !state.identity) {
      openCredentials();
      showNote('Load a signing identity first, or turn off Sign with Content Credentials.', 'error');
      return;
    }
    // Each image is cleaned with the options that apply to it, fixed now so changing a switch mid-run cannot mix them.
    const plan = new Map(todo.map((i) => [i, { key: optionsKey(i), options: encodeURIComponent(JSON.stringify(cleanOptions(optionsFor(i)))) }]));
    const epoch = state.epoch;
    const run = { total: todo.length, done: 0 };
    const leave = () => { if (state.run === run) { run.total--; render(); } };
    state.run = run;
    hideNote();
    render();
    await Promise.all(todo.map(async (item) => {
      const previous = item.result;
      const { key, options } = plan.get(item);
      item.state = 'cleaning';
      renderItem(item);
      try {
        const result = await cleanQueue(() => (alive(item, epoch)
          ? api('/api/clean', { body: item.file, headers: cleanHeaders(item, options) })
          : Promise.reject(GONE)));
        result.key = key;
        if (!alive(item, epoch)) { discard(result.id); leave(); return; }
        if (previous) discard(previous.id);
        Object.assign(item, { result, state: 'clean', error: null, failedKey: null, justCleaned: true, sealDrawn: false });
        if (result.credentials && result.credentials.committed) { state.receipts += 1; renderReceipts(); }
      } catch (err) {
        if (err === GONE || !alive(item, epoch)) { leave(); return; }
        item.error = err.message;
        // A refusal from the engine is final for these options; a lost connection is worth retrying.
        item.failedKey = err.status === 422 ? key : null;
        item.state = previous ? 'clean' : 'failed';
      }
      if (state.run !== run) return;
      run.done++;
      renderItem(item);
      render();
    }));
    if (state.run !== run) return;
    state.run = null;
    render();
    const kept = todo.filter((i) => state.items.includes(i));
    const failed = kept.filter((i) => i.error).length;
    const cleaned = kept.length - failed;
    const partial = kept.filter((i) => !i.error && (keptPayload(i) || unappliedScrub(i))).length;
    const verified = cleaned - partial;
    const caveat = partial
      ? ` ${plural(partial, 'image')} cleaned of metadata only: a payload was kept or the scrub you asked for could not be applied.`
      : '';
    announce(failed
      ? `${plural(verified, 'image')} cleaned and verified.${caveat} ${failed} could not be cleaned.`
      : `${plural(verified, 'image')} cleaned and verified.${caveat}`);
  }

  function removeItem(item) {
    const index = state.items.indexOf(item);
    state.items = state.items.filter((i) => i !== item);
    if (item.result) discard(item.result.id);
    if (item.thumbUrl) URL.revokeObjectURL(item.thumbUrl);
    item.el.remove();
    disarmClear();
    render();
    const next = state.items[index] || state.items[index - 1];
    if (next) next.ui.head.focus();
    else els.choose.focus();
    announce(`Removed ${item.name}. ${plural(state.items.length, 'image')} left.`);
  }

  function clearAll() {
    state.epoch++;
    state.run = null;
    for (const item of state.items) if (item.thumbUrl) URL.revokeObjectURL(item.thumbUrl);
    state.items = [];
    els.files.replaceChildren();
    api('/api/reset', { json: {} }).catch(() => {});
    hideNote();
    setOpen(els.save, els.saveToggle, false);
    state.review.expanded.clear();
    render();
    els.choose.focus();
    announce('List cleared.');
  }

  // ---------------------------------------------------------------- thumbnails

  function loadThumb(item) {
    const holder = item.ui.thumb;
    if (item.blocked) return;
    const fallback = async () => {
      try {
        const blob = await api('/api/preview', { body: item.file, headers: { 'X-Filename': encodeURIComponent(item.name) } });
        if (!state.items.includes(item)) return;
        if (item.thumbUrl) URL.revokeObjectURL(item.thumbUrl);
        item.thumbUrl = URL.createObjectURL(blob);
        holder.replaceChildren(h('img', { alt: '', src: item.thumbUrl, decoding: 'async' }));
      } catch {
        holder.replaceChildren(icon('image'));
      }
    };
    if (!/^image\/(jpeg|png|webp|gif|avif|bmp)$/.test(item.file.type || '')) {
      fallback();
      return;
    }
    item.thumbUrl = URL.createObjectURL(item.file);
    const img = h('img', { alt: '', decoding: 'async', loading: 'lazy' });
    img.addEventListener('error', fallback, { once: true });
    img.src = item.thumbUrl;
    holder.replaceChildren(img);
  }

  // ---------------------------------------------------------------- rendering: rows

  function mountItem(item) {
    const detailId = `detail-${item.id}`;
    const ui = {};
    ui.thumb = h('span', { class: 'thumb' }, icon('image'));
    ui.name = h('span', { class: 'file-name' });
    ui.sub = h('span', { class: 'file-sub' });
    ui.status = h('span', { class: 'status' });
    ui.head = h('button', {
      class: 'file-head', type: 'button', 'aria-expanded': 'false', 'aria-controls': detailId,
      onclick: () => { item.open = !item.open; renderItem(item); },
    }, ui.thumb, h('span', { class: 'file-main' }, ui.name, ui.sub), ui.status, icon('chevron', 'icon chev'));
    ui.download = h('a', { class: 'btn btn-icon is-off', tabindex: '-1', 'aria-hidden': 'true' }, icon('download'));
    ui.remove = h('button', { class: 'btn btn-icon', type: 'button', title: 'Remove', onclick: () => removeItem(item) }, icon('x'));
    ui.body = h('div', { class: 'detail-body' });
    ui.detail = h('div', { class: 'reveal', id: detailId, 'data-open': 'false' }, h('div', { class: 'reveal-inner' }, ui.body));
    item.ui = ui;
    item.el = h('li', { class: 'file', 'data-state': item.state, 'data-open': 'false' },
      h('div', { class: 'file-row' }, ui.head, h('div', { class: 'file-actions' }, ui.download, ui.remove)), ui.detail);
    els.files.append(item.el);
    renderItem(item);
  }

  function revealSummary(r) {
    if (r.reveals.length) return `Reveals ${r.reveals.slice(0, 4).map((x) => REVEAL_NOUN[x.key] || x.label.toLowerCase()).join(', ')}`;
    const nouns = [];
    for (const f of r.findings) {
      const noun = f.category === 'color' ? 'a colour profile'
        : f.category === 'hidden' ? 'hidden data'
          : f.category === 'structure' ? 'unknown structures'
            : /^([A-Z0-9]{2,}|Photoshop|Adobe|Apple|Google|Samsung|Microsoft|Netscape)\b/.test(f.label) ? `${f.label.replace(/ \(.*\)$/, '')}${/^(EXIF|XMP|IPTC|TIFF)\b/.test(f.label) ? ' data' : ''}`
              : f.label.charAt(0).toLowerCase() + f.label.slice(1).replace(/ \(.*\)$/, '');
      if (!nouns.includes(noun)) nouns.push(noun);
    }
    return `Contains ${nouns.slice(0, 3).join(', ')}`;
  }

  function itemSub(item) {
    const r = item.report;
    if (item.state === 'clean' && item.result) {
      const res = item.result;
      return { text: `${formatName(res.output_format)} · ${res.size[0]}×${res.size[1]} · ${bytes(res.bytes_in)} → ${bytes(res.bytes_out)}${res.credentials ? ' · signed' : ''}` };
    }
    if (item.state === 'unsupported') {
      if (item.blocked) return { text: item.blocked };
      return { text: r.raw ? `Camera RAW (${r.raw}) isn't supported` : "Not an image imgstrip can read" };
    }
    if (r && findingCount(r) && item.state !== 'scanning') return { text: revealSummary(r), reveal: true };
    const parts = [];
    const fmt = r && (r.format || r.decodable);
    if (fmt) parts.push(formatName(fmt));
    else if (extOf(item.name)) parts.push(extOf(item.name));
    if (r && r.size) parts.push(`${r.size[0]}×${r.size[1]}`);
    parts.push(bytes(item.size));
    return { text: parts.join(' · ') };
  }

  function itemStatus(item) {
    const r = item.report;
    switch (item.state) {
      case 'scanning': return [h('span', { text: 'Scanning…' })];
      case 'cleaning': return [h('span', { text: 'Cleaning…' })];
      case 'scanfailed': return [icon('alert'), h('span', { class: 'status-fail', text: 'Not scanned' })];
      case 'failed': return [icon('alert'), h('span', { class: 'status-fail', text: 'Not cleaned' })];
      case 'unsupported': return [h('span', { text: 'Not supported' })];
      case 'clean': {
        const res = item.result;
        const stale = res.key !== optionsKey(item);
        const anim = item.justCleaned;
        item.justCleaned = false;
        if (refused(item)) return [icon('alert'), h('span', { class: 'status-fail', text: 'Re-clean failed' })];
        if (keptPayload(item)) {
          return [
            icon('alert'),
            h('span', { class: 'status-fail', text: 'Payload kept' }),
            h('span', { class: 'seal-tip', id: `tip-${item.id}`, role: 'tooltip' },
              h('strong', { text: 'Metadata removed, payload kept' }),
              h('span', { text: 'Structure verified, but a likely LSB payload is still in the pixels. Turn on Scrub least-significant bits and clean again.' })),
          ];
        }
        if (unappliedScrub(item)) {
          return [
            icon('alert'),
            h('span', { class: 'status-fail', text: 'Clean · scrub not applied' }),
            h('span', { class: 'seal-tip', id: `tip-${item.id}`, role: 'tooltip' },
              h('strong', { text: 'Metadata removed, pixels not scrubbed' }),
              h('span', { text: 'Scrub least-significant bits could not be applied to this kind of image, so hidden data in its pixels was neither scanned nor destroyed.' })),
          ];
        }
        return [
          seal(res.sha256, { detail: false, animate: anim }),
          h('span', { class: stale ? '' : 'status-ok', text: stale ? 'Clean, old options' : 'Clean' }),
          h('span', { class: 'seal-tip', id: `tip-${item.id}`, role: 'tooltip' },
            h('strong', { text: 'Verified clean' }),
            h('span', { text: 'Structure verified · ' + (res.lossy ? 'pixels within tolerance' : 'pixels exact') }),
            h('span', { text: `Hidden data in pixels: ${(stegoOf(item) || { headline: 'not scanned' }).headline}` }),
            res.dct ? h('span', { text: `JPEG coefficients: ${res.dct.headline}${res.dct.psnr_db ? ` · about ${res.dct.psnr_db} dB from the original` : ''}` }) : null,
            h('code', { text: `SHA-256 ${res.sha256.slice(0, 16)}…` })),
        ];
      }
      default: {
        const scrub = optionsFor(item).scrub_lsb;
        if (hasStego(r) && !scrub) return [icon('alert'), h('span', { class: 'status-fail', text: 'Payload in pixels' })];
        const n = findingCount(r);
        if (n) return [h('span', { class: 'status-count', text: `${n} to remove` })];
        if (isDamaged(r)) return [icon('alert'), h('span', { class: 'status-fail', text: 'Damaged' })];
        if (r && !r.format) return [h('span', { text: 'Not inspected' })];
        const v = scanVerdict(r);
        return [h('span', { text: v && v.state === 'not_detected' ? 'No metadata · not detected in pixels' : 'No metadata · pixels not scanned' })];
      }
    }
  }

  // What kind of thing the row's status is, so the pill can be coloured by meaning: busy, muted (neutral), leak (the
  // file gives something away), ok (verified clean) or warn (needs a look).
  function toneOf(item) {
    const r = item.report;
    switch (item.state) {
      case 'scanning': case 'cleaning': return 'busy';
      case 'scanfailed': case 'failed': return 'warn';
      case 'unsupported': return 'muted';
      case 'clean':
        if (refused(item) || keptPayload(item) || unappliedScrub(item)) return 'warn';
        return item.result.key !== optionsKey(item) ? 'muted' : 'ok';
      default:
        if (hasStego(r) && !optionsFor(item).scrub_lsb) return 'warn';
        if (findingCount(r)) return 'leak';
        return isDamaged(r) ? 'warn' : 'muted';
    }
  }

  function renderItem(item) {
    const { el, ui } = item;
    if (!el) return;
    el.dataset.state = item.state;
    el.dataset.open = String(item.open);
    ui.name.textContent = item.name;
    const sub = itemSub(item);
    ui.sub.textContent = sub.text;
    ui.sub.classList.toggle('is-reveal', !!sub.reveal);
    ui.status.replaceChildren(...itemStatus(item));
    ui.status.dataset.tone = toneOf(item);
    ui.head.setAttribute('aria-expanded', String(item.open));
    const statusText = Array.from(ui.status.childNodes).filter((n) => !n.classList || !n.classList.contains('seal-tip')).map((n) => n.textContent).join(' ').trim();
    ui.head.setAttribute('aria-label', `${item.name}. ${statusText}. ${sub.text}`);
    if (ui.status.querySelector('.seal-tip')) ui.head.setAttribute('aria-describedby', `tip-${item.id}`);
    else ui.head.removeAttribute('aria-describedby');

    // Persistent action buttons: only their state changes, so keyboard focus never falls off them.
    const res = item.result;
    const dl = ui.download;
    if (res && (item.state === 'clean' || item.state === 'cleaning')) {
      dl.classList.remove('is-off');
      dl.href = `/api/result/${res.id}`;
      dl.setAttribute('download', res.filename);
      dl.setAttribute('aria-label', `Download ${res.filename}`);
      dl.title = `Download ${res.filename}`;
      dl.removeAttribute('tabindex');
      dl.removeAttribute('aria-hidden');
    } else if (!dl.classList.contains('is-off')) {
      const hadFocus = document.activeElement === dl;
      dl.classList.add('is-off');
      dl.removeAttribute('href');
      dl.setAttribute('tabindex', '-1');
      dl.setAttribute('aria-hidden', 'true');
      if (hadFocus) ui.remove.focus();
    }
    ui.remove.setAttribute('aria-label', `Remove ${item.name} from the list`);

    ui.detail.dataset.open = String(item.open);
    if (item.open) {
      const active = document.activeElement;
      const keep = active && ui.body.contains(active) ? active.dataset.key : null;
      ui.body.replaceChildren(...detailContent(item));
      if (keep) (ui.body.querySelector(`[data-key="${keep}"]`) || ui.head).focus();
    }
  }

  function groupList(findings, removed) {
    const groups = new Map();
    for (const f of findings) {
      if (!groups.has(f.category)) groups.set(f.category, []);
      groups.get(f.category).push(f);
    }
    const order = [...CATEGORY_ORDER, ...[...groups.keys()].filter((k) => !CATEGORY_ORDER.includes(k))];
    return order.filter((k) => groups.has(k)).map((k) => h('section', { class: 'group' },
      h('h3', { class: 'group-title' }, `${removed ? 'Removed: ' : ''}${CATEGORY[k] || k}`,
        h('span', { class: 'n' }, h('span', { class: 'sr-only', text: ', ' }), String(groups.get(k).length), h('span', { class: 'sr-only', text: groups.get(k).length === 1 ? ' item' : ' items' }))),
      h('ul', { class: 'items' }, groups.get(k).map((f) => h('li', { class: 'item' },
        h('span', { class: 'item-label', text: f.label }),
        h('span', { class: 'item-detail', text: f.detail || '—' }))))));
  }

  // The pixel-level verdict, in the server's words. Shown with its caveat every time, so a scan that found
  // nothing is never read as proof that nothing is hidden.
  function stegoBlock(v) {
    if (!v) return [];
    const head = `Hidden data in the pixels: ${v.headline}. ${capital(v.summary)}.`;
    return [h('section', { class: 'group' },
      note(head, v.state === 'detected' ? 'warn' : ''),
      v.state === 'not_scanned' ? null : note(v.caveat))];
  }

  // What cleaning did about data hidden in a JPEG's DCT coefficients (only present when asked for), in the server's
  // words and always with its caveat: it says what was done, never that nothing is hidden.
  function dctBlock(d) {
    if (!d) return [];
    const cost = d.psnr_db ? ` About ${d.psnr_db} dB from the original picture.` : '';
    return [h('section', { class: 'group' },
      note(`Hidden data in the JPEG coefficients: ${d.headline}. ${capital(d.summary)}.${cost}`),
      note(d.caveat))];
  }

  function note(text, kind = '') {
    const glyph = kind === 'warn' || kind === 'error' ? 'alert' : kind === 'ok' ? 'check' : 'info';
    return h('p', { class: `note${kind ? ` is-${kind}` : ''}` }, icon(glyph), h('span', { text }));
  }

  // What a signed file's credential says and how public viewers will read it.
  function credentialBlock(res, o) {
    const c = res.credentials;
    const who = c.signer || 'an unnamed signer';
    const lines = [
      h('p', { class: 'credential-line' }, h('strong', { text: `Signed by ${who}` }), ` · ${c.date}`),
      h('p', { class: 'credential-line', text: PIXELS[c.pixel_integrity] || '' }),
    ];
    // What was done is always shown to you; it goes into the credential only at detail "full".
    const where = c.detail === 'full' ? 'Recorded in the credential' : 'Not recorded in the credential';
    if (c.operations && c.operations.length) lines.push(h('p', { class: 'credential-state', text: `${where}: operations ${c.operations.join(', ')}.` }));
    if (c.psnr_db !== null && c.psnr_db !== undefined) lines.push(h('p', { class: 'credential-state', text: `${where}: measured fidelity ${c.psnr_db} dB PSNR.` }));
    lines.push(h('p', { class: 'credential-state', text: `Public viewers will see: ${c.label}.` }));
    for (const w of c.warnings || []) lines.push(note(w, 'warn'));
    if (c.pixel_integrity === 'equivalent' && res.output_format === 'JPEG' && !o.jpeg_lossless) {
      lines.push(note('To be able to say "identical", turn on Keep JPEG data bit-exact in Options and clean again.'));
    }
    if (c.committed) lines.push(note('It holds a sealed fingerprint of the original. Download the receipts in Options and keep them as private as the original.'));
    lines.push(note('This credential vouches for what you did to the file, not for who took the picture or that it is genuine.'));
    return h('section', { class: 'credential' }, h('h3', { class: 'group-title' }, icon('key'), 'Content Credential'), ...lines);
  }

  // A credential the scanned file already carries (cleaning removes it).
  function foundCredential(c) {
    const lines = [];
    const who = c.signer ? `Signed by ${c.signer}` : 'Signer unknown';
    lines.push(h('p', { class: 'credential-line' }, h('strong', { text: who }), c.state ? ` · ${c.label}` : ''));
    if (c.sanitization) lines.push(h('p', { class: 'credential-line', text: `An imgstrip sanitisation credential from ${c.sanitization.date}. ${PIXELS[c.sanitization.pixel_integrity] || ''}` }));
    if (c.declared_ai) lines.push(note('It declares this image AI-generated or AI-edited. Cleaning removes that label from the copy.', 'warn'));
    for (const p of c.problems || []) lines.push(note(`Problem: ${p}`, 'warn'));
    lines.push(note('Cleaning removes Content Credentials like any other metadata, so the copy is no longer signed by whoever signed this file.'));
    return h('section', { class: 'credential is-found' }, h('h3', { class: 'group-title' }, icon('key'), 'Content Credentials found'), ...lines);
  }

  // The options switched on that cannot be done for this image, with why. (Options with nothing to act on are not
  // listed: a JPEG-only switch has no business being mentioned for a PNG.)
  function skippedOptions(item) {
    const states = supportOf(item);
    const lines = [];
    for (const name of S.OPTION_NAMES) {
      const on = name === 'quality' || name === 'icc' ? false : !!state.options[name];
      if (on && states[name].state === S.UNSUPPORTED && (name !== 'c2pa' || signingAvailable())) {
        lines.push(h('p', { class: 'note is-warn' }, icon('warn'), h('span', { text: `${OPTION_LABEL[name]} was not applied to this image. ${states[name].reason}` })));
      }
    }
    const fmt = state.options.format;
    const fs = fmt && S.formatStates(factsOf(item), supportEnv())[fmt];
    if (fs && fs.state === S.UNSUPPORTED) {
      lines.push(h('p', { class: 'note is-warn' }, icon('warn'), h('span', { text: `Saving as ${formatName(fmt)} will fail for this image. ${fs.reason}` })));
    }
    return lines;
  }
  const signingAvailable = () => !!(state.info && state.info.credentials && state.info.credentials.available);

  function detailContent(item) {
    const r = item.report;
    // Rows clip long names; the detail repeats the whole name only when the row cut it off.
    const n = item.ui.name;
    const clipped = n.scrollWidth > n.clientWidth + 1 || n.scrollHeight > n.clientHeight + 1;
    const out = [h('p', { class: `detail-name${clipped ? ' is-clipped' : ''}`, text: item.name })];
    const meta = [];
    const fmt = r && (r.format || r.decodable);
    if (fmt) meta.push(formatName(fmt));
    if (r && r.size) meta.push(`${r.size[0]}×${r.size[1]}`);
    meta.push(bytes(item.size));
    if (r && r.pixels_scanned) meta.push('pixels scanned');

    if (item.state === 'clean' && item.result) {
      const res = item.result;
      const checks = h('div', { class: 'checks' },
        h('p', { class: 'check' }, icon('check'), h('span', { text: 'Every segment, chunk and box of the new file is accounted for, and the places meant to be empty are empty.' })),
        h('p', { class: 'check' }, icon('check'), h('span', {
          text: res.lossy
            ? `It decodes back to the written pixels within ${formatName(res.output_format)} re-encoding tolerance.`
            : 'It decodes back to exactly the pixels that were written.',
        })),
        h('div', { class: 'hash' },
          h('code', { text: `SHA-256 ${res.sha256}` }),
          h('button', {
            class: 'btn btn-icon', type: 'button', 'aria-label': 'Copy SHA-256', title: 'Copy SHA-256', 'data-key': 'copy',
            onclick: (e) => copyText(res.sha256, e.currentTarget),
          }, icon('copy'))));
      const kept = keptPayload(item);
      if (kept) {
        out.push(note('A steganography payload was detected in this file\'s pixels and is still there, because nothing scrubbed it. Turn on Scrub least-significant bits in Options and clean again to destroy it.', 'warn'));
        out.push(h('div', { class: 'verified is-partial' }, checks));
      } else if (unappliedScrub(item)) {
        out.push(note('Scrub least-significant bits was turned on, but it could not be applied to this kind of image, so hidden data in its pixels was neither scanned nor destroyed.', 'warn'));
        out.push(h('div', { class: 'verified is-partial' }, checks));
      } else {
        const animate = !item.sealDrawn;
        item.sealDrawn = true;
        out.push(h('div', { class: 'verified' }, seal(res.sha256, { detail: true, animate }), checks));
      }
      if (res.credentials) out.push(credentialBlock(res, optionsFor(item)));
      const frames = res.frames > 1 ? ` · ${res.frames} frames` : '';
      out.push(h('p', { class: 'meta-line', text: `${formatName(res.input_format)} ${bytes(res.bytes_in)} → ${formatName(res.output_format)} ${bytes(res.bytes_out)} · ${res.size[0]}×${res.size[1]}${frames} · saved as ${res.filename}` }));
      out.push(...stegoBlock(stegoOf(item)));
      out.push(...dctBlock(res.dct));
      if (res.key !== optionsKey(item)) out.push(note('Cleaned with earlier options. Clean again to apply the current ones.', 'warn'));
      if (refused(item)) out.push(note(`Cleaning again with the current options failed: ${item.error} This is the earlier clean file.`, 'error'));
      if (res.prnu) {
        out.push(note("The camera's sensor noise pattern was scrambled by a random warp of the pixels. Matching this photo to its camera should now fail, but unlike the checks above that cannot be proven."));
      } else if (res.output_format !== 'GIF' && !res.warnings.some((w) => w.includes('sensor-fingerprint'))) {
        out.push(note("The pixels still carry the camera's sensor noise pattern, which can link this photo to one camera. Turn on Disrupt camera fingerprint in Options to scramble it."));
      }
      out.push(...skippedOptions(item));
      if (res.removed.length) out.push(...groupList(res.removed, true));
      else if (!kept) out.push(note('Nothing beside the picture was found in the file\'s structure. The file was still rebuilt from its pixels.', 'ok'));
      if (res.actions.length) {
        out.push(h('section', { class: 'group' }, h('h3', { class: 'group-title', text: 'Changed' }),
          h('ul', { class: 'actions-list' }, res.actions.map((a) => h('li', { text: capital(a) })))));
      }
      for (const w of res.warnings) out.push(note(w, 'warn'));
      return out;
    }

    out.push(h('p', { class: 'meta-line', text: meta.join(' · ') }));
    if (item.state === 'failed' || item.state === 'scanfailed') out.push(note(item.error, 'error'));
    if (item.state === 'unsupported' && item.blocked) {
      out.push(note(`${item.blocked} It can't be cleaned.`, 'error'));
      return out;
    }
    if (!r) return out;
    if (item.state === 'unsupported') {
      out.push(note(r.raw
        ? `Camera RAW files (${r.raw}) aren't supported. Export the photo to TIFF, PNG or JPEG first.`
        : "This file isn't an image imgstrip can read, so it can't be cleaned.", 'error'));
      return out;
    }
    if (r.reveals.length) {
      out.push(h('dl', { class: 'reveals' }, r.reveals.map((x) => [
        h('dt', { text: x.label }),
        h('dd', { class: x.key === 'location' || x.key === 'serial' ? 'mono' : null, text: x.value }),
      ])));
    }
    if (r.credential && r.credential.present) out.push(foundCredential(r.credential));
    const verdict = scanVerdict(r);
    if (hasStego(r) && !optionsFor(item).scrub_lsb) {
      const lsb = supportOf(item).scrub_lsb;
      out.push(note(lsb.state === 'unsupported'
        ? `Cleaning removes everything else, but keeps this payload: Scrub least-significant bits can't be used on this image. ${lsb.reason}`
        : 'Cleaning removes everything else, but keeps this payload unless Scrub least-significant bits is on in Options.', 'warn'));
    }
    if (r.findings.length) out.push(...groupList(r.findings, false));
    if (isDamaged(r)) out.push(note(`The file's structure is damaged (${r.error}). Cleaning rebuilds it from its pixels if they can still be decoded.`, 'warn'));
    if (!r.format) out.push(note(`imgstrip can't list what a ${r.decodable} file holds, but cleaning rebuilds the image from its pixels alone.`));
    else if (!r.findings.length && !r.error) {
      out.push(note(verdict && verdict.state === 'not_detected'
        ? 'Nothing beside the picture was found in the file\'s structure. Cleaning still rebuilds it from its pixels.'
        : 'No metadata found. Cleaning still rebuilds the file from its pixels, but anything hidden in the pixel values can survive unless Scrub least-significant bits is on.'));
    }
    if (verdict && verdict.state !== 'not_scanned') out.push(...stegoBlock(verdict));
    for (const n of r.notes) out.push(note(n, 'warn'));
    // Options the image cannot take are left out of its clean, and this says so rather than dropping them quietly.
    out.push(...skippedOptions(item));
    if (!r.deep && item.state === 'scanned' && supportOf(item).deep.state === 'ok') {
      out.push(h('div', { class: 'detail-tools' }, h('button', {
        class: 'btn btn-secondary btn-small', type: 'button', 'data-key': 'deep',
        onclick: () => { item.deepOnce = true; scan(item); },
      }, h('span', { text: 'Scan the pixels too' }))));
    }
    return out;
  }

  async function copyText(text, button) {
    try {
      await navigator.clipboard.writeText(text);
      button.replaceChildren(icon('check'));
      announce('SHA-256 copied.');
      window.setTimeout(() => button.replaceChildren(icon('copy')), 1400);
    } catch {
      announce('Could not copy. Select the hash to copy it instead.');
    }
  }

  // ---------------------------------------------------------------- which options apply

  const WARN_MARK = '⚠︎';
  const supportControls = {};

  // Options are judged only once every image that will be cleaned has been read: until then nothing is ruled out.
  const judgeable = (i) => ['scanning', 'scanned', 'failed', 'clean', 'cleaning'].includes(i.state);

  function supportControl(name) {
    if (supportControls[name]) return supportControls[name];
    const input = els.optionsForm.elements.namedItem(name);
    if (!input) return null;
    const isField = input.tagName === 'SELECT';
    const root = input.closest(isField ? '.field' : '.switch');
    const host = isField ? root : $('.switch-text', root);
    const note = h('span', { class: 'support', id: `support-${name}`, hidden: true });
    host.append(note);
    return (supportControls[name] = { input, root, note, isField });
  }

  function listNames(items) {
    const names = items.map((i) => i.name);
    return names.length > 2 ? `${names[0]}, ${names[1]} and ${names.length - 2} more` : names.join(' and ');
  }

  // One option across every image in the list: usable (ok), nothing to act on (na), impossible (unsupported), or
  // usable for some of the images (partial, with the ones it skips).
  function aggregate(rows) {
    if (!rows.length || rows.some((r) => !r.facts)) return { state: S.OK };
    const bad = rows.filter((r) => r.state !== S.OK);
    if (!bad.length) return { state: S.OK };
    const unsupported = bad.filter((r) => r.state === S.UNSUPPORTED);
    const reasons = [...new Set((unsupported.length ? unsupported : bad).map((r) => r.reason))].slice(0, 2).join(' ');
    if (bad.length < rows.length) {
      return unsupported.length
        ? { state: 'partial', warn: true, text: `Skipped for ${listNames(unsupported.map((r) => r.item))}. ${reasons}` }
        : { state: 'partial', warn: false, text: `Applies to ${rows.length - bad.length} of ${plural(rows.length, 'image')}.` };
    }
    return { state: unsupported.length ? S.UNSUPPORTED : S.NA, text: reasons };
  }

  function paintNote(ctl, text, kind) {
    const { note, input } = ctl;
    note.replaceChildren();
    note.removeAttribute('title');
    note.className = 'support';
    note.hidden = !text;
    if (text) {
      if (kind === 'warn') note.append(icon('warn'));
      note.classList.add(kind === 'warn' ? 'is-warn' : 'is-na');
      note.append(h('span', { text }));
      note.title = text;
      input.setAttribute('aria-describedby', note.id);
    } else {
      input.removeAttribute('aria-describedby');
    }
  }

  function paintOption(name, agg) {
    const ctl = supportControl(name);
    if (!ctl) return;
    const { input, root } = ctl;
    if (name === 'c2pa' && !signingAvailable()) { paintNote(ctl, '', ''); root.removeAttribute('data-support'); return; }
    const off = agg.state === S.NA || agg.state === S.UNSUPPORTED;
    if (agg.state === S.OK) root.removeAttribute('data-support');
    else root.dataset.support = agg.state;
    input.disabled = off;
    // A switch that cannot apply shows as off whatever was chosen, and the choice comes back when it can apply again.
    if (!ctl.isField) input.checked = !!state.options[name] && !off;
    if (agg.state === S.UNSUPPORTED) paintNote(ctl, agg.text, 'warn');
    else if (agg.state === S.NA) paintNote(ctl, agg.text, 'na');
    else if (agg.state === 'partial') paintNote(ctl, agg.text, agg.warn ? 'warn' : 'na');
    else paintNote(ctl, '', '');
  }

  // The "Save as" menu: formats an image is too big or too deep for are marked, and a chosen one says why it will fail.
  function paintFormats(items) {
    const select = $('#opt-format');
    const root = select.closest('.field');
    if (!supportControls.format) {
      supportControls.format = { note: h('span', { class: 'support', id: 'support-format', hidden: true }), input: select };
      root.append(supportControls.format.note);
    }
    const ctl = supportControls.format;
    const decided = items.length > 0 && items.every(factsOf);
    const per = decided ? items.map((item) => ({ item, states: S.formatStates(factsOf(item), supportEnv()) })) : [];
    for (const opt of select.options) {
      if (!opt.value) continue;
      if (!opt.dataset.label) opt.dataset.label = opt.textContent;
      const bad = per.filter((p) => p.states[opt.value] && p.states[opt.value].state === S.UNSUPPORTED);
      opt.disabled = !!per.length && bad.length === per.length;
      opt.textContent = bad.length ? `${opt.dataset.label} ${WARN_MARK} ${opt.disabled ? 'not supported' : 'some images'}` : opt.dataset.label;
    }
    const chosen = select.value;
    const bad = chosen ? per.filter((p) => p.states[chosen] && p.states[chosen].state === S.UNSUPPORTED) : [];
    if (bad.length) {
      const reasons = [...new Set(bad.map((p) => p.states[chosen].reason))].slice(0, 2).join(' ');
      paintNote(ctl, `Cannot save ${listNames(bad.map((p) => p.item))} as ${formatName(chosen)}. ${reasons}`, 'warn');
    } else {
      paintNote(ctl, '', '');
    }
  }

  function syncSupport() {
    if (!state.info) return;
    const items = state.items.filter(judgeable);
    const per = new Map(items.map((item) => [item, supportOf(item)]));
    for (const name of S.OPTION_NAMES) {
      if (name === 'random_name') continue;
      const rows = items.map((item) => ({ item, facts: factsOf(item), ...per.get(item)[name] }));
      paintOption(name, aggregate(rows));
    }
    paintFormats(items);
  }

  // ---------------------------------------------------------------- the review tray

  // Every warning in the list, grouped by what is wrong rather than by file: sixty images with the same problem are
  // one entry with sixty names, not sixty notes. Errors (a file cannot be cleaned) come before warnings (it can, with
  // a caveat). Each entry is {key, severity, title, text, items}.
  function collectWarnings(items) {
    const groups = new Map();
    const add = (key, severity, title, text, item) => {
      if (!groups.has(key)) groups.set(key, { key, severity, title, texts: new Set(), items: [] });
      const g = groups.get(key);
      g.texts.add(text);
      if (!g.items.includes(item)) g.items.push(item);
    };
    for (const item of items) {
      const r = item.report;
      if (item.state === 'unsupported') {
        const why = item.blocked || (r && r.raw
          ? `Camera RAW files (${r.raw}) aren't supported. Export the photo to TIFF, PNG or JPEG first.`
          : "These files aren't images imgstrip can read.");
        add('unsupported', 'error', "Can't be cleaned", why, item);
        continue;
      }
      if (item.state === 'scanfailed') add('scanfailed', 'error', 'Could not be scanned', item.error, item);
      if (item.state === 'failed') add('failed', 'error', 'Could not be cleaned', item.error, item);
      if (refused(item)) add('refused', 'error', 'Cleaning again failed', `${item.error} The earlier clean file is kept.`, item);
      if (item.state === 'clean') {
        if (keptPayload(item)) add('payload-kept', 'warn', 'Payload kept in the pixels', 'Metadata was removed and the structure verified, but a likely hidden payload is still in the pixels. Turn on Scrub least-significant bits and clean again.', item);
        if (unappliedScrub(item)) add('scrub-unapplied', 'warn', 'Pixel scrub not applied', 'Scrub least-significant bits could not be applied to these images, so hidden data in their pixels was neither scanned nor destroyed.', item);
      } else if (r && hasStego(r) && !optionsFor(item).scrub_lsb && item.state !== 'failed') {
        const lsb = supportOf(item).scrub_lsb;
        add('payload-found', 'warn', 'Payload found in the pixels',
          lsb.state === S.UNSUPPORTED
            ? `Cleaning removes everything else but keeps it: Scrub least-significant bits can't be used here. ${lsb.reason}`
            : 'Cleaning removes everything else but keeps it unless Scrub least-significant bits is on in Options.', item);
      }
      if (r && isDamaged(r) && item.state !== 'clean') add('damaged', 'warn', 'Damaged structure', "The file's structure is damaged. Cleaning rebuilds it from its pixels if they can still be decoded.", item);
      if (!judgeable(item)) continue;
      const states = supportOf(item);
      for (const name of S.OPTION_NAMES) {
        const on = name === 'quality' || name === 'icc' ? false : !!state.options[name];
        if (on && states[name].state === S.UNSUPPORTED && (name !== 'c2pa' || signingAvailable())) {
          add(`skip:${name}`, 'warn', `${OPTION_LABEL[name]} won't be applied`, states[name].reason, item);
        }
      }
      const fmt = state.options.format;
      const fs = fmt && S.formatStates(factsOf(item), supportEnv())[fmt];
      if (fs && fs.state === S.UNSUPPORTED) add(`format:${fmt}`, 'error', `Can't be saved as ${formatName(fmt)}`, fs.reason, item);
    }
    // One cause can read differently per file (a size limit, a different error): the first two wordings are shown,
    // and the rest are left to each file's own row.
    for (const g of groups.values()) {
      const texts = [...g.texts];
      g.text = texts.length <= 2 ? texts.join(' ') : `${texts[0]} Other files have other reasons: open each one to see.`;
    }
    const rank = { error: 0, warn: 1 };
    return [...groups.values()].sort((a, b) => rank[a.severity] - rank[b.severity] || b.items.length - a.items.length);
  }

  function revealItem(item) {
    item.open = true;
    renderItem(item);
    item.el.scrollIntoView({ block: 'center', behavior: reduceMotion.matches ? 'auto' : 'smooth' });
    item.ui.head.focus({ preventScroll: true });
    item.el.dataset.flash = '';
    window.setTimeout(() => item.el.removeAttribute('data-flash'), 1000);
  }

  function setReviewOpen(open) {
    state.review.open = open;
    els.review.dataset.open = String(open);
    els.reviewToggle.setAttribute('aria-expanded', String(open));
    els.reviewPanel.hidden = !open;
  }

  function renderReview() {
    const groups = collectWarnings(state.items);
    const review = state.review;
    if (!groups.length) {
      els.review.hidden = true;
      if (review.open) setReviewOpen(false);
      review.signature = '';
      review.announced = '';
      return;
    }
    const affected = new Set(groups.flatMap((g) => g.items));
    const n = groups.length;
    els.reviewTitle.replaceChildren(`${n === 1 ? '1 thing needs' : `${n} things need`} a look`,
      h('span', { class: 'dim', text: ` · ${plural(affected.size, 'image')}` }));
    els.review.hidden = false;

    // Only rebuild when something shown changed, so a chip being pressed or focused is not torn out from under the user.
    const signature = JSON.stringify([groups.map((g) => [g.key, g.text, g.items.map((i) => i.id)]), [...review.expanded]]);
    if (signature !== review.signature) {
      review.signature = signature;
      const active = document.activeElement;
      const keep = active && els.reviewGroups.contains(active) ? active.dataset.key : null;
      els.reviewGroups.replaceChildren(...groups.map((g) => {
        const open = review.expanded.has(g.key);
        const shown = open ? g.items : g.items.slice(0, 3);
        const hidden = g.items.length - shown.length;
        return h('section', { class: `review-group is-${g.severity}` },
          h('h3', {}, h('span', { text: g.title }), h('span', { class: 'n' }, String(g.items.length), h('span', { class: 'sr-only', text: g.items.length === 1 ? ' image' : ' images' }))),
          h('p', { text: g.text }),
          h('div', { class: 'chips' },
            shown.map((item) => h('button', {
              class: 'chip', type: 'button', title: item.name, 'data-key': `chip-${g.key}-${item.id}`,
              'aria-label': `Show ${item.name}`, onclick: () => revealItem(item),
            }, item.name)),
            hidden > 0 || open && g.items.length > 3
              ? h('button', {
                class: 'chip chip-more', type: 'button', 'data-key': `more-${g.key}`, 'aria-expanded': String(open),
                onclick: () => { open ? review.expanded.delete(g.key) : review.expanded.add(g.key); review.signature = ''; renderReview(); },
              }, open ? 'Show fewer' : `+${hidden} more`)
              : null));
      }));
      if (keep) els.reviewGroups.querySelector(`[data-key="${CSS.escape(keep)}"]`)?.focus();
    }

    // Announced once the list settles, and only when the set of warnings changed: a big drop must not chatter.
    const busy = state.run || state.items.some((i) => i.state === 'scanning' || i.state === 'cleaning');
    const said = groups.map((g) => `${g.key}:${g.items.length}`).join('|');
    if (!busy && said !== review.announced) {
      review.announced = said;
      els.reviewLive.textContent = '';
      window.setTimeout(() => { els.reviewLive.textContent = `${plural(n, 'warning')} in ${plural(affected.size, 'image')}. Open the review list for details.`; }, 60);
    }
  }

  // ---------------------------------------------------------------- rendering: shell

  function render() {
    if (state.gate) return;
    syncSupport();
    const items = state.items;
    const any = items.length > 0;
    if (any) renderReview(); else if (!els.review.hidden) renderReview();
    els.well.hidden = any;
    $('#formats').hidden = any;
    els.list.hidden = !any;
    els.foot.hidden = !any;
    if (!any) return;

    const scanning = items.filter((i) => i.state === 'scanning').length;
    const clean = items.filter((i) => i.state === 'clean');
    const fresh = clean.filter((i) => i.result.key === optionsKey(i));
    const failed = items.filter((i) => i.state === 'failed' || i.state === 'scanfailed' || i.state === 'unsupported' || refused(i)).length;
    const payloads = clean.filter(keptPayload).length;
    const todo = cleanable();
    const found = items.reduce((n, i) => n + (i.state === 'clean' || i.state === 'unsupported' ? 0 : findingCount(i.report)), 0);
    const removed = clean.reduce((n, i) => n + i.result.removed.length, 0);

    els.listCount.textContent = plural(items.length, 'image');

    const s = [];
    if (state.run) s.push(h('span', { text: `Cleaning ${Math.min(state.run.done + 1, state.run.total)} of ${state.run.total}…` }));
    else if (scanning) s.push(h('span', { text: `Scanning ${plural(scanning, 'image')}…` }));
    else if (!clean.length) {
      s.push(found ? h('span', { class: 'is-reveal', text: plural(found, 'revealing item') }) : h('span', { text: 'No metadata found' }));
      s.push(` in ${plural(items.length, 'image')}`);
    } else {
      if (clean.length > payloads) s.push(h('span', { class: 'is-clean', text: `${clean.length - payloads} clean` }));
      if (payloads) s.push(h('span', { class: 'is-fail', text: `${clean.length > payloads ? ' · ' : ''}${payloads} kept a pixel payload` }));
      s.push(removed ? ` · ${plural(removed, 'item')} removed` : ' · rebuilt from pixels');
      if (fresh.length !== clean.length) s.push(' · options changed');
      else if (todo.length) s.push(` · ${todo.length} to clean`);
    }
    if (failed) s.push(h('span', { class: 'is-fail', text: ` · ${failed} not cleanable` }));
    els.summary.replaceChildren(...s);

    // The primary plate is the one next action; it is never `disabled`, so it keeps focus.
    const plate = els.primary;
    const label = $('span', plate);
    let mode;
    if (state.run) { mode = 'busy'; label.textContent = 'Cleaning…'; }
    else if (todo.length) {
      mode = 'clean';
      label.textContent = todo.every((i) => i.state === 'clean') ? 'Clean again with new options' : `Clean ${plural(todo.length, 'image')}`;
    } else if (fresh.length) {
      mode = 'download';
      label.textContent = state.packing ? 'Packing…' : fresh.length === 1 ? 'Download' : 'Download all (.zip)';
    } else { mode = 'wait'; label.textContent = scanning ? 'Scanning…' : 'Clean'; }
    plate.dataset.mode = mode;
    setBusy(plate, mode === 'busy' || mode === 'wait' || state.packing);
    // One reversed plate per state: while the save row is open, Save is the plate.
    const saveOpen = els.save.dataset.open === 'true';
    plate.classList.toggle('btn-plate', !(saveOpen && mode === 'download'));
    plate.classList.toggle('btn-secondary', saveOpen && mode === 'download');

    els.downloadSome.hidden = !(fresh.length && mode === 'clean');
    $('span', els.downloadSome).textContent = state.packing ? 'Packing…' : fresh.length === 1 ? 'Download' : `Download ${fresh.length}`;
    setBusy(els.downloadSome, state.packing);
    els.saveToggle.hidden = !fresh.length;
    if (!fresh.length && saveOpen) setOpen(els.save, els.saveToggle, false);
  }

  // ---------------------------------------------------------------- download & save

  function freshClean() {
    return state.items.filter((i) => i.state === 'clean' && i.result.key === optionsKey(i));
  }

  function triggerDownload(href, filename) {
    const a = h('a', { href, download: filename, hidden: true });
    document.body.append(a);
    a.click();
    a.remove();
  }

  async function downloadClean() {
    const items = freshClean();
    if (!items.length || state.packing) return;
    if (items.length === 1) {
      triggerDownload(`/api/result/${items[0].result.id}`, items[0].result.filename);
      return;
    }
    state.packing = true;
    render();
    try {
      const zip = await api('/api/zip', { json: { ids: items.map((i) => i.result.id) } });
      triggerDownload(`/api/result/${zip.id}`, zip.filename);
      showNote(`Downloading ${zip.filename} (${bytes(zip.bytes)}). Its entries carry no dates.`, 'ok');
    } catch (err) {
      showNote(err.message, 'error');
    } finally {
      state.packing = false;
      render();
    }
  }

  function folderError(text) {
    els.folder.setAttribute('aria-invalid', 'true');
    els.folder.setAttribute('aria-describedby', 'footNote');
    showNote(text, 'error');
    els.folder.focus();
  }

  async function saveToFolder(event) {
    event.preventDefault();
    const items = freshClean();
    const folder = els.folder.value.trim();
    if (!items.length || state.saving) return;
    if (!folder) {
      folderError('Type the folder to save into, for example ~/Pictures/clean.');
      return;
    }
    state.saving = true;
    setBusy(els.saveSubmit, true);
    try {
      const res = await api('/api/save', { json: { ids: items.map((i) => i.result.id), folder } });
      state.lastFolder = folder;
      session.set(FOLDER_KEY, folder);
      els.folder.removeAttribute('aria-invalid');
      els.folder.removeAttribute('aria-describedby');
      const ok = res.files.filter((f) => f.ok);
      const bad = res.files.filter((f) => !f.ok);
      if (bad.length) {
        folderError(`Saved ${ok.length} of ${res.files.length} to ${res.folder}. ${bad.map((f) => f.message).join(' ')}`);
      } else {
        setOpen(els.save, els.saveToggle, false);
        render();
        els.saveToggle.focus();
        showNote(`Saved ${plural(ok.length, 'image')} to ${res.folder}`, 'ok', res.folder);
      }
    } catch (err) {
      folderError(err.message);
    } finally {
      state.saving = false;
      setBusy(els.saveSubmit, false);
    }
  }

  function showNote(text, kind, code) {
    const n = els.footNote;
    n.className = `foot-note is-${kind}`;
    const span = h('span');
    if (code && text.endsWith(code)) span.append(text.slice(0, -code.length), h('code', { text: code }));
    else span.textContent = text;
    n.replaceChildren(icon(kind === 'ok' ? 'check' : 'alert'), span);
    n.hidden = false;
    announce(text);
  }

  function hideNote() { els.footNote.hidden = true; }

  // ---------------------------------------------------------------- clear (two presses, no timer)

  let clearArmed = false;
  function armClear() {
    clearArmed = true;
    els.clear.classList.add('is-armed');
    $('span', els.clear).textContent = 'Clear all?';
    announce(`Press Clear again to remove all ${plural(state.items.length, 'image')} from the list.`);
  }
  function disarmClear() {
    if (!clearArmed) return;
    clearArmed = false;
    els.clear.classList.remove('is-armed');
    $('span', els.clear).textContent = 'Clear';
  }

  // ---------------------------------------------------------------- options

  function initOptions(info) {
    const select = $('#opt-format');
    for (const f of info.formats) select.append(h('option', { value: f, text: formatName(f) }));
    const form = els.optionsForm;
    for (const [k, v] of Object.entries(state.options)) {
      const field = form.elements.namedItem(k);
      if (!field) continue;
      if (field.type === 'checkbox') field.checked = !!v;
      else field.value = v;
      if (field.tagName === 'SELECT' && field.value !== String(v)) { field.value = OPTION_DEFAULTS[k]; state.options[k] = OPTION_DEFAULTS[k]; }
    }
    form.addEventListener('change', (e) => {
      const f = e.target;
      if (!f.name) return;
      const beforeKey = keysSignature();
      const beforeDeep = state.options.deep;
      state.options[f.name] = f.type === 'checkbox' ? f.checked : f.value;
      local.set(OPTIONS_KEY, JSON.stringify(persistable()));
      if (f.name === 'c2pa') syncCredentials({ focus: state.options.c2pa });
      if (f.name === 'deep' && beforeDeep !== state.options.deep) {
        // Files still waiting re-check the setting when their scan returns (see scan()).
        for (const item of state.items) if (item.state === 'scanned' || item.state === 'scanfailed') scan(item);
      }
      for (const item of state.items) renderItem(item);
      render();
      if (beforeKey !== keysSignature() && state.items.some((i) => i.state === 'clean')) {
        announce('Options changed. Clean again to apply them to the cleaned images.');
      }
    });
    form.addEventListener('submit', (e) => e.preventDefault());
  }

  // ---------------------------------------------------------------- content credentials

  const persistable = () => Object.fromEntries(Object.entries(state.options).filter(([k]) => !VOLATILE.includes(k)));

  function openCredentials() {
    setOpen(els.options, els.optionsToggle, true);
    const target = els.credIdentity.querySelector('[data-key="load"]') || els.credIdentity.querySelector('button');
    if (target) window.setTimeout(() => target.focus(), 60);
  }

  function adoptIdentity(identity) {
    state.identity = identity;
    syncCredentials();
    for (const item of state.items) renderItem(item);
    render();
  }

  function showIdentityError(text) {
    els.identityError.replaceChildren(icon('alert'), h('span', { text }));
    els.identityError.hidden = false;
  }

  function resetIdentityForm() {
    els.identityForm.reset(); // clears the passphrase and the chosen files
    els.identityError.hidden = true;
  }

  function closeIdentityForm(focusLoad) {
    resetIdentityForm();
    els.identityForm.hidden = true;
    if (focusLoad) {
      const target = els.credIdentity.querySelector('[data-key="load"]');
      if (target) target.focus();
    }
  }

  async function fileToBase64(file) {
    if (file.size > 256 * 1024) throw new Error(`${file.name} is too large to be a certificate or a key.`);
    const buf = new Uint8Array(await file.arrayBuffer());
    let bin = '';
    for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
    return btoa(bin);
  }

  async function submitIdentity(event) {
    event.preventDefault();
    if (state.loadingIdentity) return;
    const cert = els.credCert.files[0];
    if (!cert) {
      showIdentityError('Choose your certificate file first.');
      els.credCert.focus();
      return;
    }
    state.loadingIdentity = true;
    setBusy(els.identityLoad, true);
    els.identityError.hidden = true;
    let needPassphrase = false;
    try {
      const key = els.credKey.files[0];
      const anchors = els.credAnchors.files[0];
      const body = {
        cert: await fileToBase64(cert),
        key: key ? await fileToBase64(key) : null,
        anchors: anchors ? await fileToBase64(anchors) : null,
        passphrase: els.credPass.value || null,
      };
      const res = await api('/api/identity', { json: body });
      state.testRoot = null;
      closeIdentityForm(false);
      adoptIdentity(res.identity);
      announce(`Signing identity loaded: ${res.identity.signer}.`);
      const warn = res.identity.notices.find((n) => n.severity === 'warning');
      if (!warn) els.credIdentity.querySelector('button')?.focus();
    } catch (err) {
      showIdentityError(err.message);
      needPassphrase = err.code === 'passphrase';
    } finally {
      els.credPass.value = ''; // never kept longer than the request needs it
      state.loadingIdentity = false;
      setBusy(els.identityLoad, false);
    }
    if (needPassphrase) els.credPass.focus();
  }

  async function useTestIdentity() {
    try {
      const res = await api('/api/identity/test', { json: { organization: 'Test Newsroom' } });
      state.testRoot = res.root_pem;
      adoptIdentity(res.identity);
      announce('Test identity created. Credentials made with it show as Unverified to public viewers.');
    } catch (err) {
      els.identityForm.hidden = false;
      showIdentityError(err.message);
    }
  }

  async function removeIdentity() {
    try { await api('/api/identity', { method: 'DELETE' }); } catch { /* the page forgets it either way */ }
    state.testRoot = null;
    adoptIdentity(null);
    announce('Signing identity removed from memory.');
    const target = els.credIdentity.querySelector('[data-key="load"]');
    if (target) target.focus();
  }

  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    triggerDownload(url, filename);
    window.setTimeout(() => URL.revokeObjectURL(url), 4000);
  }

  async function downloadReceipts() {
    try {
      const blob = await api('/api/receipts', { json: {} });
      downloadBlob(blob, 'imgstrip-receipts.json');
      announce('Receipts downloaded. Keep them as private as the originals.');
    } catch (err) {
      showNote(err.message, 'error');
    }
  }

  function renderIdentity() {
    const box = els.credIdentity;
    const caps = state.info && state.info.credentials;
    box.replaceChildren();
    if (!caps || !caps.available) {
      box.append(note(`${(caps && caps.hint) || "Content Credentials need optional support: pip install 'imgstrip[c2pa]'"}`, 'warn'));
      return;
    }
    const id = state.identity;
    if (!id) {
      box.append(h('div', { class: 'cred-empty' },
        h('p', { text: 'No signing identity loaded.' }),
        h('button', { class: 'btn btn-secondary btn-small', type: 'button', 'data-key': 'load', onclick: () => { els.identityForm.hidden = false; els.credCert.focus(); } },
          icon('key'), h('span', { text: 'Load certificate' })),
        h('button', { class: 'btn btn-quiet btn-small', type: 'button', 'data-key': 'test', onclick: useTestIdentity }, h('span', { text: 'Try a test identity' }))));
      return;
    }
    const facts = [
      ['Signer', id.common_name && id.common_name !== id.signer ? `${id.signer} (${id.common_name})` : id.signer],
      ['Issued by', id.issuer],
      ['Valid', `${id.valid_from} to ${id.valid_until}`],
      ['Key', id.key],
      ['SHA-256', id.fingerprint],
    ];
    const kinds = { error: 'error', warning: 'warn', info: '' };
    const card = h('div', { class: 'cred-card' },
      h('div', { class: 'cred-card-head' },
        h('p', { class: 'cred-name' }, icon('key'), h('span', { text: id.signer })),
        h('button', { class: 'btn btn-quiet btn-small', type: 'button', 'data-key': 'remove', onclick: removeIdentity, 'aria-label': 'Remove the signing identity from memory' }, h('span', { text: 'Remove' }))),
      h('dl', { class: 'cred-facts' }, facts.map(([k, v]) => [h('dt', { text: k }), h('dd', { class: k === 'SHA-256' ? 'mono' : null, text: v })])),
      h('div', { class: 'cred-notes' }, id.notices.map((n) => note(n.message, kinds[n.severity]))));
    box.append(card);
    if (id.test && state.testRoot) {
      box.append(h('div', { class: 'cred-empty' },
        h('p', { text: "Add this test root to a viewer's trust anchors to see its credentials as Trusted." }),
        h('button', { class: 'btn btn-secondary btn-small', type: 'button', onclick: () => downloadBlob(new Blob([state.testRoot], { type: 'application/x-pem-file' }), 'test-root-ca.pem') },
          icon('download'), h('span', { text: 'Download test root' }))));
    }
  }

  function renderCredHints() {
    const o = state.options;
    const hints = [];
    if (!o.jpeg_lossless && !o.jpeg_canonical && !o.scrub_dct) hints.push('JPEG files are re-encoded, so their credential will say the picture is "equivalent", not "identical". Turn on Keep JPEG data bit-exact to be able to say "identical".');
    if (o.scrub_lsb || o.scrub_prnu || o.scrub_dct) hints.push('Scrubbing is on, so the credential will say the pixels were deliberately altered to protect the source.');
    if (o.c2pa_commit) hints.push('The receipt holds the secret that proves which original a signed image came from. Anyone who gets it and the original can prove that link: keep it offline and as private as the original.');
    els.credHints.replaceChildren(...hints.map((t) => note(t)));
  }

  function renderReceipts() {
    els.credReceipts.hidden = !state.receipts;
    if (state.receipts) {
      els.receiptText.textContent = `${plural(state.receipts, 'receipt')} held in memory. They are lost when you stop imgstrip-web, so download them if you will need to audit these images.`;
    }
  }

  function syncCredentials({ focus = false } = {}) {
    const caps = state.info && state.info.credentials;
    const available = !!(caps && caps.available);
    const input = els.c2paSwitch.querySelector('input');
    input.disabled = !available;
    els.c2paHint.textContent = available
      ? 'Adds a signed note (C2PA) that you removed the metadata, and whether the picture is unchanged. Optional.'
      : "Needs the optional C2PA support: pip install 'imgstrip[c2pa]'";
    const on = available && !!state.options.c2pa;
    input.checked = on;
    setOpen(els.credentials, null, on);
    renderIdentity();
    renderCredHints();
    renderReceipts();
    if (on && focus) {
      const target = els.credIdentity.querySelector('[data-key="load"]');
      if (target && !state.identity) window.setTimeout(() => target.focus(), 120);
    }
  }

  function initCredentials(info) {
    state.identity = info.identity || null;
    state.receipts = info.receipts || 0;
    els.credDate.value = state.options.c2pa_date;
    els.credDetail.value = state.options.c2pa_detail;
    els.credCommit.checked = !!state.options.c2pa_commit;
    els.credentials.addEventListener('change', (e) => {
      const f = e.target;
      if (f === els.credDate) state.options.c2pa_date = f.value;
      else if (f === els.credDetail) state.options.c2pa_detail = f.value;
      else if (f === els.credCommit) state.options.c2pa_commit = f.checked;
      else return;
      local.set(OPTIONS_KEY, JSON.stringify(persistable()));
      renderCredHints();
      for (const item of state.items) renderItem(item);
      render();
    });
    els.identityForm.addEventListener('submit', submitIdentity);
    $('#identityCancel').addEventListener('click', () => closeIdentityForm(true));
    $('#receiptDownload').addEventListener('click', downloadReceipts);
    syncCredentials();
  }

  // ---------------------------------------------------------------- brand marks

  function drawMarks() {
    const fixed = 'b3f19a07c2d4e58a61f0c3b7d9e2a4f6c8b1d3e5f7a9c0b2d4e6f8a1c3e5b7d9';
    $('#mark').append(sealSVG(fixed, { detail: false, check: false }));
    $('#wellMark').append(sealSVG(fixed, { detail: true, check: false }));
    $('#gateMark').append(sealSVG(fixed, { detail: true, check: false }));
  }

  // ---------------------------------------------------------------- events

  function bind() {
    els.choose.addEventListener('click', () => els.picker.click());
    $('#add').addEventListener('click', () => els.picker.click());
    els.picker.addEventListener('change', () => { addFiles(els.picker.files); els.picker.value = ''; });

    els.clear.addEventListener('click', () => {
      if (clearArmed) { disarmClear(); clearAll(); } else armClear();
    });
    els.clear.addEventListener('blur', disarmClear);

    els.optionsToggle.addEventListener('click', () => {
      setOpen(els.options, els.optionsToggle, els.options.dataset.open !== 'true');
    });
    els.reviewToggle.addEventListener('click', () => setReviewOpen(!state.review.open));
    els.limitsToggle.addEventListener('click', () => {
      setOpen(els.limits, els.limitsToggle, els.limits.dataset.open !== 'true');
    });

    els.primary.addEventListener('click', () => {
      if (isBusy(els.primary)) return;
      const mode = els.primary.dataset.mode;
      if (mode === 'clean') cleanAll();
      else if (mode === 'download') downloadClean();
    });
    els.downloadSome.addEventListener('click', () => { if (!isBusy(els.downloadSome)) downloadClean(); });
    els.saveToggle.addEventListener('click', () => {
      const open = els.save.dataset.open !== 'true';
      setOpen(els.save, els.saveToggle, open);
      render();
      if (open) {
        if (!els.folder.value) els.folder.value = state.lastFolder || state.info?.folder || '';
        window.setTimeout(() => els.folder.focus(), 60);
      }
    });
    els.saveForm.addEventListener('submit', saveToFolder);
    els.folder.addEventListener('input', () => els.folder.removeAttribute('aria-invalid'));

    // A link pasted into this tab only changes the fragment: reload so it is redeemed.
    window.addEventListener('hashchange', () => { if (/[#&]k=/.test(window.location.hash)) window.location.reload(); });
    // The seal tooltip hides on Escape until focus or the pointer moves on.
    document.addEventListener('focusin', () => document.body.classList.remove('tips-off'));
    els.files.addEventListener('mouseout', (e) => { if (e.target.closest && e.target.closest('.status')) document.body.classList.remove('tips-off'); });

    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      const active = document.activeElement;
      if (!document.body.classList.contains('tips-off') && document.querySelector('.seal-tip')) {
        document.body.classList.add('tips-off');
        if (active && active.classList.contains('file-head')) return;
      }
      if (clearArmed) { disarmClear(); return; }
      if (state.review.open && els.review.contains(active)) {
        setReviewOpen(false);
        els.reviewToggle.focus();
      } else if (els.save.dataset.open === 'true' && els.foot.contains(active)) {
        setOpen(els.save, els.saveToggle, false);
        render();
        els.saveToggle.focus();
      } else if (els.options.dataset.open === 'true' && (els.options.contains(active) || active === els.optionsToggle)) {
        setOpen(els.options, els.optionsToggle, false);
        els.optionsToggle.focus();
      }
    });

    // drag and drop anywhere on the page
    let depth = 0;
    const hasFiles = (e) => Array.from(e.dataTransfer?.types || []).includes('Files');
    document.addEventListener('dragenter', (e) => {
      if (!hasFiles(e) || state.gate) return;
      e.preventDefault();
      depth++;
      document.body.classList.add('is-dragging');
    });
    document.addEventListener('dragover', (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = state.gate ? 'none' : 'copy';
    });
    document.addEventListener('dragleave', (e) => {
      if (!hasFiles(e)) return;
      depth = Math.max(0, depth - 1);
      if (!depth) document.body.classList.remove('is-dragging');
    });
    document.addEventListener('drop', (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth = 0;
      document.body.classList.remove('is-dragging');
      addFiles(e.dataTransfer.files);
    });
    document.addEventListener('paste', (e) => {
      const files = Array.from(e.clipboardData?.files || []).filter((f) => f.type.startsWith('image/'));
      if (files.length && !(e.target instanceof HTMLInputElement)) {
        e.preventDefault();
        addFiles(files);
      }
    });
  }

  async function init() {
    drawMarks();
    bind();
    state.lastFolder = session.get(FOLDER_KEY);
    if (!(await authenticate())) return;
    try {
      state.info = await api('/api/info', { method: 'GET' });
    } catch (err) {
      if (!state.gate) showGate(err.status ? 'expired' : 'down');
      return;
    }
    initOptions(state.info);
    initCredentials(state.info);
    render();
  }

  init();
})();
