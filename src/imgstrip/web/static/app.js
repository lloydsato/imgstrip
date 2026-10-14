/* imgstrip web app: scan first, clean on request, prove the result. */
'use strict';

(() => {
  const TOKEN = document.querySelector('meta[name="imgstrip-token"]').content;
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
    person: 'a name', software: 'software', preview: 'a hidden image', trailer: 'appended data', comment: 'a comment',
  };
  const OPTION_DEFAULTS = {
    format: '', quality: '', icc: 'convert',
    deep: false, jpeg_lossless: false, scrub_lsb: false, auto_orient: true, optimize: false,
  };
  const STORE_KEY = 'imgstrip.options.v1';

  const state = {
    info: null,
    items: [],
    nextId: 1,
    options: loadOptions(),
    cleaningRun: null, // {total, done} while a clean batch runs
    lastFolder: null,
  };

  const els = {
    window: $('#window'), well: $('#well'), list: $('#list'), files: $('#files'), listCount: $('#listCount'),
    foot: $('#foot'), summary: $('#summary'), primary: $('#primary'), downloadSome: $('#downloadSome'),
    saveToggle: $('#saveToggle'), save: $('#save'), saveForm: $('#saveForm'), folder: $('#folder'),
    footNote: $('#footNote'), picker: $('#picker'), options: $('#options'), optionsToggle: $('#optionsToggle'),
    optionsForm: $('#optionsForm'), limits: $('#limits'), limitsToggle: $('#limitsToggle'),
    clear: $('#clear'), announce: $('#announce'),
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
  const formatName = (f) => ({ JPEG: 'JPEG', PNG: 'PNG', WEBP: 'WebP', GIF: 'GIF', TIFF: 'TIFF', BMP: 'BMP', AVIF: 'AVIF', HEIF: 'HEIF', MPO: 'MPO' }[f] || f);

  function extOf(name) {
    const m = /\.([^.]+)$/.exec(name);
    return m ? m[1].toUpperCase() : '';
  }

  function announce(text) {
    els.announce.textContent = '';
    window.setTimeout(() => { els.announce.textContent = text; }, 30);
  }

  function setOpen(reveal, toggle, open) {
    reveal.dataset.open = String(open);
    if (toggle) toggle.setAttribute('aria-expanded', String(open));
  }

  function loadOptions() {
    try {
      const saved = JSON.parse(window.localStorage.getItem(STORE_KEY) || '{}');
      return { ...OPTION_DEFAULTS, ...pick(saved, Object.keys(OPTION_DEFAULTS)) };
    } catch {
      return { ...OPTION_DEFAULTS };
    }
  }

  function saveOptions() {
    try { window.localStorage.setItem(STORE_KEY, JSON.stringify(state.options)); } catch { /* private window */ }
  }

  function pick(obj, keys) {
    const out = {};
    for (const k of keys) if (obj && Object.prototype.hasOwnProperty.call(obj, k)) out[k] = obj[k];
    return out;
  }

  function cleanOptions() {
    const o = state.options;
    return {
      format: o.format || null,
      quality: o.quality ? Number(o.quality) : null,
      icc: o.icc,
      jpeg_lossless: !!o.jpeg_lossless,
      scrub_lsb: !!o.scrub_lsb,
      auto_orient: !!o.auto_orient,
      optimize: !!o.optimize,
    };
  }
  const optionsKey = () => JSON.stringify(cleanOptions());

  // ---------------------------------------------------------------- API

  async function api(path, { method = 'POST', body, json, headers = {} } = {}) {
    const hdrs = { 'X-Imgstrip-Token': TOKEN, ...headers };
    if (json !== undefined) {
      hdrs['Content-Type'] = 'application/json';
      body = JSON.stringify(json);
    } else if (body) {
      hdrs['Content-Type'] = 'application/octet-stream';
    }
    let res;
    try {
      res = await fetch(path, { method, body, headers: hdrs, credentials: 'same-origin', cache: 'no-store' });
    } catch {
      throw new Error('imgstrip is not running any more. Start it again with imgstrip-web.');
    }
    const type = res.headers.get('Content-Type') || '';
    if (!res.ok) {
      let message = `Something went wrong (HTTP ${res.status}).`;
      if (type.includes('application/json')) {
        try { message = (await res.json()).error || message; } catch { /* keep default */ }
      }
      throw new Error(message);
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
      fn().then(resolve, reject).finally(() => { active--; next(); });
    };
    return (fn) => new Promise((resolve, reject) => { queue.push({ fn, resolve, reject }); next(); });
  }
  const scanQueue = limiter(2);
  const cleanQueue = limiter(2);

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

  // ---------------------------------------------------------------- items

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
      open: false,
      thumbUrl: null,
      justCleaned: false,
      el: null,
    };
    state.items.push(item);
    return item;
  }

  function addFiles(fileList) {
    const files = Array.from(fileList || []).filter((f) => f && f.size !== undefined);
    if (!files.length) return;
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
    item.state = 'scanning';
    renderItem(item);
    render();
    const deep = state.options.deep ? '1' : '0';
    try {
      item.report = await scanQueue(() => api(`/api/inspect?deep=${deep}`, {
        body: item.file, headers: { 'X-Filename': encodeURIComponent(item.name) },
      }));
      item.error = null;
      if (!state.items.includes(item)) return;
      item.state = item.result && item.result.key === optionsKey() ? 'clean' : 'scanned';
    } catch (err) {
      if (!state.items.includes(item)) return;
      item.state = 'scanfailed';
      item.error = err.message;
    }
    renderItem(item);
    render();
    if (!state.items.some((i) => i.state === 'scanning')) {
      const found = state.items.reduce((n, i) => n + (i.report && i.state !== 'clean' ? i.report.findings.length : 0), 0);
      announce(`Scan finished. ${plural(found, 'revealing item')} found.`);
    }
  }

  function cleanable() {
    const key = optionsKey();
    return state.items.filter((i) =>
      (i.state === 'scanned') ||
      (i.state === 'failed' && i.failedKey !== key) ||
      (i.state === 'clean' && i.result && i.result.key !== key));
  }

  async function cleanAll() {
    const todo = cleanable();
    if (!todo.length || state.cleaningRun) return;
    const key = optionsKey();
    const options = encodeURIComponent(JSON.stringify(cleanOptions()));
    state.cleaningRun = { total: todo.length, done: 0 };
    hideNote();
    render();
    await Promise.all(todo.map(async (item) => {
      const previous = item.result;
      item.state = 'cleaning';
      renderItem(item);
      try {
        const result = await cleanQueue(() => api('/api/clean', {
          body: item.file, headers: { 'X-Filename': encodeURIComponent(item.name), 'X-Options': options },
        }));
        result.key = key;
        if (!state.items.includes(item)) {
          api(`/api/result/${result.id}`, { method: 'DELETE' }).catch(() => {});
          return;
        }
        if (previous) api(`/api/result/${previous.id}`, { method: 'DELETE' }).catch(() => {});
        item.result = result;
        item.state = 'clean';
        item.error = null;
        item.justCleaned = true;
      } catch (err) {
        if (!state.items.includes(item)) return;
        if (previous) api(`/api/result/${previous.id}`, { method: 'DELETE' }).catch(() => {});
        item.result = null;
        item.state = 'failed';
        item.error = err.message;
        item.failedKey = key;
      }
      state.cleaningRun.done++;
      renderItem(item);
      render();
    }));
    const run = state.cleaningRun;
    state.cleaningRun = null;
    render();
    const failed = todo.filter((i) => i.state === 'failed').length;
    announce(failed
      ? `${plural(run.total - failed, 'image')} cleaned and verified. ${failed} could not be cleaned.`
      : `${plural(run.total, 'image')} cleaned and verified.`);
  }

  function removeItem(item) {
    state.items = state.items.filter((i) => i !== item);
    if (item.result) api(`/api/result/${item.result.id}`, { method: 'DELETE' }).catch(() => {});
    if (item.thumbUrl) URL.revokeObjectURL(item.thumbUrl);
    const focusTarget = item.el.nextElementSibling || item.el.previousElementSibling;
    item.el.remove();
    render();
    const btn = focusTarget && focusTarget.querySelector('.file-head');
    (btn || (state.items.length ? els.window : $('#choose'))).focus?.();
  }

  function clearAll() {
    for (const item of state.items) if (item.thumbUrl) URL.revokeObjectURL(item.thumbUrl);
    state.items = [];
    els.files.replaceChildren();
    api('/api/reset', { json: {} }).catch(() => {});
    hideNote();
    setOpen(els.save, els.saveToggle, false);
    render();
    $('#choose').focus();
    announce('List cleared.');
  }

  // ---------------------------------------------------------------- thumbnails

  function loadThumb(item) {
    const holder = item.el.querySelector('.thumb');
    const fallback = async () => {
      try {
        const blob = await api('/api/preview', { body: item.file, headers: { 'X-Filename': encodeURIComponent(item.name) } });
        if (item.thumbUrl) URL.revokeObjectURL(item.thumbUrl);
        item.thumbUrl = URL.createObjectURL(blob);
        const img = h('img', { alt: '', src: item.thumbUrl, decoding: 'async' });
        holder.replaceChildren(img);
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
    const head = h('button', {
      class: 'file-head', type: 'button', 'aria-expanded': 'false', 'aria-controls': detailId,
      onclick: () => { item.open = !item.open; renderItem(item); },
    },
    h('span', { class: 'thumb' }, icon('image')),
    h('span', { class: 'file-main' }, h('span', { class: 'file-name' }), h('span', { class: 'file-sub' })),
    h('span', { class: 'status' }),
    icon('chevron', 'icon chev'));
    const actions = h('div', { class: 'file-actions' });
    const detail = h('div', { class: 'reveal', id: detailId, 'data-open': 'false' }, h('div', { class: 'reveal-inner' }, h('div', { class: 'detail-body' })));
    item.el = h('li', { class: 'file', 'data-state': item.state, 'data-open': 'false' },
      h('div', { class: 'file-row' }, head, actions), detail);
    els.files.append(item.el);
    renderItem(item);
  }

  function itemSub(item) {
    const r = item.report;
    if (item.state === 'clean' && item.result) {
      const res = item.result;
      return { text: `${formatName(res.output_format)} · ${res.size[0]}×${res.size[1]} · ${bytes(res.bytes_out)}`, leak: false };
    }
    if (r && r.reveals.length && item.state !== 'scanning') {
      const names = r.reveals.slice(0, 3).map((x) => REVEAL_NOUN[x.key] || x.label.toLowerCase());
      return { text: `Reveals ${names.join(', ')}`, leak: true };
    }
    const parts = [];
    if (r && r.format) parts.push(formatName(r.format));
    else if (extOf(item.name)) parts.push(extOf(item.name));
    if (r && r.size) parts.push(`${r.size[0]}×${r.size[1]}`);
    parts.push(bytes(item.size));
    return { text: parts.join(' · '), leak: false };
  }

  function itemStatus(item) {
    const r = item.report;
    switch (item.state) {
      case 'scanning': return [h('span', { text: 'Scanning…' })];
      case 'cleaning': return [h('span', { text: 'Cleaning…' })];
      case 'scanfailed': return [icon('alert'), h('span', { class: 'status-fail', text: 'Not scanned' })];
      case 'failed': return [icon('alert'), h('span', { class: 'status-fail', text: 'Not cleaned' })];
      case 'clean': {
        const stale = item.result.key !== optionsKey();
        const anim = item.justCleaned;
        item.justCleaned = false;
        return [
          seal(item.result.sha256, { detail: false, animate: anim }),
          h('span', { class: stale ? '' : 'status-ok', text: stale ? 'Clean (old options)' : 'Clean' }),
        ];
      }
      default: {
        const n = r ? r.findings.length + (r.format && r.error ? 1 : 0) : 0;
        if (n) return [h('span', { class: 'status-count', text: plural(n, 'item') })];
        if (r && !r.format) return [h('span', { text: 'Not inspected' })];
        return [h('span', { text: 'Nothing found' })];
      }
    }
  }

  function renderItem(item) {
    const el = item.el;
    if (!el) return;
    el.dataset.state = item.state;
    el.dataset.open = String(item.open);
    $('.file-name', el).textContent = item.name;
    $('.file-name', el).title = item.name;
    const sub = itemSub(item);
    const subEl = $('.file-sub', el);
    subEl.textContent = sub.text;
    subEl.classList.toggle('is-leak', sub.leak);
    $('.status', el).replaceChildren(...itemStatus(item));
    const head = $('.file-head', el);
    head.setAttribute('aria-expanded', String(item.open));
    head.setAttribute('aria-label', `${item.name}, ${$('.status', el).textContent}. ${sub.text}`);

    const actions = $('.file-actions', el);
    const parts = [];
    if (item.state === 'clean') {
      parts.push(h('a', {
        class: 'btn btn-icon', href: `/api/result/${item.result.id}`, download: item.result.filename,
        'aria-label': `Download ${item.result.filename}`, title: `Download ${item.result.filename}`,
      }, icon('download')));
    }
    parts.push(h('button', {
      class: 'btn btn-icon', type: 'button', 'aria-label': `Remove ${item.name} from the list`, title: 'Remove',
      disabled: item.state === 'cleaning', onclick: () => removeItem(item),
    }, icon('x')));
    actions.replaceChildren(...parts);

    const detail = $('.reveal', el);
    detail.dataset.open = String(item.open);
    if (item.open) $('.detail-body', el).replaceChildren(...detailContent(item));
  }

  function groupList(findings, removed) {
    const groups = new Map();
    for (const f of findings) {
      if (!groups.has(f.category)) groups.set(f.category, []);
      groups.get(f.category).push(f);
    }
    const order = [...CATEGORY_ORDER, ...[...groups.keys()].filter((k) => !CATEGORY_ORDER.includes(k))];
    return order.filter((k) => groups.has(k)).map((k) => h('section', { class: 'group' },
      h('h3', { class: 'group-title' }, `${removed ? 'Removed: ' : ''}${CATEGORY[k] || k}`, h('span', { class: 'n', text: String(groups.get(k).length) })),
      h('ul', { class: 'items' }, groups.get(k).map((f) => h('li', { class: 'item' },
        h('span', { class: 'item-label', text: f.label }),
        h('span', { class: 'item-detail', text: f.detail || '—' }))))));
  }

  function note(text, kind = '') {
    return h('p', { class: `note${kind ? ` is-${kind}` : ''}` }, icon(kind === 'warn' || kind === 'error' ? 'alert' : 'check'), h('span', { text }));
  }

  function detailContent(item) {
    const r = item.report;
    const out = [];
    const meta = [];
    if (r && r.format) meta.push(formatName(r.format));
    if (r && r.size) meta.push(`${r.size[0]}×${r.size[1]}`);
    meta.push(bytes(item.size));
    if (r && r.deep) meta.push('deep scan');

    if (item.state === 'clean' && item.result) {
      const res = item.result;
      const lossy = !res.pixels_exact;
      const checks = h('div', { class: 'checks' },
        h('p', { class: 'check' }, icon('check'), h('span', { text: 'Every byte of the new file is accounted for.' })),
        h('p', { class: 'check' }, icon('check'), h('span', {
          text: lossy
            ? `Pixels match the original within ${formatName(res.output_format)} re-encoding tolerance.`
            : 'Pixels are bit-for-bit identical to the original.',
        })));
      const hash = h('div', { class: 'hash' },
        h('code', { text: `SHA-256 ${res.sha256}` }),
        h('button', {
          class: 'btn btn-icon', type: 'button', 'aria-label': 'Copy SHA-256', title: 'Copy SHA-256',
          onclick: (e) => copyText(res.sha256, e.currentTarget),
        }, icon('copy')));
      checks.append(hash);
      out.push(h('div', { class: 'verified' }, seal(res.sha256, { detail: true }), checks));
      const frames = res.frames > 1 ? ` · ${res.frames} frames` : '';
      out.push(h('p', { class: 'meta-line', text: `${formatName(res.input_format)} ${bytes(res.bytes_in)} → ${formatName(res.output_format)} ${bytes(res.bytes_out)} · ${res.size[0]}×${res.size[1]}${frames} · saved as ${res.filename}` }));
      if (res.removed.length) out.push(...groupList(res.removed, true));
      else out.push(note('Nothing beside the picture was found. The file was still rebuilt from its pixels.'));
      if (res.actions.length) {
        out.push(h('section', { class: 'group' }, h('h3', { class: 'group-title', text: 'Changed' }),
          h('ul', { class: 'actions-list' }, res.actions.map((a) => h('li', { text: a.charAt(0).toUpperCase() + a.slice(1) })))));
      }
      for (const w of res.warnings) out.push(note(w, 'warn'));
      return out;
    }

    out.push(h('p', { class: 'meta-line', text: meta.join(' · ') }));
    if (item.state === 'failed' || item.state === 'scanfailed') out.push(note(item.error, 'error'));
    if (!r) return out;
    if (r.reveals.length) {
      out.push(h('dl', { class: 'reveals' }, r.reveals.map((x) => [
        h('dt', { text: x.label }),
        h('dd', { class: x.key === 'location' || x.key === 'serial' ? 'mono' : null, text: x.value }),
      ])));
    }
    if (r.findings.length) out.push(...groupList(r.findings, false));
    if (r.format && r.error) out.push(note(`The file's structure is damaged: ${r.error}. Cleaning rebuilds it from its pixels if they can still be decoded.`, 'warn'));
    if (!r.format) out.push(note("imgstrip can't inspect this container, but cleaning still rebuilds the image from its pixels alone."));
    else if (!r.findings.length && !r.error) {
      out.push(note(r.deep
        ? 'Nothing beside the picture was found, in the container or in the pixels. Cleaning still rebuilds the file from its pixels.'
        : 'Nothing beside the picture was found. Cleaning still rebuilds the file from its pixels, which removes anything a scan cannot recognise.'));
    }
    for (const n of r.notes) out.push(note(n, 'warn'));
    return out;
  }

  async function copyText(text, button) {
    try {
      await navigator.clipboard.writeText(text);
      button.replaceChildren(icon('check'));
      button.setAttribute('aria-label', 'Copied');
      window.setTimeout(() => { button.replaceChildren(icon('copy')); button.setAttribute('aria-label', 'Copy SHA-256'); }, 1400);
    } catch { /* clipboard blocked: the hash stays selectable */ }
  }

  // ---------------------------------------------------------------- rendering: shell

  function render() {
    const items = state.items;
    const any = items.length > 0;
    els.well.hidden = any;
    els.list.hidden = !any;
    els.foot.hidden = !any;
    if (!any) return;

    const scanning = items.filter((i) => i.state === 'scanning').length;
    const clean = items.filter((i) => i.state === 'clean');
    const fresh = clean.filter((i) => i.result.key === optionsKey());
    const failed = items.filter((i) => i.state === 'failed' || i.state === 'scanfailed').length;
    const todo = cleanable();
    const found = items.reduce((n, i) => n + (i.report && i.state !== 'clean' ? i.report.findings.length : 0), 0);
    const removed = clean.reduce((n, i) => n + i.result.removed.length, 0);

    els.listCount.textContent = plural(items.length, 'image');

    // summary line
    const s = [];
    if (state.cleaningRun) s.push(h('span', { text: `Cleaning ${Math.min(state.cleaningRun.done + 1, state.cleaningRun.total)} of ${state.cleaningRun.total}…` }));
    else if (scanning) s.push(h('span', { text: `Scanning ${plural(scanning, 'image')}…` }));
    else if (todo.length && !clean.length) {
      s.push(found ? h('span', { class: 'is-leak', text: plural(found, 'revealing item') }) : h('span', { text: 'Nothing revealing found' }));
      s.push(` in ${plural(items.length, 'image')}`);
    } else if (clean.length) {
      s.push(h('span', { class: 'is-clean', text: `${clean.length} clean` }));
      s.push(removed ? ` · ${plural(removed, 'item')} removed` : ' · rebuilt from pixels');
      if (todo.length && fresh.length !== clean.length) s.push(' · options changed');
      else if (todo.length) s.push(` · ${todo.length} to clean`);
    } else {
      s.push(h('span', { text: plural(items.length, 'image') }));
    }
    if (failed) s.push(h('span', { class: 'is-leak', text: ` · ${failed} failed` }));
    els.summary.replaceChildren(...s);

    // primary plate: the one next action
    const plate = els.primary;
    const label = $('span', plate);
    plate.classList.remove('is-busy');
    let mode;
    if (state.cleaningRun) {
      mode = 'busy';
      label.textContent = 'Cleaning…';
      plate.disabled = true;
      plate.classList.add('is-busy');
    } else if (todo.length) {
      mode = 'clean';
      const allStale = todo.every((i) => i.state === 'clean');
      label.textContent = allStale ? `Clean again with new options` : `Clean ${plural(todo.length, 'image')}`;
      plate.disabled = false;
    } else if (fresh.length) {
      mode = 'download';
      label.textContent = fresh.length === 1 ? 'Download' : `Download all (.zip)`;
      plate.disabled = false;
    } else {
      mode = 'wait';
      label.textContent = scanning ? 'Scanning…' : 'Clean';
      plate.disabled = true;
    }
    plate.dataset.mode = mode;

    // secondary: download (when the plate is busy cleaning others) and save
    const downloadable = fresh.length;
    els.downloadSome.hidden = !(downloadable && mode === 'clean');
    $('span', els.downloadSome).textContent = downloadable === 1 ? 'Download' : `Download ${downloadable}`;
    els.saveToggle.hidden = !downloadable;
    if (!downloadable) setOpen(els.save, els.saveToggle, false);
  }

  // ---------------------------------------------------------------- download & save

  function freshClean() {
    const key = optionsKey();
    return state.items.filter((i) => i.state === 'clean' && i.result.key === key);
  }

  function triggerDownload(href, filename) {
    const a = h('a', { href, download: filename, hidden: true });
    document.body.append(a);
    a.click();
    a.remove();
  }

  async function downloadClean(button) {
    const items = freshClean();
    if (!items.length) return;
    if (items.length === 1) {
      triggerDownload(`/api/result/${items[0].result.id}`, items[0].result.filename);
      return;
    }
    const label = $('span', button);
    const before = label.textContent;
    button.disabled = true;
    label.textContent = 'Packing…';
    try {
      const zip = await api('/api/zip', { json: { ids: items.map((i) => i.result.id) } });
      triggerDownload(`/api/result/${zip.id}`, zip.filename);
      showNote(`Downloading ${zip.filename} (${bytes(zip.bytes)}). Its entries carry no dates.`, 'ok');
    } catch (err) {
      showNote(err.message, 'error');
    } finally {
      button.disabled = false;
      label.textContent = before;
      render();
    }
  }

  async function saveToFolder(event) {
    event.preventDefault();
    const items = freshClean();
    const folder = els.folder.value.trim();
    if (!items.length) return;
    if (!folder) {
      showNote('Type the folder to save into, for example ~/Pictures/clean.', 'error');
      els.folder.focus();
      return;
    }
    const submit = $('button[type="submit"]', els.saveForm);
    submit.disabled = true;
    try {
      const res = await api('/api/save', { json: { ids: items.map((i) => i.result.id), folder } });
      state.lastFolder = folder;
      try { window.localStorage.setItem('imgstrip.folder', folder); } catch { /* ignore */ }
      const ok = res.files.filter((f) => f.ok);
      const bad = res.files.filter((f) => !f.ok);
      if (bad.length) {
        showNote(`Saved ${ok.length} of ${res.files.length} to ${res.folder}. ${bad.map((f) => f.message).join(' ')}`, 'error');
      } else {
        showNote(`Saved ${plural(ok.length, 'image')} to ${res.folder}`, 'ok', res.folder);
        setOpen(els.save, els.saveToggle, false);
      }
    } catch (err) {
      showNote(err.message, 'error');
    } finally {
      submit.disabled = false;
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
      const before = state.options.deep;
      state.options[f.name] = f.type === 'checkbox' ? f.checked : f.value;
      saveOptions();
      if (f.name === 'deep' && before !== state.options.deep) {
        for (const item of state.items) if (item.state === 'scanned' || item.state === 'scanfailed') scan(item);
      }
      for (const item of state.items) renderItem(item);
      render();
    });
    form.addEventListener('submit', (e) => e.preventDefault());
  }

  // ---------------------------------------------------------------- brand marks

  function drawMarks() {
    const fixed = 'b3f19a07c2d4e58a61f0c3b7d9e2a4f6c8b1d3e5f7a9c0b2d4e6f8a1c3e5b7d9';
    $('#mark').append(sealSVG(fixed, { detail: false, check: false }));
    $('#wellMark').append(sealSVG(fixed, { detail: true, check: false }));
  }

  // ---------------------------------------------------------------- events

  function bind() {
    $('#choose').addEventListener('click', () => els.picker.click());
    $('#add').addEventListener('click', () => els.picker.click());
    els.picker.addEventListener('change', () => { addFiles(els.picker.files); els.picker.value = ''; });

    let armed = null;
    els.clear.addEventListener('click', () => {
      if (armed) {
        window.clearTimeout(armed);
        armed = null;
        els.clear.classList.remove('is-armed');
        $('span', els.clear).textContent = 'Clear';
        clearAll();
        return;
      }
      els.clear.classList.add('is-armed');
      $('span', els.clear).textContent = 'Clear all?';
      armed = window.setTimeout(() => {
        armed = null;
        els.clear.classList.remove('is-armed');
        $('span', els.clear).textContent = 'Clear';
      }, 3000);
    });

    els.optionsToggle.addEventListener('click', () => {
      setOpen(els.options, els.optionsToggle, els.options.dataset.open !== 'true');
    });
    els.limitsToggle.addEventListener('click', () => {
      setOpen(els.limits, els.limitsToggle, els.limits.dataset.open !== 'true');
    });

    els.primary.addEventListener('click', () => {
      const mode = els.primary.dataset.mode;
      if (mode === 'clean') cleanAll();
      else if (mode === 'download') downloadClean(els.primary);
    });
    els.downloadSome.addEventListener('click', () => downloadClean(els.downloadSome));
    els.saveToggle.addEventListener('click', () => {
      const open = els.save.dataset.open !== 'true';
      setOpen(els.save, els.saveToggle, open);
      if (open) {
        if (!els.folder.value) els.folder.value = state.lastFolder || state.info?.folder || '';
        window.setTimeout(() => els.folder.focus(), 60);
      }
    });
    els.saveForm.addEventListener('submit', saveToFolder);

    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      if (els.save.dataset.open === 'true') { setOpen(els.save, els.saveToggle, false); els.saveToggle.focus(); }
      else if (els.options.dataset.open === 'true') { setOpen(els.options, els.optionsToggle, false); els.optionsToggle.focus(); }
    });

    // drag and drop anywhere on the page
    let depth = 0;
    const hasFiles = (e) => Array.from(e.dataTransfer?.types || []).includes('Files');
    document.addEventListener('dragenter', (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth++;
      document.body.classList.add('is-dragging');
    });
    document.addEventListener('dragover', (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
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
    try { state.lastFolder = window.localStorage.getItem('imgstrip.folder'); } catch { /* ignore */ }
    try {
      state.info = await api('/api/info', { method: 'GET' });
    } catch (err) {
      state.info = { formats: ['JPEG', 'PNG', 'WEBP', 'GIF', 'TIFF', 'BMP'], folder: '' };
      showNote(err.message, 'error');
      els.foot.hidden = false;
    }
    initOptions(state.info);
    render();
  }

  init();
})();
