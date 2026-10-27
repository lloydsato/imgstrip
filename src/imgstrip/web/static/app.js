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
  const OPTION_DEFAULTS = {
    format: '', quality: '', icc: 'convert',
    deep: false, jpeg_lossless: false, scrub_lsb: false, scrub_alpha: false, scrub_dct: false, jpeg_canonical: false, scrub_prnu: false, auto_orient: true, optimize: false, random_name: false,
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
    deep: 'Deep scan', jpeg_lossless: 'Keep JPEG data bit-exact', scrub_lsb: 'Scrub least-significant bits', scrub_alpha: 'Clean transparency channel',
    scrub_dct: 'Scrub JPEG coefficients', jpeg_canonical: 'Rebuild JPEG data canonically', scrub_prnu: 'Disrupt camera fingerprint',
    c2pa: 'Sign with Content Credentials', quality: 'Quality', icc: 'Colour profile', auto_orient: 'Apply orientation',
    optimize: 'Smaller PNGs', random_name: 'Random file names', c2pa_commit: 'Make it auditable later',
  };
  // What each switch does, in plain words, for its (i) button. Written for someone who has never heard of the jargon.
  const INFO = {
    deep: 'Looks inside the picture itself for a secret message, not only at the labels stuck to the file. It takes a little longer. If it finds nothing, that is not proof that nothing is hidden.',
    scrub_lsb: 'Some people hide a secret message by changing the tiniest bit of every colour dot in a picture. This switch shuffles those tiny bits, which destroys such a message. The picture looks the same to your eyes. It only covers this one way of hiding things.',
    scrub_alpha: 'Some pictures have see-through parts, and a message can be hidden in how see-through each dot is. This switch turns almost-clear dots fully clear and almost-solid dots fully solid, which wipes that message. Pictures with no see-through parts are left alone.',
    scrub_prnu: 'Every camera sensor leaves a faint pattern in its photos, a bit like a fingerprint. This switch nudges parts of the picture by a few dots to blur that pattern, so the photo is harder to tie to one camera. It changes the picture very slightly, and nobody can prove it worked.',
    random_name: 'File names like IMG_20240517_133700 can tell people when a photo was taken. This gives each cleaned file a random name instead.',
    c2pa: 'Adds a signed note to the cleaned file. The note says you removed the hidden information and whether the picture itself is unchanged. It does not say who took the photo or that it is real. You need your own certificate to sign.',
    c2pa_commit: 'Locks a secret fingerprint of the original photo inside the note, so that later you can prove which original a cleaned photo came from. Only the receipt you download can open it, and it is lost when you close imgstrip unless you save it.',
    jpeg_lossless: 'Keeps the JPEG’s picture data exactly as it is and only strips the labels around it. Nothing is re-saved, so quality stays the same. But anything hidden inside the picture data stays too, and the picture can’t be turned upright.',
    scrub_dct: 'For JPEG photos. Slightly changes the numbers JPEG uses to store the picture, which destroys most messages hidden there. The photo may lose a little quality, and the result tells you how much.',
    jpeg_canonical: 'For JPEG photos. Writes the file again in one standard, tidy way, which removes anything tucked into odd corners, padding or spare bits. The picture stays exactly the same. Messages hidden in the picture’s own numbers are not removed.',
    auto_orient: 'Some photos are stored sideways and rely on a small note to be shown upright. Cleaning removes that note, so this turns the picture itself upright first. It is best left on.',
    optimize: 'Tries harder to make PNG files smaller. It takes longer, and the picture is not changed.',
  };
  const NARROW = window.matchMedia('(max-width: 899.98px)');
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
    selected: null, // the item whose details are shown
    sheet: false, // narrow screens: the detail is open as a full-screen sheet
  };

  const els = {
    window: $('#window'), well: $('#well'), list: $('#list'), files: $('#files'), listCount: $('#listCount'),
    listFound: $('#listFound'), workspace: $('#body'), listPane: $('#listPane'), formats: $('#formats'), steps: $('#steps'),
    detail: $('#detail'), detailContent: $('#detailContent'), sheetBack: $('#sheetBack'), infoTip: $('#infoTip'),
    localAddr: $('#localAddr'),
    foot: $('#foot'), summary: $('#summary'), primary: $('#primary'), downloadSome: $('#downloadSome'),
    saveToggle: $('#saveToggle'), save: $('#save'), saveForm: $('#saveForm'), saveSubmit: $('#saveSubmit'),
    folder: $('#folder'), footNote: $('#footNote'), picker: $('#picker'), options: $('#options'),
    optionsToggle: $('#optionsToggle'), optionsForm: $('#optionsForm'), limits: $('#limits'),
    limitsToggle: $('#limitsToggle'), clear: $('#clear'), hashList: $('#hashList'), choose: $('#choose'), announce: $('#announce'),
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
      scrub_alpha: !!o.scrub_alpha,
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
    els.formats.hidden = true;
    els.steps.hidden = true;
    els.list.hidden = true;
    els.foot.hidden = true;
    els.workspace.dataset.empty = 'true';
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
    if (!state.selected) selectItem(added[0]);
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
    const next = state.items[index] || state.items[index - 1] || null;
    if (state.selected === item) {
      state.selected = next;
      if (next) renderSelection(next);
      renderDetail();
    }
    setSheet(false);
    render();
    if (next) next.ui.head.focus({ preventScroll: true });
    else els.choose.focus();
    announce(`Removed ${item.name}. ${plural(state.items.length, 'image')} left.`);
  }

  function clearAll() {
    state.epoch++;
    state.run = null;
    for (const item of state.items) if (item.thumbUrl) URL.revokeObjectURL(item.thumbUrl);
    state.items = [];
    state.selected = null;
    setSheet(false);
    refill(els.detailContent, []);
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

  // Rebuild a container's children without dropping keyboard focus: the focused control is found again by its data-key.
  function refill(container, nodes) {
    const active = document.activeElement;
    const key = active && container.contains(active) ? active.dataset.key : null;
    container.replaceChildren(...nodes);
    if (key) {
      const again = container.querySelector(`[data-key="${CSS.escape(key)}"]`);
      if (again) again.focus({ preventScroll: true });
    }
  }

  function mountItem(item) {
    const ui = {};
    ui.thumb = h('span', { class: 'thumb' }, icon('image'));
    ui.name = h('span', { class: 'file-name' });
    ui.head = h('button', {
      class: 'file-head', type: 'button', 'aria-controls': 'detail',
      onclick: () => selectItem(item, { open: true }),
      onkeydown: (e) => rowKey(e, item),
    }, ui.name);
    ui.meta = h('p', { class: 'file-meta' });
    ui.note = h('div', { class: 'file-more-note' });
    ui.fps = h('div', { class: 'fps' });
    ui.more = h('div', { class: 'file-more' }, ui.note, ui.fps);
    ui.seal = h('span', { class: 'st-seal' });
    ui.main = h('span', { class: 'st-main' });
    ui.sub = h('span', { class: 'st-sub' });
    ui.progress = h('span', { class: 'progress', role: 'progressbar', 'aria-label': 'Working', hidden: true });
    ui.download = h('a', { class: 'st-link is-off', tabindex: '-1', 'aria-hidden': 'true' }, 'Download');
    ui.side = h('div', { class: 'file-side' }, ui.seal, h('div', { class: 'st-text' }, ui.main, ui.sub, ui.progress, ui.download));
    item.ui = ui;
    item.el = h('li', { class: 'file', 'data-state': item.state, 'data-selected': 'false' },
      h('div', { class: 'file-row' }, ui.thumb, h('div', { class: 'file-id' }, h('h3', { class: 'file-title' }, ui.head), ui.meta), ui.side, ui.more));
    els.files.append(item.el);
    renderItem(item);
  }

  // Arrow keys step through the rows, so a long list is not a long run of Tab presses.
  function rowKey(e, item) {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    const at = state.items.indexOf(item);
    const next = state.items[at + (e.key === 'ArrowDown' ? 1 : -1)];
    if (!next) return;
    e.preventDefault();
    next.ui.head.focus();
  }

  // The plain phrase for each kind of thing a file can give away: a short one for the row, and the half-sentence the
  // detail's lede is built from. `shows` things are read off the picture's labels, `carries` ones are hidden inside the file.
  const REVEAL_TAG = {
    location: 'Where it was taken', camera: 'Camera', lens: 'Camera', time: 'When it was taken', gps: 'Where it was taken',
    serial: 'Camera serial number', person: "A person's name", software: 'Editing software', comment: 'A comment',
    preview: 'Hidden preview image', trailer: 'Extra data after the image',
  };
  const REVEAL_LEDE = {
    location: ['shows', 'where it was taken'], gps: ['shows', 'where it was taken'],
    camera: ['shows', 'which camera took it'], lens: ['shows', 'which camera took it'], time: ['shows', 'when'],
    serial: ['shows', "the camera's serial number"], person: ['shows', "a person's name"],
    software: ['shows', 'what software was used'], comment: ['shows', 'a written comment'],
    preview: ['carries', 'a hidden preview image'], trailer: ['carries', 'extra data tacked on after the picture'],
  };
  const uniq = (list) => [...new Set(list)];

  // What a file without readable labels still holds, as short nouns ("EXIF data", "a colour profile").
  function findingNouns(r) {
    const nouns = [];
    for (const f of r.findings) {
      const noun = f.category === 'color' ? 'a colour profile'
        : f.category === 'hidden' ? 'hidden data'
          : f.category === 'structure' ? 'unknown structures'
            : /^([A-Z0-9]{2,}|Photoshop|Adobe|Apple|Google|Samsung|Microsoft|Netscape)\b/.test(f.label) ? `${f.label.replace(/ \(.*\)$/, '')}${/^(EXIF|XMP|IPTC|TIFF)\b/.test(f.label) ? ' data' : ''}`
              : f.label.charAt(0).toLowerCase() + f.label.slice(1).replace(/ \(.*\)$/, '');
      if (!nouns.includes(noun)) nouns.push(noun);
    }
    return nouns;
  }

  // The short phrases shown under a row's name: [{text, kind}], at most three and a count of the rest.
  function rowTags(r) {
    const tags = [];
    if (hasStego(r)) tags.push({ text: 'Hidden data in the pixels', kind: 'leak' });
    if (r.reveals.length) {
      const keys = r.reveals.map((x) => x.key);
      const both = keys.includes('camera') && keys.includes('time');
      for (const x of r.reveals) {
        const text = both && (x.key === 'camera' || x.key === 'lens' || x.key === 'time') ? 'Camera and time' : REVEAL_TAG[x.key] || capital(x.label.toLowerCase());
        if (!tags.some((t) => t.text === text)) tags.push({ text, kind: 'leak' });
      }
    } else {
      for (const noun of findingNouns(r)) tags.push({ text: capital(noun), kind: 'other' });
    }
    return tags;
  }

  function itemMeta(item) {
    const r = item.report;
    if (item.state === 'clean' && item.result) {
      const res = item.result;
      const frames = res.frames > 1 ? ` · ${res.frames} frames` : '';
      return `${formatName(res.output_format)} · ${res.size[0]}×${res.size[1]}${frames} · ${bytes(res.bytes_out)}${res.credentials ? ' · signed' : ''}`;
    }
    const parts = [];
    const fmt = r && (r.format || r.decodable);
    if (fmt) parts.push(formatName(fmt));
    else if (extOf(item.name)) parts.push(extOf(item.name));
    if (r && r.size) parts.push(`${r.size[0]}×${r.size[1]}`);
    parts.push(bytes(item.size));
    return parts.join(' · ');
  }

  const joinNames = (names) => (names.length > 1 ? `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}` : names[0] || '');

  // The line (or phrases) under the name: what the file gives away, or what cleaning did.
  function itemNote(item) {
    const r = item.report;
    const text = (t, cls = '') => [h('p', { class: `file-text${cls}`, text: t })];
    switch (item.state) {
      case 'scanning': return text('Looking inside the file…');
      case 'cleaning': return text('Rebuilding the file from its pixels…');
      case 'scanfailed': case 'failed': return text(item.error || '', ' is-warn');
      case 'unsupported': return text(item.blocked || (r && r.raw ? `Camera RAW (${r.raw}) isn't supported` : "Not an image imgstrip can read"));
      case 'clean': {
        const res = item.result;
        if (refused(item)) return text('Cleaning again failed. This is the earlier clean file.', ' is-warn');
        const labels = uniq(res.removed.map((f) => f.label.replace(/ \(.*\)$/, '')));
        const removed = labels.length
          ? `Removed ${labels.length > 3 ? `${labels.slice(0, 3).join(', ')} and ${labels.length - 3} more` : joinNames(labels)}.`
          : 'Rebuilt from the pixels alone.';
        if (keptPayload(item)) return text(`${removed} A likely payload is still in the pixels.`, ' is-warn');
        if (unappliedScrub(item)) return text(`${removed} The pixel scrub could not be applied.`, ' is-warn');
        return text(`${removed} Both checks passed.${res.key !== optionsKey(item) ? ' Cleaned with earlier options.' : ''}`);
      }
      default: {
        if (!r) return [];
        const tags = rowTags(r);
        if (tags.length) {
          const shown = tags.slice(0, 3);
          const rest = tags.length - shown.length;
          return [h('ul', { class: 'tags' },
            shown.map((t) => h('li', { class: 'tag', 'data-kind': t.kind, text: t.text })),
            rest > 0 ? h('li', { class: 'tag tag-more', text: `+${rest} more` }) : null)];
        }
        if (isDamaged(r)) return text('The file is damaged. Cleaning rebuilds it if its pixels can be read.', ' is-warn');
        const v = scanVerdict(r);
        return text(v && v.state === 'not_detected' ? 'Nothing found. Not detected in the pixels.' : 'Nothing found. The pixels were not scanned.');
      }
    }
  }

  // What the row's right-hand side says: a seal, a headline and a small line under it, or a working bar.
  function itemStatus(item) {
    const r = item.report;
    const warn = (t, sub) => ({ icon: 'alert', main: t, sub });
    switch (item.state) {
      case 'scanning': return { main: 'Scanning…', busy: true };
      case 'cleaning': return { main: 'Cleaning…', busy: true };
      case 'scanfailed': return warn('Not scanned');
      case 'failed': return warn('Not cleaned');
      case 'unsupported': return { main: 'Not supported' };
      case 'clean': {
        const res = item.result;
        const stale = res.key !== optionsKey(item);
        const anim = item.justCleaned;
        item.justCleaned = false;
        if (refused(item)) return { ...warn('Re-clean failed'), download: true };
        if (keptPayload(item)) return { ...warn('Payload kept'), download: true };
        if (unappliedScrub(item)) return { ...warn('Scrub not applied'), download: true };
        return { seal: seal(res.sha256, { detail: false, animate: anim }), main: stale ? 'Clean, old options' : 'Verified', download: true };
      }
      default: {
        const scrub = optionsFor(item).scrub_lsb;
        if (hasStego(r) && !scrub) return warn('Payload in pixels');
        const n = findingCount(r);
        if (n) return { main: plural(n, 'item'), sub: 'found' };
        if (isDamaged(r)) return warn('Damaged');
        if (r && !r.format) return { main: 'Not inspected' };
        return { main: 'No metadata', sub: 'found' };
      }
    }
  }

  // What kind of thing the row's status is, so it can be coloured by meaning: busy, muted (neutral), leak (the
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

  // ---- fingerprints: SHA-256 is the proof, MD5 is only there to match older records ----

  const ALGOS = [['SHA-256', 'sha256'], ['MD5', 'md5']];
  const shortHash = (hex) => (hex.length > 20 ? `${hex.slice(0, 8)}…${hex.slice(-8)}` : hex);
  const groupHash = (hex) => (hex.match(/.{1,8}/g) || []).join(' ');

  // The hashes a row shows: the file as received, and (once cleaned) the new file beside it.
  function fingerprintsOf(item) {
    if (item.state === 'clean' && item.result && item.result.before && item.result.after) {
      return { before: item.result.before, after: item.result.after };
    }
    const fp = item.report && item.report.fingerprints;
    return fp ? { before: fp, after: null } : null;
  }

  function fpLine(item, fp, which, tag) {
    const when = which === 'after' ? 'after cleaning' : 'before cleaning';
    return h('div', { class: `fps-line${tag ? ' is-tagged' : ''}` },
      tag ? h('span', { class: 'fp-tag', text: tag }) : null,
      ALGOS.map(([label, key]) => h('span', { class: 'fp' },
        h('span', { class: 'fp-algo', text: label }),
        h('code', { text: shortHash(fp[key]) }),
        h('button', {
          class: 'copy', type: 'button', 'data-key': `copy-${which}-${key}`,
          'aria-label': `Copy ${label} of ${item.name}, ${when}`,
          onclick: (e) => copyText(fp[key], e.currentTarget, `${label} ${when}`),
        }, icon('copy')))));
  }

  function renderFps(item) {
    const fps = fingerprintsOf(item);
    const sig = fps ? `${item.name}|${fps.before.sha256}|${fps.after ? fps.after.sha256 : ''}` : '';
    if (item.ui.fpsSig === sig) return;
    item.ui.fpsSig = sig;
    if (!fps) { refill(item.ui.fps, []); return; }
    refill(item.ui.fps, fps.after
      ? [fpLine(item, fps.before, 'before', 'Before'), fpLine(item, fps.after, 'after', 'After')]
      : [fpLine(item, fps.before, 'before', '')]);
  }

  function renderSelection(item) {
    if (!item.el) return;
    const selected = state.selected === item;
    item.el.dataset.selected = String(selected);
    if (selected) item.ui.head.setAttribute('aria-current', 'true');
    else item.ui.head.removeAttribute('aria-current');
  }

  function renderItem(item) {
    const { el, ui } = item;
    if (!el) return;
    el.dataset.state = item.state;
    renderSelection(item);
    ui.name.textContent = item.name;
    const meta = itemMeta(item);
    ui.meta.textContent = meta;
    const tone = toneOf(item);
    el.dataset.tone = tone;
    ui.side.dataset.tone = tone;

    const st = itemStatus(item);
    ui.side.dataset.busy = String(!!st.busy);
    ui.seal.replaceChildren(...(st.seal ? [st.seal] : []));
    ui.main.replaceChildren(...(st.icon ? [icon(st.icon)] : []), st.main);
    ui.sub.textContent = st.sub || '';
    ui.progress.hidden = !st.busy;
    el.setAttribute('aria-busy', String(!!st.busy));
    refill(ui.note, itemNote(item));
    renderFps(item);
    ui.more.hidden = !ui.note.childNodes.length && !ui.fps.childNodes.length;
    ui.head.setAttribute('aria-label', `${item.name}. ${st.main}${st.sub ? ` ${st.sub}` : ''}. ${meta}`);

    // The download link persists: only its state changes, so keyboard focus never falls off it.
    const res = item.result;
    const dl = ui.download;
    if (res && st.download) {
      dl.classList.remove('is-off');
      dl.href = `/api/result/${res.id}`;
      dl.setAttribute('download', res.filename);
      dl.setAttribute('aria-label', `Download ${res.filename}`);
      dl.removeAttribute('tabindex');
      dl.removeAttribute('aria-hidden');
    } else if (!dl.classList.contains('is-off')) {
      const hadFocus = document.activeElement === dl;
      dl.classList.add('is-off');
      dl.removeAttribute('href');
      dl.setAttribute('tabindex', '-1');
      dl.setAttribute('aria-hidden', 'true');
      if (hadFocus) ui.head.focus();
    }
    if (state.selected === item) renderDetail();
  }

  // ---------------------------------------------------------------- selecting a file and the detail pane

  // The pieces of the page a full-screen detail sheet covers: they are inert while it is open.
  const behindSheet = () => [$('.bar'), els.options, els.listPane, els.foot];

  function setSheet(open) {
    const on = open && NARROW.matches;
    state.sheet = on;
    els.detail.dataset.sheet = on ? 'open' : 'closed';
    if (on) { els.detail.setAttribute('role', 'dialog'); els.detail.setAttribute('aria-modal', 'true'); } else { els.detail.removeAttribute('role'); els.detail.removeAttribute('aria-modal'); }
    document.body.classList.toggle('sheet-open', on);
    for (const part of behindSheet()) part.inert = on;
  }

  function openSheet() {
    setSheet(true);
    if (!state.sheet) return;
    const title = $('#detailTitle');
    (title || els.detailContent).focus({ preventScroll: true });
  }

  function closeSheet() {
    if (!state.sheet) return;
    setSheet(false);
    const item = state.selected;
    if (item && item.ui) item.ui.head.focus({ preventScroll: true });
  }

  function selectItem(item, { open = false } = {}) {
    const previous = state.selected;
    state.selected = item;
    if (previous && previous !== item) renderSelection(previous);
    renderSelection(item);
    renderDetail();
    if (previous !== item) announce(`Showing details for ${item.name}.`);
    if (open && NARROW.matches) openSheet();
  }

  function renderDetail() {
    const item = state.selected;
    refill(els.detailContent, item ? detailContent(item) : []);
  }

  // ---------------------------------------------------------------- the detail pane

  // What a finding means, in plain words, for the ones whose name alone would not say.
  const WHY = [
    [/thumbnail/i, 'A small copy of the photo. It can show the uncropped original.'],
    [/c2pa|jumbf/i, 'A signed note about where this picture came from. Cleaning removes it.'],
    [/^data after|^appended|trailing/i, 'Extra bytes tacked on after the picture. They can hide a file or an older version of the image.'],
    [/^unknown (chunk|segment|box|tag|marker)/i, 'A part of the file imgstrip does not recognise. It could hold anything.'],
    [/^xmp/i, 'Extra notes added by editing and photo apps.'],
    [/iptc|photoshop/i, 'Captions, credits and keywords added by photo software.'],
    [/icc/i, 'Describes the screen the colours were made for. Some profiles carry a device or a user name.'],
    [/modification time/i, 'When the file was last edited.'],
    [/dpi|physical pixel/i, 'A print-size setting. It can hint at the device that made the file.'],
  ];

  // A finding's detail, split for display: a size or coordinates on the right, the rest as text underneath.
  function describeFinding(f) {
    const d = f.detail || '';
    const out = { side: '', leak: false, text: '', why: '' };
    const size = /^([\d,]+) bytes?(?:[;\s]+(.*))?$/.exec(d);
    if (size) {
      out.side = `${Number(size[1].replace(/,/g, '')).toLocaleString('en-US')} bytes`;
      out.text = (size[2] || '').replace(/^\((.*)\)$/, '$1');
    } else if (/^-?\d+(\.\d+)?, -?\d+(\.\d+)?$/.test(d)) {
      out.side = d;
      out.leak = true;
      out.why = 'Not shown on a map. This app never goes online.';
    } else {
      out.text = d === f.label ? '' : d;
    }
    if (!out.why && !/^EXIF/i.test(f.label)) {
      const hit = WHY.find(([re]) => re.test(f.label));
      if (hit) out.why = hit[1];
    }
    return out;
  }

  function findingItem(f) {
    const d = describeFinding(f);
    return h('li', { class: 'd-item' },
      h('div', { class: 'd-row' },
        h('span', { class: 'd-label', text: f.label }),
        d.side ? h('span', { class: `d-side${d.leak ? ' is-leak' : ''}`, text: d.side }) : null),
      d.text ? h('p', { class: 'd-text', text: d.text }) : null,
      d.why ? h('p', { class: 'd-text', text: d.why }) : null);
  }

  // A headed block with a count on the right. `count` is a string, or a node for richer states.
  function detailSection(title, count, body, { cls = '', tone = '' } = {}) {
    return h('section', { class: `d-sec${cls ? ` ${cls}` : ''}` },
      h('header', { class: 'd-head' }, h('h3', { text: title }), count === null ? null : h('p', { class: `d-count${tone ? ` is-${tone}` : ''}` }, count)),
      body);
  }

  // Findings by kind. A scanned file always lists the three usual kinds (saying "None found" when empty); a cleaned one
  // lists only what was removed.
  function findingSections(findings, { removed = false } = {}) {
    const groups = new Map();
    for (const f of findings) {
      if (!groups.has(f.category)) groups.set(f.category, []);
      groups.get(f.category).push(f);
    }
    const always = removed ? [] : ['metadata', 'hidden', 'color'];
    const order = [...new Set([...CATEGORY_ORDER.filter((k) => always.includes(k) || groups.has(k)), ...groups.keys()])];
    return order.map((k) => {
      const list = groups.get(k) || [];
      const count = list.length ? (removed ? `${list.length} removed` : plural(list.length, 'item')) : 'None found';
      return detailSection(CATEGORY[k] || capital(k), count, list.length ? h('ul', { class: 'd-list' }, list.map(findingItem)) : null);
    });
  }

  // The plain-language opening: what this file gives away, with the important phrases in bold.
  function ledeFor(item) {
    const r = item.report;
    const bold = (t) => h('strong', { text: t });
    const list = (parts) => parts.flatMap((p, i) => [i === 0 ? '' : i === parts.length - 1 ? ' and ' : ', ', bold(p)]);
    const shows = uniq(r.reveals.map((x) => REVEAL_LEDE[x.key]).filter((p) => p && p[0] === 'shows').map((p) => p[1]));
    const carries = uniq(r.reveals.map((x) => REVEAL_LEDE[x.key]).filter((p) => p && p[0] === 'carries').map((p) => p[1]));
    const photo = r.reveals.some((x) => ['camera', 'lens', 'location', 'gps', 'time'].includes(x.key));
    const noun = photo ? 'photo' : 'image';
    if (shows.length) {
      return h('p', { class: 'd-lede' }, `This ${noun} shows `, ...list(shows), '.', carries.length ? [' It also carries ', ...list(carries), '.'] : '');
    }
    if (carries.length) return h('p', { class: 'd-lede' }, `This ${noun} carries `, ...list(carries), '.');
    if (r.findings.length) return h('p', { class: 'd-lede' }, 'This image holds ', ...list(findingNouns(r).slice(0, 4)), '.');
    if (!r.format) return h('p', { class: 'd-lede', text: "imgstrip can't list what this kind of file holds, but cleaning rebuilds the image from its pixels alone." });
    return h('p', { class: 'd-lede', text: "Nothing beside the picture was found in this file's structure." });
  }

  // The pixel-level verdict, in the server's words. Shown with its caveat every time, so a scan that found
  // nothing is never read as proof that nothing is hidden.
  function pixelSection(item, verdict) {
    const v = verdict;
    if (!v) return null;
    const ring = (solid) => h('span', { class: `ring${solid ? ' is-solid' : ''}`, 'aria-hidden': 'true' });
    const word = { not_scanned: 'Not scanned', detected: 'Payload found', not_detected: 'Nothing detected', scrubbed: 'Scrubbed' }[v.state] || capital(v.headline);
    const tone = v.state === 'detected' ? 'leak' : 'strong';
    const body = [];
    const canScan = !item.report.deep && item.state === 'scanned' && supportOf(item).deep.state === 'ok';
    if (v.state === 'not_scanned') {
      body.push(h('p', { class: 'd-text', text: "Only the file's structure was read. Nothing was tested in the pixels, so this says nothing about them." }));
      body.push(h('div', { class: 'd-tools' },
        canScan ? h('button', {
          class: 'btn btn-secondary btn-small', type: 'button', 'data-key': 'deep',
          onclick: () => { item.deepOnce = true; scan(item); },
        }, 'Scan the pixels too') : null,
        h('p', { class: 'd-text', text: 'A scan that finds nothing is never proof that nothing is hidden.' })));
    } else {
      body.push(note(`${capital(v.headline)}. ${capital(v.summary)}.`, v.state === 'detected' ? 'warn' : ''));
      body.push(note(v.caveat));
    }
    return detailSection('In the pixels', [ring(v.state === 'detected'), word], h('div', { class: 'd-body' }, ...body), { tone });
  }

  // File fingerprints. Scanned: the file as received. Cleaned: before and after, side by side.
  function hashBlock(item, name, hex, key) {
    return h('div', { class: 'hash-block' },
      h('h4', { text: name }),
      h('div', { class: 'hash-line' },
        h('code', { class: 'hash-code', 'data-hash': key, text: groupHash(hex), oncopy: copyPlain }),
        h('button', {
          class: 'copy-box', type: 'button', 'data-key': `copy-${key}`,
          'aria-label': `Copy ${name} of ${item.name}${key.startsWith('after') ? ', after cleaning' : key.startsWith('before') ? ', before cleaning' : ''}`,
          onclick: (e) => copyText(hex, e.currentTarget, name),
        }, icon('copy'))));
  }

  // What a pasted hash says about a file: a SHA-256 is 64 hex characters and an MD5 is 32, and only a whole one can
  // match. `fps` is { before, after|null }.
  const HASH_LABEL = /^(?:sha-?256|md5)\s*[:=]\s*/i;
  function checkHash(raw, fps) {
    const typed = String(raw || '').trim();
    if (!typed) return { kind: 'empty', hint: '', text: '' };
    const v = typed.replace(HASH_LABEL, '').replace(/\s+/g, '').toLowerCase();
    if (!/^[0-9a-f]+$/.test(v)) return { kind: 'bad', hint: 'A hash only has the digits 0 to 9 and the letters a to f.', text: '' };
    const algo = v.length === 64 ? ['SHA-256', 'sha256'] : v.length === 32 ? ['MD5', 'md5'] : null;
    if (!algo) return { kind: 'short', hint: `A SHA-256 has 64 characters and an MD5 has 32. This has ${v.length}.`, text: '' };
    const [label, key] = algo;
    const hits = [];
    if (fps.before[key] === v) hits.push(fps.after ? 'the file as you added it' : 'this file');
    if (fps.after && fps.after[key] === v) hits.push('the cleaned file');
    if (!hits.length) return { kind: 'none', hint: '', text: `No match. This is not the ${label} of ${fps.after ? 'this file, before or after cleaning' : 'this file'}.` };
    return { kind: 'match', hint: '', text: `Match. This is the ${label} of ${hits.join(' and ')}.${key === 'md5' ? ' MD5 can be forged, so take this as a hint, not proof.' : ''}` };
  }

  // A box to paste a hash from an older record or a download page. Its text is kept on the item so a redraw does not lose it.
  function hashCheckBlock(item, fps) {
    const hint = h('p', { class: 'hash-hint', id: 'hashHint' });
    const result = h('p', { class: 'hash-result', id: 'hashResult', role: 'status' });
    const input = h('input', {
      type: 'text', id: 'hashCheck', 'data-key': 'hash-check', spellcheck: 'false', autocomplete: 'off', autocapitalize: 'off',
      placeholder: 'Paste a SHA-256 or MD5', 'aria-describedby': 'hashHint',
    });
    const paint = () => {
      const r = checkHash(item.hashCheck, fps);
      hint.textContent = r.hint;
      result.dataset.kind = r.kind;
      result.replaceChildren(...(r.text ? [icon(r.kind === 'match' ? 'check' : 'alert'), h('span', { text: r.text })] : []));
      if (r.kind === 'bad') input.setAttribute('aria-invalid', 'true'); else input.removeAttribute('aria-invalid');
    };
    input.value = item.hashCheck || '';
    input.addEventListener('input', () => { item.hashCheck = input.value; paint(); });
    paint();
    return h('div', { class: 'hash-check' }, h('label', { for: 'hashCheck', text: 'Compare with a hash you already have' }), input, hint, result);
  }

  function fingerprintSection(item) {
    const fps = fingerprintsOf(item);
    if (!fps) return null;
    const blocks = (fp, which) => ALGOS.map(([label, key]) => hashBlock(item, label, fp[key], `${which}-${key}`));
    if (!fps.after) {
      return detailSection('File fingerprints', 'As received', [
        ...blocks(fps.before, 'before'),
        h('p', { class: 'hash-note', text: 'MD5 is here to match older records. Rely on SHA-256 as proof. After cleaning, the new file gets its own hashes.' }),
        hashCheckBlock(item, fps),
      ]);
    }
    const same = fps.before.sha256 === fps.after.sha256;
    return detailSection('File fingerprints', 'Before and after', [
      h('div', { class: 'hash-set', 'data-set': 'before' },
        h('p', { class: 'hash-set-head' }, h('strong', { text: 'Before cleaning' }), h('span', { text: 'as received' })), ...blocks(fps.before, 'before')),
      h('div', { class: 'hash-set', 'data-set': 'after' },
        h('p', { class: 'hash-set-head' }, h('strong', { text: 'After cleaning' }), h('span', { text: 'the new file' })), ...blocks(fps.after, 'after')),
      h('p', { class: 'hash-note', text: same
        ? 'The two SHA-256 values match, so the new file is byte for byte the file you added: it had nothing to remove.'
        : 'The values differ because the file was rebuilt. That is expected. The two checks above are what show the picture is right. MD5 is here to match older records; rely on SHA-256 as proof.' }),
      hashCheckBlock(item, fps),
    ]);
  }

  // ---- the hash list: every file's hashes as a spreadsheet (CSV), built here and never sent anywhere ----

  // One CSV cell. A cell a spreadsheet would run as a formula (it starts with = + - @ or a control character) gets a
  // leading apostrophe, and a cell with a comma, quote or line break is quoted.
  function csvCell(value) {
    let s = String(value === null || value === undefined ? '' : value);
    if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`;
    return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  }

  function hashListCsv(items) {
    const rows = [['file', 'state', 'before_sha256', 'before_md5', 'after_sha256', 'after_md5', 'saved_as']];
    for (const item of items) {
      const fps = fingerprintsOf(item);
      if (!fps) continue;
      const after = fps.after;
      rows.push([item.name, after ? 'cleaned' : 'not cleaned', fps.before.sha256, fps.before.md5,
        after ? after.sha256 : '', after ? after.md5 : '', after ? item.result.filename : '']);
    }
    // A byte-order mark so a spreadsheet reads non-English file names right; CRLF line ends are what CSV asks for.
    return `﻿${rows.map((r) => r.map(csvCell).join(',')).join('\r\n')}\r\n`;
  }

  function downloadHashList() {
    const have = state.items.filter((i) => fingerprintsOf(i));
    if (!have.length) return;
    const url = URL.createObjectURL(new Blob([hashListCsv(have)], { type: 'text/csv;charset=utf-8' }));
    triggerDownload(url, 'imgstrip-hashes.csv');
    window.setTimeout(() => URL.revokeObjectURL(url), 10000);
    announce(`Hash list for ${plural(have.length, 'file')} downloaded.`);
  }

  // A hash on screen is grouped with spaces for reading; copying it by hand gives the plain hex.
  function copyPlain(e) {
    const text = String(window.getSelection()).replace(/\s+/g, '');
    if (!text || !e.clipboardData) return;
    e.clipboardData.setData('text/plain', text);
    e.preventDefault();
  }

  async function copyText(text, button, label = 'Hash') {
    try {
      await navigator.clipboard.writeText(text);
      button.replaceChildren(icon('check'));
      button.dataset.done = '';
      announce(`${label} copied.`);
      window.setTimeout(() => { button.replaceChildren(icon('copy')); delete button.dataset.done; }, 1400);
    } catch {
      announce('Could not copy. Select the hash to copy it instead.');
    }
  }

  function note(text, kind = '') {
    const glyph = kind === 'warn' || kind === 'error' ? 'alert' : kind === 'ok' ? 'check' : 'info';
    return h('p', { class: `note${kind ? ` is-${kind}` : ''}` }, icon(glyph), h('span', { text }));
  }

  // What cleaning did about data hidden in a JPEG's DCT coefficients (only present when asked for), in the server's
  // words and always with its caveat: it says what was done, never that nothing is hidden.
  function dctBlock(d) {
    if (!d) return [];
    const cost = d.psnr_db ? ` About ${d.psnr_db} dB from the original picture.` : '';
    return [note(`Hidden data in the JPEG coefficients: ${d.headline}. ${capital(d.summary)}.${cost}`), note(d.caveat)];
  }

  function alphaBlock(a) {
    if (!a) return [];
    return [note(`Hidden data in the transparency channel: ${a.headline}. ${capital(a.summary)}.`), note(a.caveat)];
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

  // The buttons that end the detail: Download (once cleaned) and Remove, so a single file can be dealt with from here.
  function detailActions(item) {
    const res = item.state === 'clean' && item.result;
    return h('div', { class: 'd-foot' },
      res ? h('a', { class: 'btn btn-secondary btn-small', href: `/api/result/${res.id}`, download: res.filename, 'data-key': 'download' }, icon('download'), h('span', { text: 'Download' })) : null,
      h('button', { class: 'btn btn-quiet btn-small', type: 'button', 'data-key': 'remove', onclick: () => removeItem(item) }, icon('x'), h('span', { text: 'Remove from list' })));
  }

  function detailContent(item) {
    const out = [h('h2', { class: 'd-title', id: 'detailTitle', tabindex: '-1', 'data-key': 'title', text: item.name })];
    const r = item.report;

    if (item.state === 'clean' && item.result) return out.concat(cleanDetail(item));

    if (item.state === 'scanning') {
      out.push(h('p', { class: 'd-lede', text: 'Reading this file. What it gives away will appear here when the scan finishes.' }));
      out.push(detailActions(item));
      return out;
    }
    if (item.state === 'failed' || item.state === 'scanfailed') out.push(note(item.error, 'error'));
    if (item.state === 'unsupported' && item.blocked) {
      out.push(note(`${item.blocked} It can't be cleaned.`, 'error'));
    } else if (item.state === 'unsupported') {
      out.push(note(r && r.raw
        ? `Camera RAW files (${r.raw}) aren't supported. Export the photo to TIFF, PNG or JPEG first.`
        : "This file isn't an image imgstrip can read, so it can't be cleaned.", 'error'));
    }
    if (!r || item.state === 'unsupported') {
      const fp = fingerprintSection(item);
      if (fp) out.push(fp);
      out.push(detailActions(item));
      return out;
    }

    out.push(ledeFor(item));
    const verdict = scanVerdict(r);
    if (hasStego(r) && !optionsFor(item).scrub_lsb) {
      const lsb = supportOf(item).scrub_lsb;
      out.push(note(lsb.state === 'unsupported'
        ? `Cleaning removes everything else, but keeps this payload: Scrub least-significant bits can't be used on this image. ${lsb.reason}`
        : 'Cleaning removes everything else, but keeps this payload unless Scrub least-significant bits is on in Options.', 'warn'));
    }
    if (isDamaged(r)) out.push(note(`The file's structure is damaged (${r.error}). Cleaning rebuilds it from its pixels if they can still be decoded.`, 'warn'));
    for (const n of r.notes) out.push(note(n, 'warn'));
    // Options the image cannot take are left out of its clean, and this says so rather than dropping them quietly.
    out.push(...skippedOptions(item));
    if (r.format) out.push(...findingSections(r.findings));
    if (r.credential && r.credential.present) out.push(foundCredential(r.credential));
    const pixels = pixelSection(item, verdict);
    if (pixels) out.push(pixels);
    const fp = fingerprintSection(item);
    if (fp) out.push(fp);
    out.push(detailActions(item));
    return out;
  }

  function cleanDetail(item) {
    const res = item.result;
    const o = optionsFor(item);
    const out = [];
    const kept = keptPayload(item);
    const bold = (t) => h('strong', { text: t });
    out.push(h('p', { class: 'd-lede' }, res.removed.length
      ? ['This copy was rebuilt from the pixels alone. ', bold(`${plural(res.removed.length, 'item')} removed`), '.']
      : ['This copy was rebuilt from the pixels alone. Nothing beside the picture was found to remove.']));
    if (kept) out.push(note('A steganography payload was detected in this file\'s pixels and is still there, because nothing scrubbed it. Turn on Scrub least-significant bits in Options and clean again to destroy it.', 'warn'));
    else if (unappliedScrub(item)) out.push(note('Scrub least-significant bits was turned on, but it could not be applied to this kind of image, so hidden data in its pixels was neither scanned nor destroyed.', 'warn'));
    if (res.key !== optionsKey(item)) out.push(note('Cleaned with earlier options. Clean again to apply the current ones.', 'warn'));
    if (refused(item)) out.push(note(`Cleaning again with the current options failed: ${item.error} This is the earlier clean file.`, 'error'));

    const checks = h('div', { class: 'checks' },
      h('p', { class: 'check' }, icon('check'), h('span', { text: 'Every segment, chunk and box of the new file is accounted for, and the places meant to be empty are empty.' })),
      h('p', { class: 'check' }, icon('check'), h('span', {
        text: res.lossy
          ? `It decodes back to the written pixels within ${formatName(res.output_format)} re-encoding tolerance.`
          : 'It decodes back to exactly the pixels that were written.',
      })));
    if (kept || unappliedScrub(item)) {
      out.push(h('div', { class: 'verified is-partial' }, checks));
    } else {
      const animate = !item.sealDrawn;
      item.sealDrawn = true;
      out.push(h('div', { class: 'verified' }, seal(res.sha256, { detail: true, animate }), checks));
    }

    if (res.credentials) out.push(credentialBlock(res, o));
    const frames = res.frames > 1 ? ` · ${res.frames} frames` : '';
    out.push(h('p', { class: 'd-meta', text: `${formatName(res.input_format)} ${bytes(res.bytes_in)} → ${formatName(res.output_format)} ${bytes(res.bytes_out)} · ${res.size[0]}×${res.size[1]}${frames} · saved as ${res.filename}` }));
    const stego = stegoOf(item);
    if (stego) out.push(note(`Hidden data in the pixels: ${stego.headline}. ${capital(stego.summary)}.`, stego.state === 'detected' ? 'warn' : ''), ...(stego.state === 'not_scanned' ? [] : [note(stego.caveat)]));
    out.push(...dctBlock(res.dct));
    out.push(...alphaBlock(res.alpha));
    if (res.prnu) {
      out.push(note("The camera's sensor noise pattern was scrambled by a random warp of the pixels. Matching this photo to its camera should now fail, but unlike the checks above that cannot be proven."));
    } else if (res.output_format !== 'GIF' && !res.warnings.some((w) => w.includes('sensor-fingerprint'))) {
      out.push(note("The pixels still carry the camera's sensor noise pattern, which can link this photo to one camera. Turn on Disrupt camera fingerprint in Options to scramble it."));
    }
    out.push(...skippedOptions(item));
    if (res.removed.length) out.push(...findingSections(res.removed, { removed: true }));
    else if (!kept) out.push(note('Nothing beside the picture was found in the file\'s structure. The file was still rebuilt from its pixels.', 'ok'));
    if (res.actions.length) {
      out.push(detailSection('Changed', null, h('ul', { class: 'actions-list' }, res.actions.map((a) => h('li', { text: capital(a) })))));
    }
    for (const w of res.warnings) out.push(note(w, 'warn'));
    const fp = fingerprintSection(item);
    if (fp) out.push(fp);
    out.push(detailActions(item));
    return out;
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
    const root = input.closest(isField ? '.field' : '.switch-row');
    const note = h('span', { class: 'support', id: `support-${name}`, hidden: true });
    root.append(note);
    // A switch is already described by its hint; the support note is added to that, never in its place.
    return (supportControls[name] = { input, root, note, isField, base: input.getAttribute('aria-describedby') || '' });
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
    const describedBy = (extra) => {
      const ids = [ctl.base, extra].filter(Boolean).join(' ');
      if (ids) input.setAttribute('aria-describedby', ids); else input.removeAttribute('aria-describedby');
    };
    note.replaceChildren();
    note.removeAttribute('title');
    note.className = 'support';
    note.hidden = !text;
    if (text) {
      if (kind === 'warn') note.append(icon('warn'));
      note.classList.add(kind === 'warn' ? 'is-warn' : 'is-na');
      note.append(h('span', { text }));
      note.title = text;
      describedBy(note.id);
    } else {
      describedBy('');
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
      supportControls.format = { note: h('span', { class: 'support', id: 'support-format', hidden: true }), input: select, base: '' };
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
    selectItem(item);
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
    // The drop zone stays at the top of the list; the steps and the file-type note are only for the empty page.
    els.workspace.dataset.empty = String(!any);
    els.formats.hidden = any;
    els.steps.hidden = any;
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
    els.hashList.hidden = !items.some((i) => fingerprintsOf(i));
    const filesWithFindings = items.filter((i) => i.state !== 'clean' && i.state !== 'unsupported' && findingCount(i.report)).length;
    if (clean.length) {
      els.listFound.textContent = `${plural(removed, 'item')} removed, in ${plural(clean.length, 'file')}${found ? ` · ${plural(found, 'item')} still to remove` : ''}`;
    } else if (found) {
      els.listFound.textContent = `${plural(found, 'item')} found${scanning ? ' so far' : ''}, in ${plural(filesWithFindings, 'file')}`;
    } else {
      els.listFound.textContent = scanning ? 'Nothing found so far' : 'No metadata found';
    }

    // The bar says plainly that nothing has changed yet, until something has.
    const s = [];
    if (state.run) s.push(h('span', { text: `Cleaning ${Math.min(state.run.done + 1, state.run.total)} of ${state.run.total}…` }));
    else if (scanning) s.push(h('span', { text: `Nothing is changed yet. ${scanning === 1 ? '1 file is' : `${scanning} files are`} still being scanned.` }));
    else if (!clean.length) {
      s.push('Nothing is changed yet.');
      if (todo.length) s.push(` ${plural(todo.length, 'image')} ${todo.length === 1 ? 'is' : 'are'} ready to clean.`);
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
    // A switch can sit outside the form and still belong to it (Deep scan, in the action bar), so listen on the page.
    document.addEventListener('change', (e) => {
      const f = e.target;
      if (!f.name || f.form !== form) return;
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
    if (o.scrub_lsb || o.scrub_alpha || o.scrub_prnu || o.scrub_dct) hints.push('Scrubbing is on, so the credential will say the pixels were deliberately altered to protect the source.');
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

  // ---------------------------------------------------------------- the (i) buttons

  // Every switch has an (i) button beside it. Pressing it opens a small bubble with the explanation, read out by screen
  // readers through the live region that holds it. Escape, a press elsewhere, or moving on closes it.
  let tipFor = null;

  function closeTip({ restore = false } = {}) {
    if (!tipFor) return;
    const button = tipFor;
    tipFor = null;
    button.setAttribute('aria-expanded', 'false');
    els.infoTip.replaceChildren();
    if (restore) button.focus();
  }

  function placeTip(button, bubble) {
    const r = button.getBoundingClientRect();
    const vw = document.documentElement.clientWidth;
    const vh = document.documentElement.clientHeight;
    const b = bubble.getBoundingClientRect();
    const edge = 12;
    const left = Math.min(Math.max(edge, r.left + r.width / 2 - b.width / 2), Math.max(edge, vw - b.width - edge));
    let top = r.bottom + 8;
    if (top + b.height > vh - edge) top = r.top - 8 - b.height >= edge ? r.top - 8 - b.height : Math.max(edge, vh - b.height - edge);
    bubble.style.left = `${left}px`;
    bubble.style.top = `${top}px`;
  }

  function openTip(button) {
    if (tipFor === button) { closeTip(); return; }
    closeTip();
    const name = button.dataset.info;
    if (!INFO[name]) return;
    tipFor = button;
    button.setAttribute('aria-expanded', 'true');
    const bubble = h('div', { class: 'info-bubble' }, h('strong', { text: OPTION_LABEL[name] || name }), h('span', { text: INFO[name] }));
    els.infoTip.replaceChildren(bubble);
    placeTip(button, bubble);
  }

  // ---------------------------------------------------------------- events

  function bind() {
    els.choose.addEventListener('click', () => els.picker.click());
    els.sheetBack.addEventListener('click', closeSheet);
    NARROW.addEventListener('change', () => { if (state.sheet) setSheet(false); });
    document.addEventListener('click', (e) => {
      const button = e.target.closest && e.target.closest('.info[data-info]');
      if (button) openTip(button);
    });
    document.addEventListener('pointerdown', (e) => {
      if (tipFor && !(e.target.closest && (e.target.closest('.info-bubble') || e.target.closest('.info[data-info]')))) closeTip();
    });
    document.addEventListener('focusin', (e) => { if (tipFor && e.target !== tipFor) closeTip(); });
    document.addEventListener('scroll', () => closeTip(), true);
    window.addEventListener('resize', () => closeTip());
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
    els.hashList.addEventListener('click', downloadHashList);
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
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      const active = document.activeElement;
      if (tipFor) { closeTip({ restore: true }); return; }
      if (state.sheet) { closeSheet(); return; }
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
    bind();
    els.localAddr.textContent = window.location.host;
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
