"""The two-pane web app, in a real browser (skipped without Node, Playwright and Chromium).

Covers what the redesign added or moved:

* an (i) button beside every switch, with a plain-words explanation that opens, closes and stays on screen;
* the SHA-256 and MD5 of every file, before and after cleaning, checked against hashlib and against the bytes
  actually downloaded;
* the hash list (CSV) and the box that compares a pasted hash with a file's;
* the list and the detail pane: selection, the full-screen sheet on a narrow screen, the keyboard;
* no sideways scrolling and no unreadable text from a phone to a wide monitor, in light and dark.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import textwrap
import threading

import pytest

from imgstrip import fixtures
from imgstrip.web import server as web
from test_credentials_ui import CHROMIUM, PLAYWRIGHT
from test_support_ui import _photo, _png
from test_web import Client

STATIC = os.path.join(os.path.dirname(web.__file__), "static")
HTML = open(os.path.join(STATIC, "index.html"), encoding="utf-8").read()
JS = open(os.path.join(STATIC, "app.js"), encoding="utf-8").read()

needs_browser = pytest.mark.skipif(not (PLAYWRIGHT and CHROMIUM), reason="needs node, playwright and chromium")

# ---------------------------------------------------------------------------
# Static checks: no browser needed


def _info_texts() -> dict[str, str]:
    block = JS[JS.index("const INFO = {") :]
    block = block[: block.index("\n  };")]
    return {m.group(1): m.group(2) for m in re.finditer(r"^\s{4}(\w+): '((?:[^'\\]|\\.)*)',?$", block, re.M)}


def test_every_switch_has_an_info_button_right_beside_it():
    rows = HTML.split('<div class="switch-row')[1:]
    names = []
    for row in rows:
        row = row[:2500]
        # the (i) comes straight after the switch's label, before its hint, and says what it is for
        m = re.search(
            r'<label class="switch"[^>]*>\s*<input type="checkbox" role="switch"[^>]*?\b(?:name|id)="([\w-]+)".*?</label>\s*'
            r'<button class="info" type="button" data-info="(\w+)" aria-expanded="false" aria-label="What does [^"]+ do\?">',
            row,
            re.S,
        )
        assert m, row[:300]
        names.append(m.group(1))
        assert row.index('class="info"') < row.index('class="hint"'), m.group(1)
    assert len(names) == len(set(names)) == len(re.findall(r'role="switch"', HTML)) == 12, names


def test_every_info_button_has_a_plain_words_explanation():
    infos = _info_texts()
    keys = re.findall(r'data-info="(\w+)"', HTML)
    assert len(keys) == len(set(keys)) == 12
    assert set(keys) == set(infos), set(keys) ^ set(infos)
    for key, text in infos.items():
        assert 40 <= len(text) <= 520, (key, len(text))
        assert text.endswith("."), key
        # short sentences, no shorthand a newcomer would have to look up
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            assert len(sentence.split()) <= 38, (key, sentence)
        for jargon in ("LSB", "DCT", "PRNU", "C2PA", "JUMBF", "chroma", "Huffman", "quantiz"):
            assert jargon not in text, (key, jargon)


def test_every_info_text_is_used_by_the_page_and_is_not_duplicated_in_the_html():
    infos = _info_texts()
    for key in infos:
        assert "INFO[name]" in JS and key in HTML
    assert len(set(infos.values())) == len(infos), "two switches share one explanation"


# ---------------------------------------------------------------------------
# Browser checks

SCRIPT = textwrap.dedent(
    r"""
    const { chromium } = require(process.env.PLAYWRIGHT_MODULE);
    const nodeCrypto = require('crypto');
    (async () => {
      const [dir, shotDir, ...urls] = process.argv.slice(2);
      const browser = await chromium.launch({ executablePath: process.env.CHROMIUM, args: ['--no-sandbox'] });
      const errors = [];
      const out = {};
      let n = 0;
      const squash = (t) => String(t || '').replace(/\s+/g, ' ').trim();

      async function open(name, { width = 1280, height = 900, scheme = 'light', touch = false, perms = false } = {}) {
        const ctx = await browser.newContext({
          viewport: { width, height }, colorScheme: scheme,
          ...(touch ? { hasTouch: true, isMobile: true, deviceScaleFactor: 2 } : {}),
          ...(perms ? { permissions: ['clipboard-read', 'clipboard-write'] } : {}),
          acceptDownloads: true,
        });
        const page = await ctx.newPage();
        page.on('pageerror', (e) => errors.push(name + ' pageerror: ' + e.message));
        page.on('console', (m) => { if (m.type() === 'error' && !m.text().includes('422')) errors.push(name + ' console: ' + m.text()); });
        await page.goto(urls[n++]);
        await page.waitForSelector('#optionsToggle');
        return page;
      }
      const add = async (page, files) => {
        await page.setInputFiles('#picker', files.map((f) => `${dir}/${f}`));
        await page.waitForFunction((k) => document.querySelectorAll('.file').length >= k && !document.querySelector('.file[data-state="scanning"]'), files.length, { timeout: 90000 });
        await page.waitForTimeout(200);
      };
      const cleanAll = async (page) => {
        await page.click('#primary');
        await page.waitForFunction(() => document.querySelectorAll('.file[data-state="clean"]').length > 0 && !document.querySelector('.file[data-state="cleaning"]') && document.querySelector('#primary').dataset.mode !== 'busy', null, { timeout: 120000 });
        await page.waitForTimeout(300);
      };
      const detailText = (page) => page.$eval('#detailContent', (e) => squashIn(e.innerText));

      // ---------------------------------------------------------------- 1. the (i) buttons
      for (const [label, width] of [['wide', 1280], ['phone', 320]]) {
        const page = await open('info-' + label, { width, height: label === 'phone' ? 700 : 900 });
        await add(page, ['plain.png']);
        await page.click('#optionsToggle');
        if (await page.$eval('#opt-c2pa', (e) => !e.disabled)) { await page.click('#c2paSwitch'); await page.waitForTimeout(400); }
        const keys = await page.$$eval('button.info[data-info]', (els) => els.filter((e) => e.offsetParent).map((e) => e.dataset.info));
        const rows = [];
        for (const key of keys) {
          const btn = page.locator(`button.info[data-info="${key}"]`);
          await btn.scrollIntoViewIfNeeded();
          const before = await page.$eval(`button.info[data-info="${key}"]`, (e) => {
            const input = e.closest('.switch-row').querySelector('input');
            return { checked: input.checked, switchRight: input.closest('label').getBoundingClientRect().right, infoLeft: e.getBoundingClientRect().left };
          });
          await btn.click();
          await page.waitForTimeout(120);
          rows.push(await page.evaluate((k) => {
            const e = document.querySelector(`button.info[data-info="${k}"]`);
            const input = e.closest('.switch-row').querySelector('input');
            const bubble = document.querySelector('#infoTip .info-bubble');
            const r = bubble ? bubble.getBoundingClientRect() : null;
            return {
              key: k, expanded: e.getAttribute('aria-expanded'), checked: input.checked,
              tip: bubble ? bubble.textContent.replace(/\s+/g, ' ').trim() : '', tipRole: document.querySelector('#infoTip').getAttribute('role'),
              rect: r ? { left: r.left, right: r.right, top: r.top, bottom: r.bottom } : null,
              vw: document.documentElement.clientWidth, vh: window.innerHeight,
              btn: (() => { const b = e.getBoundingClientRect(); return { w: b.width, h: b.height }; })(),
              label: e.getAttribute('aria-label'),
            };
          }, key));
          rows[rows.length - 1].before = before;
          await page.keyboard.press('Escape');
          await page.waitForTimeout(80);
          rows[rows.length - 1].afterEscape = await page.evaluate((k) => ({
            expanded: document.querySelector(`button.info[data-info="${k}"]`).getAttribute('aria-expanded'),
            tip: document.querySelector('#infoTip').textContent.trim(),
            focus: document.activeElement === document.querySelector(`button.info[data-info="${k}"]`),
          }), key);
        }
        out['info_' + label] = rows;
        if (label === 'wide') {
          // opening a second one closes the first; an outside click closes it; Enter opens from the keyboard
          const [a, b] = ['scrub_lsb', 'random_name'];
          await page.click(`button.info[data-info="${a}"]`);
          await page.click(`button.info[data-info="${b}"]`);
          out.infoSwap = await page.evaluate(([x, y]) => ({
            a: document.querySelector(`button.info[data-info="${x}"]`).getAttribute('aria-expanded'),
            b: document.querySelector(`button.info[data-info="${y}"]`).getAttribute('aria-expanded'),
            bubbles: document.querySelectorAll('#infoTip .info-bubble').length,
          }), [a, b]);
          await page.click(`button.info[data-info="${b}"]`);
          out.infoToggleOff = await page.$eval(`button.info[data-info="${b}"]`, (e) => e.getAttribute('aria-expanded'));
          await page.click(`button.info[data-info="${a}"]`);
          await page.mouse.click(5, 5);
          out.infoOutside = await page.$eval(`button.info[data-info="${a}"]`, (e) => e.getAttribute('aria-expanded'));
          await page.focus(`button.info[data-info="${a}"]`);
          await page.keyboard.press('Enter');
          out.infoEnter = await page.$eval(`button.info[data-info="${a}"]`, (e) => e.getAttribute('aria-expanded'));
          await page.keyboard.press('Tab');
          out.infoTabAway = await page.$eval(`button.info[data-info="${a}"]`, (e) => e.getAttribute('aria-expanded'));
          // the footer's Deep scan has one too
          out.deepInfo = await page.$eval('#foot button.info[data-info="deep"]', (e) => !!e.offsetParent);
        }
        await page.context().close();
      }

      // ---------------------------------------------------------------- 2. hashes, the hash list, the compare box
      {
        const page = await open('hashes', { perms: true });
        const files = ['plain.png', 'photo.jpg', '=evil, "x".png'];
        await add(page, files);
        const grab = async () => page.$$eval('.file', (els) => els.map((e) => ({
          name: e.querySelector('.file-name').textContent,
          state: e.dataset.state,
          lines: Array.from(e.querySelectorAll('.fps-line')).map((l) => ({
            tag: (l.querySelector('.fp-tag') || { textContent: '' }).textContent,
            fps: Array.from(l.querySelectorAll('.fp')).map((f) => ({ algo: f.querySelector('.fp-algo').textContent, short: f.querySelector('code').textContent, copyLabel: f.querySelector('button.copy').getAttribute('aria-label') })),
          })),
          status: e.querySelector('.file-side').innerText.replace(/\s+/g, ' ').trim(),
        })));
        const hashesOf = () => page.$$eval('#detailContent [data-hash]', (els) => Object.fromEntries(els.map((e) => [e.dataset.hash, e.textContent])));
        out.listBtnVisible = await page.$eval('#hashList', (e) => !!e.offsetParent);
        out.scanned = await grab();
        // the detail of each file, as received
        out.scannedDetail = [];
        for (let i = 0; i < files.length; i++) {
          await page.click(`.file:nth-child(${i + 1}) .file-head`);
          await page.waitForTimeout(150);
          out.scannedDetail.push({ hashes: await hashesOf(), section: await page.$eval('#detailContent', (e) => e.innerText.replace(/\s+/g, ' ')) });
        }
        // copy buttons put the plain hex on the clipboard
        await page.click('.file:nth-child(2) .file-head');
        await page.click('#detailContent button[data-key="copy-before-sha256"]');
        out.copiedSha = await page.evaluate(() => navigator.clipboard.readText());
        out.copyDone = await page.$eval('#detailContent button[data-key="copy-before-sha256"]', (e) => e.hasAttribute('data-done'));
        await page.click('#detailContent button[data-key="copy-before-md5"]');
        out.copiedMd5 = await page.evaluate(() => navigator.clipboard.readText());
        await page.click('.file:nth-child(2) .fps-line button[data-key="copy-before-md5"]');
        out.copiedRowMd5 = await page.evaluate(() => navigator.clipboard.readText());

        // the compare box (file 2 is selected)
        const d0 = (await hashesOf());
        const compare = async (v) => {
          await page.fill('#hashCheck', v);
          return page.evaluate(() => ({
            hint: document.querySelector('#hashHint').textContent.trim(),
            result: document.querySelector('#hashResult').textContent.replace(/\s+/g, ' ').trim(),
            kind: document.querySelector('#hashResult').dataset.kind,
            invalid: document.querySelector('#hashCheck').getAttribute('aria-invalid'),
            live: document.querySelector('#hashResult').getAttribute('role'),
            describedby: document.querySelector('#hashCheck').getAttribute('aria-describedby'),
          }));
        };
        out.cmp = {
          spaced: await compare(d0['before-sha256']),
          upperMd5: await compare(d0['before-md5'].replace(/\s+/g, '').toUpperCase()),
          labelled: await compare('SHA-256: ' + d0['before-sha256'].replace(/\s+/g, '')),
          wrong: await compare('a'.repeat(64)),
          short: await compare('abc'),
          bad: await compare('zzzz'),
          empty: await compare(''),
        };
        await page.fill('#hashCheck', 'a'.repeat(64));
        await page.click('.file:nth-child(1) .file-head');
        await page.click('.file:nth-child(2) .file-head');
        out.cmpKept = await page.$eval('#hashCheck', (e) => e.value);
        await page.fill('#hashCheck', '');

        // clean everything: before and after appear in each row and in the detail
        await cleanAll(page);
        out.cleaned = await grab();
        out.cleanedHrefs = await page.$$eval('.file', (els) => els.map((e) => (e.querySelector('a.st-link') || {}).getAttribute && e.querySelector('a.st-link').getAttribute('href')));
        // the bytes each download link really serves, hashed here (the servers are stopped by the time the tests run)
        out.served = [];
        for (const href of out.cleanedHrefs) {
          const body = await (await page.request.get(new URL(href, page.url()).toString())).body();
          out.served.push({ sha256: nodeCrypto.createHash('sha256').update(body).digest('hex'), md5: nodeCrypto.createHash('md5').update(body).digest('hex'), bytes: body.length });
        }
        out.cleanedDetail = [];
        for (let i = 0; i < files.length; i++) {
          await page.click(`.file:nth-child(${i + 1}) .file-head`);
          await page.waitForTimeout(150);
          out.cleanedDetail.push({
            hashes: await hashesOf(),
            heads: await page.$$eval('#detailContent .hash-set-head', (els) => els.map((e) => e.innerText.replace(/\s+/g, ' ').trim())),
            note: await page.$eval('#detailContent .hash-note', (e) => e.textContent.trim()),
          });
        }
        // compare against the cleaned file (file 2 is selected)
        await page.click('.file:nth-child(2) .file-head');
        const d1 = await hashesOf();
        out.cmpAfter = {
          cleaned: await compare(d1['after-sha256']),
          original: await compare(d1['before-sha256']),
          none: await compare('b'.repeat(32)),
        };
        await page.fill('#hashCheck', '');

        // the hash list
        out.listBtnAfter = await page.$eval('#hashList', (e) => !!e.offsetParent);
        const [dl] = await Promise.all([page.waitForEvent('download'), page.click('#hashList')]);
        out.csvName = dl.suggestedFilename();
        out.csv = require('fs').readFileSync(await dl.path(), 'utf8');
        await page.waitForTimeout(150);
        out.csvAnnounce = await page.$eval('#announce', (e) => e.textContent);
        await page.context().close();
      }

      // ---------------------------------------------------------------- 3. the list and the detail pane
      {
        const page = await open('selection', { width: 1280, height: 800 });
        await add(page, ['plain.png', 'photo.jpg', 'tiny.jpg']);
        const current = () => page.$$eval('.file', (els) => els.map((e) => e.querySelector('.file-head').getAttribute('aria-current') === 'true'));
        const title = () => page.$eval('#detailTitle', (e) => e.textContent);
        out.sel = { first: await current(), firstTitle: await title() };
        await page.click('.file:nth-child(3) .file-head');
        out.sel.third = await current(); out.sel.thirdTitle = await title();
        out.sel.selectedAttr = await page.$$eval('.file', (els) => els.map((e) => e.dataset.selected));
        // arrow keys walk the list, Enter keeps the pane in step
        await page.focus('.file:nth-child(3) .file-head');
        await page.keyboard.press('ArrowUp');
        out.sel.arrowUp = await page.evaluate(() => Array.from(document.querySelectorAll('.file .file-head')).indexOf(document.activeElement));
        await page.keyboard.press('Enter');
        out.sel.afterEnter = await title();
        await page.keyboard.press('ArrowDown'); await page.keyboard.press('ArrowDown'); await page.keyboard.press('ArrowDown');
        out.sel.arrowEnd = await page.evaluate(() => Array.from(document.querySelectorAll('.file .file-head')).indexOf(document.activeElement));
        // the detail is a pane beside the list, not a sheet
        out.sel.wideSheet = await page.$eval('#detail', (e) => e.dataset.sheet);
        out.sel.wideDialog = await page.$eval('#detail', (e) => e.getAttribute('role'));
        // removing the selected file moves the selection to its neighbour
        await page.click('.file:nth-child(2) .file-head');
        await page.click('#detailContent [data-key=remove]');
        await page.waitForTimeout(150);
        out.sel.afterRemove = { title: await title(), n: (await page.$$('.file')).length, current: await current() };
        await page.click('#clear'); await page.click('#clear');
        await page.waitForTimeout(150);
        out.sel.afterClear = {
          empty: await page.$eval('#body', (e) => e.dataset.empty), n: (await page.$$('.file')).length,
          detail: squash(await page.$eval('#detailContent', (e) => e.textContent)),
          listBtn: await page.$eval('#hashList', (e) => !!e.offsetParent),
        };
        await page.context().close();
      }

      // ---------------------------------------------------------------- 4. the full-screen sheet on a narrow screen
      {
        const page = await open('sheet', { width: 375, height: 700, touch: true });
        await add(page, ['plain.png', 'photo.jpg']);
        const state = () => page.evaluate(() => ({
          sheet: document.querySelector('#detail').dataset.sheet,
          role: document.querySelector('#detail').getAttribute('role'),
          modal: document.querySelector('#detail').getAttribute('aria-modal'),
          bodyClass: document.body.classList.contains('sheet-open'),
          inert: ['.bar', '#options', '#listPane', '#foot'].map((s) => document.querySelector(s).inert),
          focus: document.activeElement && (document.activeElement.id || document.activeElement.className),
          detailVisible: (() => { const r = document.querySelector('#detail').getBoundingClientRect(); return r.width > 0 && r.right > 0 && r.left < window.innerWidth; })(),
        }));
        out.sheet0 = await state();
        await page.tap('.file:nth-child(2) .file-head');
        await page.waitForTimeout(250);
        out.sheetOpen = await state();
        out.sheetRect = await page.$eval('#detail', (e) => { const r = e.getBoundingClientRect(); return { l: r.left, t: r.top, w: r.width, h: r.height, vw: innerWidth, vh: innerHeight }; });
        out.sheetTitle = await page.$eval('#detailTitle', (e) => e.textContent);
        await page.keyboard.press('Escape');
        await page.waitForTimeout(250);
        out.sheetEsc = await state();
        out.sheetEscFocusRow = await page.evaluate(() => document.activeElement === document.querySelectorAll('.file .file-head')[1]);
        await page.tap('.file:nth-child(1) .file-head');
        await page.waitForTimeout(250);
        await page.tap('#sheetBack');
        await page.waitForTimeout(250);
        out.sheetBack = await state();
        out.sheetBackFocusRow = await page.evaluate(() => document.activeElement === document.querySelectorAll('.file .file-head')[0]);
        // widening the window while it is open puts the page back as it was
        await page.tap('.file:nth-child(2) .file-head');
        await page.waitForTimeout(250);
        await page.setViewportSize({ width: 1280, height: 800 });
        await page.waitForTimeout(250);
        out.sheetWidened = await state();
        await page.context().close();
      }

      // ---------------------------------------------------------------- 5. every screen size, in every state
      const SIZES = [[320, 568], [360, 740], [375, 667], [414, 896], [600, 900], [768, 1024], [899, 700], [900, 700], [1024, 768], [1280, 800], [1440, 900], [1920, 1080]];
      out.matrix = [];
      const probe = (page) => page.evaluate(() => {
        const vw = document.documentElement.clientWidth;
        const bad = [];
        const clipped = (el) => {
          for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
            const cs = getComputedStyle(p);
            if (cs.overflowX !== 'visible' || cs.overflow !== 'visible') { const r = p.getBoundingClientRect(); if (r.right <= vw + 1 && r.left >= -1) return true; }
          }
          return false;
        };
        for (const el of document.querySelectorAll('body *')) {
          const cs = getComputedStyle(el);
          if (cs.display === 'none' || cs.visibility === 'hidden' || el.closest('[hidden]') || el.closest('[inert]') && false) continue;
          const r = el.getBoundingClientRect();
          if (!r.width || !r.height) continue;
          if (r.right > vw + 1 || r.left < -1) {
            if (el.closest('#detail') && document.querySelector('#detail').dataset.sheet === 'closed') continue;
            if (clipped(el)) continue;
            bad.push((el.id ? '#' + el.id : el.tagName.toLowerCase() + (el.className && el.className.baseVal === undefined ? '.' + String(el.className).split(' ')[0] : '')) + ` ${Math.round(r.left)}..${Math.round(r.right)}`);
          }
        }
        return {
          vw, scroll: document.documentElement.scrollWidth, bodyScroll: document.body.scrollWidth, offenders: bad.slice(0, 8),
          appHeight: (document.querySelector('.app') || document.body).getBoundingClientRect().height, vh: innerHeight,
        };
      });
      for (const [w, h] of SIZES) {
        for (const scheme of (w === 375 || w === 1280 ? ['light', 'dark'] : ['light'])) {
          const page = await open(`m${w}${scheme}`, { width: w, height: h, scheme, touch: w < 900 });
          const row = { w, h, scheme, states: {} };
          row.states.empty = await probe(page);
          if (!process.env.SKIP_SHOTS && scheme === 'light') await page.screenshot({ path: `${shotDir}/empty-${w}.png` });
          await add(page, ['plain.png', 'photo.jpg', 'tiny.jpg', 'junk.bin']);
          row.states.list = await probe(page);
          if (scheme === 'light') await page.screenshot({ path: `${shotDir}/list-${w}.png` });
          await page.click('#optionsToggle');
          await page.waitForTimeout(350);
          row.states.options = await probe(page);
          const last = page.locator('#options button.info[data-info="optimize"]');
          await last.scrollIntoViewIfNeeded();
          await last.click();
          await page.waitForTimeout(150);
          row.states.bubble = await probe(page);
          row.bubbleRect = await page.$eval('#infoTip .info-bubble', (e) => { const r = e.getBoundingClientRect(); return { l: r.left, r: r.right, t: r.top, b: r.bottom, vw: innerWidth, vh: innerHeight }; });
          if (scheme === 'light') await page.screenshot({ path: `${shotDir}/bubble-${w}.png` });
          await page.keyboard.press('Escape');
          await page.click('#optionsToggle');
          await page.waitForTimeout(350);
          await cleanAll(page);
          row.states.cleaned = await probe(page);
          await page.click('.file:nth-child(2) .file-head');
          await page.waitForTimeout(300);
          row.states.detail = await probe(page);
          if (scheme === 'light') await page.screenshot({ path: `${shotDir}/detail-${w}.png` });
          row.sheetOpen = await page.$eval('#detail', (e) => e.dataset.sheet);
          out.matrix.push(row);
          await page.context().close();
        }
      }

      // ---------------------------------------------------------------- 6. touch targets on a phone
      {
        const page = await open('touch', { width: 390, height: 800, touch: true });
        await add(page, ['plain.png', 'photo.jpg']);
        await page.click('#optionsToggle');
        await page.waitForTimeout(350);
        const small = () => page.evaluate(() => {
          const out = [];
          // (a switch is operated through its checkbox, which a phone makes a 44px square over the track)
          const sel = 'button, a.btn, a.st-link, select, input[type=text], input[type=password], .switch input, .info, .copy, .copy-box, summary';
          for (const el of document.querySelectorAll(sel)) {
            const cs = getComputedStyle(el);
            if (cs.display === 'none' || cs.visibility === 'hidden' || el.closest('[hidden]') || el.closest('.reveal[data-open="false"]')) continue;
            // a row's name button is stretched over the whole row by a pseudo-element, so its hit area is the row
            if (el.classList.contains('file-head')) continue;
            const r = el.getBoundingClientRect();
            if (!r.width || !r.height) continue;
            // the hit area may be larger than the drawn shape (padding or a pseudo-element)
            if (r.width < 44 || r.height < 44) out.push(`${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}.${String(el.className).split(' ')[0]}[${el.dataset.info || el.name || ''}] ${Math.round(r.width)}x${Math.round(r.height)}`);
          }
          return out;
        });
        out.touchSmall = await small();
        await page.context().close();
      }

      // ---------------------------------------------------------------- 7. text a person can read, light and dark
      out.contrast = {};
      for (const scheme of ['light', 'dark']) {
        const page = await open('contrast-' + scheme, { width: 1280, height: 900, scheme });
        await add(page, ['plain.png', 'photo.jpg', 'tiny.jpg', 'junk.bin', 'exif.jpg']);
        await page.click('#optionsToggle');
        await page.waitForTimeout(350);
        const measure = () => page.evaluate(() => {
          const parse = (c) => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return [0, 0, 0, 0]; const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; };
          const over = (top, bottom) => { const a = top[3]; return [top[0] * a + bottom[0] * (1 - a), top[1] * a + bottom[1] * (1 - a), top[2] * a + bottom[2] * (1 - a), 1]; };
          const lum = ([r, g, b]) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
          const bgOf = (el) => {
            const chain = [];
            for (let p = el; p; p = p.parentElement) chain.push(parse(getComputedStyle(p).backgroundColor));
            let c = parse(getComputedStyle(document.documentElement).backgroundColor);
            if (c[3] === 0) c = [255, 255, 255, 1];
            for (let i = chain.length - 1; i >= 0; i--) if (chain[i][3] > 0) c = over(chain[i], c);
            return c;
          };
          const bad = [];
          let checked = 0;
          const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
          const seen = new Set();
          for (let t = walker.nextNode(); t; t = walker.nextNode()) {
            if (!t.textContent.trim()) continue;
            const el = t.parentElement;
            if (!el || seen.has(el)) continue;
            seen.add(el);
            const cs = getComputedStyle(el);
            if (cs.display === 'none' || cs.visibility === 'hidden' || el.closest('[hidden]') || el.closest('script,style,svg,option')) continue;
            const r = el.getBoundingClientRect();
            if (!r.width || !r.height || r.right < 0 || r.bottom < 0) continue;
            // dimmed on purpose: an option that does not apply to the pictures in the list
            if (el.closest('[data-support="na"], [data-support="unsupported"], :disabled, [aria-disabled="true"]')) continue;
            let o = 1; for (let p = el; p; p = p.parentElement) o *= Number(getComputedStyle(p).opacity);
            if (o < 0.99) continue;
            const fg = over(parse(cs.color), bgOf(el));
            const bg = bgOf(el);
            const a = lum(fg) + 0.05, b = lum(bg) + 0.05;
            const ratio = Math.max(a, b) / Math.min(a, b);
            const px = parseFloat(cs.fontSize), bold = Number(cs.fontWeight) >= 700;
            const need = px >= 24 || (px >= 18.66 && bold) ? 3 : 4.5;
            checked++;
            if (ratio < need) bad.push(`${el.tagName.toLowerCase()}.${String(el.className).split(' ')[0]} "${t.textContent.trim().slice(0, 28)}" ${ratio.toFixed(2)} < ${need}`);
          }
          return { checked, bad };
        });
        out.contrast[scheme + '_options'] = await measure();
        await page.click('#optionsToggle');
        await page.waitForTimeout(350);
        await page.click('.file:nth-child(5) .file-head');
        await page.waitForTimeout(200);
        out.contrast[scheme + '_scanned'] = await measure();
        await cleanAll(page);
        await page.click('.file:nth-child(1) .file-head');
        await page.waitForTimeout(200);
        out.contrast[scheme + '_cleaned'] = await measure();
        await page.context().close();
      }

      out.errors = errors;
      console.log(JSON.stringify(out));
      await browser.close();
    })().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
    """
).replace("squashIn(", "((t) => String(t || '').replace(/\\s+/g, ' ').trim())(")


def _serve(stack, tmp_path, n):
    session = web.Session(str(tmp_path / f"work{n}"))
    httpd, session = web.make_server(0, session)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    stack.callback(session.close)
    stack.callback(httpd.server_close)
    stack.callback(httpd.shutdown)
    port = httpd.server_address[1]
    url = web.launch_url(type("H", (), {"server_address": ("127.0.0.1", port)})(), session.launch_key)
    return Client(port, session), url


# a name a spreadsheet would run as a formula, with a comma and quotes in it to test the quoting of the CSV
EVIL = '=evil, "x".png'
FILES = {}


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("redesign-ui")
    files = tmp_path / "files"
    files.mkdir()
    buf = io.BytesIO()
    _photo(120, 90, seed=4).save(buf, "JPEG", quality=90, exif=fixtures.exif_blob(1, True))
    FILES["photo.jpg"] = buf.getvalue()
    FILES["plain.png"] = _png(_photo(100, 80))
    FILES[EVIL] = _png(_photo(64, 48, seed=9))
    FILES["tiny.jpg"] = _jpeg(_photo(40, 40))
    FILES["junk.bin"] = b"this is not an image" * 40
    FILES["exif.jpg"] = fixtures.jpeg_everything()
    for name, data in FILES.items():
        (files / name).write_bytes(data)
    script = tmp_path / "ui.js"
    script.write_text(SCRIPT)
    shots = os.environ.get("REDESIGN_UI_SCREENSHOTS") or str(tmp_path / "shots")
    os.makedirs(shots, exist_ok=True)
    env = {**os.environ, "PLAYWRIGHT_MODULE": PLAYWRIGHT, "CHROMIUM": CHROMIUM}
    with contextlib.ExitStack() as stack:
        servers = [_serve(stack, tmp_path, n) for n in range(40)]
        proc = subprocess.run(
            ["node", str(script), str(files), shots, *(u for _, u in servers)],
            capture_output=True,
            text=True,
            env=env,
            timeout=1500,
        )
        assert proc.returncode == 0, proc.stderr[-3000:]
        got = json.loads(proc.stdout.strip().splitlines()[-1])
        return got


def _jpeg(im):
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _md5(data):
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _short(hex_):
    """How a row shows a hash: the first and last eight characters."""
    return f"{hex_[:8]}…{hex_[-8:]}"


def _plain(grouped):
    return re.sub(r"\s+", "", grouped)


# ---------------------------------------------------------------------------
# (i) buttons


@needs_browser
@pytest.mark.parametrize("label", ["wide", "phone"])
def test_every_visible_info_button_opens_a_bubble_with_its_explanation(run, label):
    rows = run["info_" + label]
    infos = _info_texts()
    assert len(rows) >= 11, [r["key"] for r in rows]
    for r in rows:
        assert r["expanded"] == "true", r["key"]
        assert r["tip"].endswith(infos[r["key"]].replace("\\'", "'")), r["key"]
        assert r["tipRole"] == "status", "the explanation is read out when it opens"
        assert r["checked"] == r["before"]["checked"], f"opening the explanation of {r['key']} must not flip the switch"
        rect = r["rect"]
        assert rect and 0 <= rect["left"] and rect["right"] <= r["vw"] and 0 <= rect["top"] and rect["bottom"] <= r["vh"], (r["key"], rect)
        assert r["before"]["infoLeft"] >= r["before"]["switchRight"] - 8, "the (i) sits after the switch, not before it"
        assert r["label"].startswith("What does ")
        # Escape closes it, empties the live region and keeps the keyboard where it was
        assert r["afterEscape"] == {"expanded": "false", "tip": "", "focus": True}, r["key"]


@needs_browser
def test_the_info_button_swaps_toggles_off_closes_outside_and_works_from_the_keyboard(run):
    assert run["infoSwap"] == {"a": "false", "b": "true", "bubbles": 1}
    assert run["infoToggleOff"] == "false"
    assert run["infoOutside"] == "false"
    assert run["infoEnter"] == "true"
    assert run["infoTabAway"] == "false", "moving on closes it"
    assert run["deepInfo"] is True, "the Deep scan switch in the bottom bar has one too"


@needs_browser
def test_the_info_button_is_a_comfortable_target_on_a_phone(run):
    wide = run["info_wide"]
    assert all(r["btn"]["w"] >= 24 and r["btn"]["h"] >= 24 for r in wide)
    small = [s for s in run["touchSmall"] if ".info" in s or ".info " in s]
    assert small == [], small


# ---------------------------------------------------------------------------
# hashes


def _row(rows, name):
    return next(r for r in rows if r["name"] == name)


@needs_browser
def test_each_row_shows_the_shortened_sha256_and_md5_of_the_file_as_received(run):
    for name in ("plain.png", "photo.jpg", EVIL):
        data = FILES[name]
        row = _row(run["scanned"], name)
        assert len(row["lines"]) == 1 and row["lines"][0]["tag"] == "", "before cleaning there is one line, with no tag"
        sha, md5 = row["lines"][0]["fps"]
        assert sha["algo"] == "SHA-256" and sha["short"] == _short(_sha(data)), name
        assert md5["algo"] == "MD5" and md5["short"] == _short(_md5(data)), name
        assert sha["copyLabel"] == f"Copy SHA-256 of {name}, before cleaning"


@needs_browser
def test_the_detail_shows_the_full_hashes_in_groups_of_eight(run):
    for name, detail in zip(("plain.png", "photo.jpg", EVIL), run["scannedDetail"], strict=True):
        data = FILES[name]
        h = detail["hashes"]
        assert _plain(h["before-sha256"]) == _sha(data) and _plain(h["before-md5"]) == _md5(data)
        assert all(len(g) == 8 for g in h["before-sha256"].split()) and len(h["before-sha256"].split()) == 8
        assert "File fingerprints As received" in detail["section"]
        assert "MD5 is here to match older records" in detail["section"]
    assert run["listBtnVisible"] is True


@needs_browser
def test_copy_buttons_put_the_plain_hash_on_the_clipboard(run):
    data = FILES["photo.jpg"]
    assert run["copiedSha"] == _sha(data) and run["copiedMd5"] == _md5(data) and run["copiedRowMd5"] == _md5(data)
    assert run["copyDone"] is True


@needs_browser
def test_after_cleaning_every_row_shows_before_and_after_and_they_are_the_real_hashes(run):
    for i, name in enumerate(("plain.png", "photo.jpg", EVIL)):
        row = _row(run["cleaned"], name)
        assert row["state"] == "clean"
        assert [ln["tag"] for ln in row["lines"]] == ["Before", "After"]
        before, after = row["lines"]
        data = FILES[name]
        assert before["fps"][0]["short"] == _short(_sha(data)) and before["fps"][1]["short"] == _short(_md5(data))
        # the "after" hashes are those of the bytes the download link really serves
        served = run["served"][i]
        assert after["fps"][0]["short"] == _short(served["sha256"]), name
        assert after["fps"][1]["short"] == _short(served["md5"]), name
        assert after["fps"][0]["copyLabel"].endswith("after cleaning")
        detail = run["cleanedDetail"][i]
        h = detail["hashes"]
        assert _plain(h["before-sha256"]) == _sha(data) and _plain(h["before-md5"]) == _md5(data)
        assert _plain(h["after-sha256"]) == served["sha256"] and _plain(h["after-md5"]) == served["md5"]
        assert detail["heads"] == ["Before cleaning as received", "After cleaning the new file"]
        if served["sha256"] != _sha(data):
            assert "The values differ because the file was rebuilt" in detail["note"]


@needs_browser
def test_the_compare_box_says_match_no_match_or_what_is_wrong_with_what_was_pasted(run):
    c = run["cmp"]
    assert c["spaced"]["kind"] == "match" and "SHA-256 of this file" in c["spaced"]["result"]
    assert c["upperMd5"]["kind"] == "match" and "MD5 of this file" in c["upperMd5"]["result"] and "MD5 can be forged" in c["upperMd5"]["result"]
    assert c["labelled"]["kind"] == "match", "a pasted 'SHA-256: …' line works"
    assert c["wrong"]["kind"] == "none" and c["wrong"]["result"].startswith("No match.")
    assert c["short"]["kind"] == "short" and "has 3" in c["short"]["hint"] and c["short"]["result"] == ""
    assert c["bad"]["kind"] == "bad" and c["bad"]["invalid"] == "true" and "0 to 9" in c["bad"]["hint"]
    assert c["empty"]["kind"] == "empty" and c["empty"]["hint"] == "" and c["empty"]["result"] == ""
    # the verdict is announced; the box points to its hint
    assert all(v["live"] == "status" and v["describedby"] == "hashHint" for v in c.values())
    assert run["cmpKept"] == "a" * 64, "what was typed is still there after looking at another file and back"


@needs_browser
def test_the_compare_box_tells_the_cleaned_file_from_the_original(run):
    c = run["cmpAfter"]
    assert c["cleaned"]["kind"] == "match" and "of the cleaned file" in c["cleaned"]["result"]
    assert c["original"]["kind"] == "match" and "the file as you added it" in c["original"]["result"]
    assert c["none"]["kind"] == "none" and "before or after cleaning" in c["none"]["result"]


@needs_browser
def test_the_hash_list_is_a_csv_with_every_files_hashes(run):
    assert run["csvName"] == "imgstrip-hashes.csv"
    text = run["csv"]
    assert text.startswith("﻿") and "\r\n" in text
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    assert rows[0] == ["file", "state", "before_sha256", "before_md5", "after_sha256", "after_md5", "saved_as"]
    assert len(rows) == 4
    by_name = {r[0].lstrip("'"): r for r in rows[1:]}
    for name in ("plain.png", "photo.jpg", EVIL):
        r = by_name[name]
        data = FILES[name]
        assert r[1] == "cleaned" and r[2] == _sha(data) and r[3] == _md5(data)
        assert len(r[4]) == 64 and len(r[5]) == 32 and r[6].endswith((".png", ".jpg"))
    evil = next(r for r in rows[1:] if "evil" in r[0])
    assert evil[0].startswith("'="), "a file name that starts with = is not left for a spreadsheet to run as a formula"
    assert "3 files" in run["csvAnnounce"]


@needs_browser
def test_the_hash_list_button_is_only_there_when_there_is_something_to_list(run):
    assert run["listBtnAfter"] is True
    assert run["sel"]["afterClear"]["listBtn"] is False


# ---------------------------------------------------------------------------
# list and detail


@needs_browser
def test_exactly_one_row_is_selected_and_the_detail_follows_it(run):
    s = run["sel"]
    assert s["first"] == [True, False, False] and s["firstTitle"] == "plain.png"
    assert s["third"] == [False, False, True] and s["thirdTitle"] == "tiny.jpg"
    assert s["selectedAttr"] == ["false", "false", "true"]
    assert s["arrowUp"] == 1 and s["afterEnter"] == "photo.jpg"
    assert s["arrowEnd"] == 2, "the arrow keys stop at the last row"
    assert s["wideSheet"] == "closed" and s["wideDialog"] is None, "on a wide screen the detail is a pane, not a dialog"


@needs_browser
def test_removing_the_selected_file_selects_a_neighbour_and_clearing_leaves_the_empty_state(run):
    a = run["sel"]["afterRemove"]
    assert a["n"] == 2 and a["current"].count(True) == 1 and a["title"] in ("plain.png", "tiny.jpg")
    c = run["sel"]["afterClear"]
    assert c["empty"] == "true" and c["n"] == 0 and c["detail"] == ""


@needs_browser
def test_on_a_phone_the_detail_is_a_full_screen_sheet_that_manages_focus(run):
    assert run["sheet0"]["sheet"] == "closed" and run["sheet0"]["role"] is None and run["sheet0"]["inert"] == [False] * 4
    o = run["sheetOpen"]
    assert o["sheet"] == "open" and o["role"] == "dialog" and o["modal"] == "true" and o["bodyClass"] is True
    assert o["inert"] == [True] * 4, "the page behind the sheet can't be reached"
    assert o["focus"] == "detailTitle" and run["sheetTitle"] == "photo.jpg"
    r = run["sheetRect"]
    assert r["l"] == 0 and r["t"] == 0 and r["w"] == r["vw"] and r["h"] == r["vh"], "it covers the screen"
    for key in ("sheetEsc", "sheetBack"):
        s = run[key]
        assert s["sheet"] == "closed" and s["role"] is None and s["inert"] == [False] * 4 and s["bodyClass"] is False, key
    assert run["sheetEscFocusRow"] and run["sheetBackFocusRow"], "closing puts the keyboard back on the row"
    w = run["sheetWidened"]
    assert w["sheet"] == "closed" and w["inert"] == [False] * 4 and w["bodyClass"] is False


# ---------------------------------------------------------------------------
# every size


SIZES = [(320, 568), (360, 740), (375, 667), (414, 896), (600, 900), (768, 1024), (899, 700), (900, 700), (1024, 768), (1280, 800), (1440, 900), (1920, 1080)]


@needs_browser
def test_nothing_scrolls_sideways_or_hangs_off_the_screen_at_any_size_in_any_state(run):
    seen = 0
    for row in run["matrix"]:
        for state, p in row["states"].items():
            seen += 1
            where = f"{row['w']}px {row['scheme']} {state}"
            assert p["scroll"] <= p["vw"] and p["bodyScroll"] <= p["vw"], (where, p)
            assert p["offenders"] == [], (where, p["offenders"])
    assert {(r["w"], r["h"]) for r in run["matrix"]} == set(SIZES)
    assert seen == 6 * len(run["matrix"])


@needs_browser
def test_the_explanation_bubble_stays_on_screen_at_every_size(run):
    for row in run["matrix"]:
        b = row["bubbleRect"]
        where = f"{row['w']}px"
        assert 0 <= b["l"] and b["r"] <= b["vw"] and 0 <= b["t"] and b["b"] <= b["vh"], (where, b)


@needs_browser
def test_the_layout_switches_at_900px(run):
    for row in run["matrix"]:
        narrow = row["w"] < 900
        assert row["states"]["detail"]["vw"] == row["w"]
        # a narrow screen opens the picked file as a sheet; a wide one shows it beside the list
        assert row["sheetOpen"] == ("open" if narrow else "closed"), row["w"]


@needs_browser
def test_the_app_shell_fits_a_wide_screen_without_the_page_scrolling(run):
    for row in run["matrix"]:
        if row["w"] >= 900:
            for state, p in row["states"].items():
                assert p["appHeight"] <= p["vh"] + 1, (row["w"], state, p["appHeight"], p["vh"])


@needs_browser
def test_controls_are_big_enough_to_hit_on_a_phone(run):
    assert run["touchSmall"] == [], run["touchSmall"]


@needs_browser
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("state", ["options", "scanned", "cleaned"])
def test_text_has_enough_contrast_in_light_and_dark(run, scheme, state):
    c = run["contrast"][f"{scheme}_{state}"]
    assert c["checked"] > 30
    assert c["bad"] == [], c["bad"]


@needs_browser
def test_the_page_logs_no_errors(run):
    assert run["errors"] == []
