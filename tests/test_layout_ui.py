"""A real browser holds the layout together with many images and many warnings (skipped without Node, Playwright and Chromium).

The page must not break sideways, the action bar must stay on screen however long the list gets, and the warnings must
stay compact: one line until opened, grouped by cause, never taller than a part of the screen.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import textwrap

import numpy as np
import pytest
from PIL import Image

from imgstrip import fixtures
from test_credentials_ui import CHROMIUM, PLAYWRIGHT
from test_support_ui import _photo, _png, _serve

pytestmark = pytest.mark.skipif(not (PLAYWRIGHT and CHROMIUM), reason="needs node, playwright and chromium")

MANY = 40
LONG_NAME = "IMG_20240517_133700_" + "a_really_long_file_name_with_no_breaks_" * 4 + "0001.jpg"

SCRIPT = textwrap.dedent(
    r"""
    const { chromium } = require(process.env.PLAYWRIGHT_MODULE);
    (async () => {
      const [dir, shotDir, names, ...urls] = process.argv.slice(2);
      const many = JSON.parse(names);
      const browser = await chromium.launch({ executablePath: process.env.CHROMIUM, args: ['--no-sandbox'] });
      const errors = [];
      const out = {};
      let n = 0;

      async function open(name, { width = 900, height = 800, scheme = 'light', reduced = false } = {}) {
        const ctx = await browser.newContext({
          viewport: { width, height }, colorScheme: scheme, reducedMotion: reduced ? 'reduce' : 'no-preference',
        });
        const page = await ctx.newPage();
        page.on('pageerror', (e) => errors.push(name + ' pageerror: ' + e.message));
        page.on('console', (m) => {
          // The server answers 422 to a file that is not an image: the browser logs that by itself, and it is expected.
          if (m.type() === 'error' && !m.text().includes('422') && !(name === 'note' && m.text().includes('400'))) errors.push(name + ' console: ' + m.text());
        });
        await page.goto(urls[n++]);
        await page.waitForSelector('#optionsToggle');
        return page;
      }
      const add = async (page, files) => {
        await page.setInputFiles('#picker', files.map((f) => `${dir}/${f}`));
        await page.waitForFunction(() => document.querySelectorAll('.file').length > 0 && !document.querySelector('.file[data-state="scanning"]'), null, { timeout: 120000 });
        await page.waitForTimeout(200);
      };
      const options = async (page, names) => {
        await page.click('#optionsToggle');
        for (const s of names) await page.click(`label.switch:has(input[name=${s}])`);
        await page.click('#optionsToggle');
        await page.waitForTimeout(300);
      };

      // Everything that leaves the window sideways, or makes the page scroll sideways.
      const breaks = (page) => page.evaluate(() => {
        const bad = [];
        const root = document.documentElement;
        if (root.scrollWidth > root.clientWidth + 1) bad.push(`page scrolls sideways: ${root.scrollWidth} > ${root.clientWidth}`);
        const win = document.querySelector('#window').getBoundingClientRect();
        for (const el of document.querySelectorAll('#window *')) {
          const hiddenParent = el.closest('[hidden], .sr-only, .sprite');
          if (hiddenParent) continue;
          const inner = el.closest('.reveal-inner');
          if (inner && getComputedStyle(inner).visibility === 'hidden') continue;
          const cs = getComputedStyle(el);
          if (cs.display === 'none' || cs.visibility === 'hidden' || el.matches('input[type=checkbox]')) continue;
          const r = el.getBoundingClientRect();
          if (!r.width && !r.height) continue;
          if (r.left < win.left - 1 || r.right > win.right + 1) bad.push(`${el.id || el.className || el.tagName} ${Math.round(r.left)}..${Math.round(r.right)} outside ${Math.round(win.left)}..${Math.round(win.right)}`);
        }
        return bad.slice(0, 8);
      });
      // Narrow screens scroll the page; wide ones scroll the list pane inside the app.
      const scrollList = (page, y) => page.evaluate((to) => {
        const pane = document.querySelector('#listPane');
        if (getComputedStyle(pane).overflowY === 'auto') pane.scrollTop = to; else window.scrollTo(0, to);
      }, y);
      const box = (page, sel) => page.$eval(sel, (e) => { const r = e.getBoundingClientRect(); return { top: r.top, bottom: r.bottom, height: r.height, left: r.left, right: r.right }; });

      // A. a long, mixed list with warnings, at every width
      out.sizes = {};
      for (const [width, height] of [[320, 640], [375, 700], [768, 900], [1440, 900]]) {
        const page = await open(`mixed-${width}`, { width, height });
        await add(page, many);
        await options(page, ['scrub_lsb', 'scrub_dct', 'scrub_prnu']);
        const r = {};
        r.count = (await page.textContent('#listCount')).trim();
        r.reviewTitle = (await page.textContent('#reviewTitle')).replace(/\s+/g, ' ').trim();
        r.collapsed = await box(page, '#reviewToggle');
        r.panelHiddenCollapsed = await page.$eval('#reviewPanel', (e) => e.hidden);
        r.breaksClosed = await breaks(page);

        // the action bar and the list top stay on screen wherever the page is scrolled
        r.foot = {};
        for (const [label, y] of [['top', 0], ['middle', 1500], ['bottom', 999999]]) {
          await scrollList(page, y);
          await page.waitForTimeout(150);
          const f = await box(page, '#foot');
          const t = await box(page, '.list-top');
          r.foot[label] = { footTop: f.top, footBottom: f.bottom, listTop: t.top, vh: height };
        }
        await scrollList(page, 0);

        // open the review tray
        await page.click('#reviewToggle');
        await page.waitForTimeout(250);
        r.open = await page.$eval('#reviewToggle', (e) => e.getAttribute('aria-expanded'));
        r.panel = await box(page, '#reviewGroups');
        r.vh = height;
        r.groups = await page.$$eval('.review-group', (gs) => gs.map((g) => ({
          title: g.querySelector('h3 span').textContent,
          n: g.querySelector('.n').textContent.replace(/\D/g, ''),
          text: g.querySelector('p').textContent,
          chips: g.querySelectorAll('.chip:not(.chip-more)').length,
          more: (g.querySelector('.chip-more') || {}).textContent || '',
        })));
        r.breaksOpen = await breaks(page);
        r.footWhileOpen = await box(page, '#foot');

        // "+N more" shows every name, then folds back
        const more = await page.$('.review-group .chip-more');
        if (more) {
          await more.click();
          r.expandedChips = await page.$$eval('.review-group', (gs) => gs[0].querySelectorAll('.chip:not(.chip-more)').length);
          r.expandedBreaks = await breaks(page);
          r.expandedPanel = await box(page, '#reviewGroups');
          await page.click('.review-group .chip-more');
          r.foldedChips = await page.$$eval('.review-group', (gs) => gs[0].querySelectorAll('.chip:not(.chip-more)').length);
        }

        // a chip takes you to its file, open and in view
        await scrollList(page, 0);
        const chip = await page.$('.review-group .chip:not(.chip-more)');
        r.chipName = await chip.getAttribute('title');
        await chip.click();
        // The scroll is smooth, so wait for it to stop (the page position unchanged for 300 ms) instead of guessing a
        // duration: a long list on a slow machine takes more than a second.
        await page.waitForFunction(() => {
          const now = performance.now();
          const w = window.__scrollWatch || (window.__scrollWatch = { y: -1, since: now });
          const y = window.scrollY + document.querySelector('#listPane').scrollTop;
          if (w.y !== y) { w.y = y; w.since = now; }
          return now - w.since > 300;
        }, null, { timeout: 15000, polling: 50 });
        r.chipTarget = await page.evaluate((name) => {
          const row = Array.from(document.querySelectorAll('.file')).find((f) => f.querySelector('.file-name').textContent === name);
          if (!row) return null;
          const rect = row.querySelector('.file-head').getBoundingClientRect();
          return { selected: row.dataset.selected, focused: document.activeElement === row.querySelector('.file-head'), top: rect.top, bottom: rect.bottom, vh: innerHeight };
        }, r.chipName);
        out.sizes[width] = r;
        if (width === 375) await page.screenshot({ path: `${shotDir}/many-375.png` });
        if (width === 1440) await page.screenshot({ path: `${shotDir}/many-1440.png` });
        await page.context().close();
      }

      // B. the options tray never outgrows the screen, notes and all
      {
        const page = await open('tray', { width: 375, height: 700 });
        await add(page, many);
        await page.click('#optionsToggle');
        for (const s of ['scrub_lsb', 'scrub_dct', 'scrub_prnu']) await page.click(`label.switch:has(input[name=${s}])`);
        await page.waitForTimeout(300);
        out.tray = { box: await box(page, '#options > .reveal-inner'), vh: 700, breaks: await breaks(page) };
        out.trayNotes = await page.$$eval('.support:not([hidden])', (els) => els.map((e) => ({
          clamped: e.scrollHeight <= 2.6 * parseFloat(getComputedStyle(e).lineHeight) * 1 + 20, height: e.getBoundingClientRect().height,
        })));
        await page.screenshot({ path: `${shotDir}/tray-375.png` });
        await page.context().close();
      }

      // C. a list with nothing wrong shows no warnings at all, and warnings go when their files do
      {
        const page = await open('clean-list');
        await add(page, ['plain.png']);
        out.noWarnings = { hidden: await page.$eval('#review', (e) => e.hidden) };
        await add(page, ['junk.bin', 'raw.cr2']);
        out.withBad = { hidden: await page.$eval('#review', (e) => e.hidden), title: (await page.textContent('#reviewTitle')).replace(/\s+/g, ' ').trim(), groups: await page.$$eval('.review-group h3 > span:first-child', (e) => e.map((x) => x.textContent)) };
        for (let i = 0; i < 2; i++) {
          await page.click('.file:nth-child(2) .file-head');
          await page.click('#detailContent button[data-key=remove]');
        }
        await page.waitForTimeout(200);
        out.removedBad = { hidden: await page.$eval('#review', (e) => e.hidden), rows: await page.$$eval('.file', (e) => e.length) };
        await add(page, ['junk.bin']);
        await page.click('#reviewToggle');
        await page.click('#clear'); await page.click('#clear');
        await page.waitForTimeout(200);
        out.afterClear = { hidden: await page.$eval('#review', (e) => e.hidden), open: await page.$eval('#reviewPanel', (e) => e.hidden) === false };
        await page.context().close();
      }

      // D. the keyboard: open with Enter, reach the chips, close with Escape and land back on the toggle
      {
        const page = await open('keys');
        await add(page, ['junk.bin', 'raw.cr2']);
        await page.focus('#reviewToggle');
        await page.keyboard.press('Enter');
        await page.waitForTimeout(150);
        out.keys = { open: await page.$eval('#reviewToggle', (e) => e.getAttribute('aria-expanded')) };
        await page.keyboard.press('Tab');
        out.keys.chipFocused = await page.evaluate(() => document.activeElement.classList.contains('chip'));
        await page.keyboard.press('Escape');
        await page.waitForTimeout(150);
        out.keys.closed = await page.$eval('#reviewToggle', (e) => e.getAttribute('aria-expanded'));
        out.keys.focusBack = await page.evaluate(() => document.activeElement.id);
        out.keys.live = (await page.textContent('#reviewLive')).trim();
        await page.context().close();
      }

      // E. dark, and reduced motion
      {
        const page = await open('dark', { scheme: 'dark', width: 375, height: 700 });
        await add(page, many.slice(0, 12));
        await options(page, ['scrub_lsb', 'scrub_dct']);
        out.dark = { breaks: await breaks(page), bg: await page.evaluate(() => getComputedStyle(document.body).backgroundColor) };
        await page.screenshot({ path: `${shotDir}/dark-375.png` });
        await page.context().close();
        const calm = await open('calm', { reduced: true });
        await add(calm, ['plain.png']);
        out.calm = await calm.$eval('.file', (e) => getComputedStyle(e).transitionDuration);
        await calm.context().close();
      }

      // F. a long error in the action bar wraps and stays inside it
      {
        const page = await open('note', { width: 375, height: 700 });
        await add(page, many.slice(0, 6));
        await page.click('#primary');
        await page.waitForFunction(() => !document.querySelector('.file[data-state="cleaning"]') && document.querySelector('#primary').dataset.mode !== 'busy', null, { timeout: 120000 });
        out.cleaned = {
          mode: await page.$eval('#primary', (e) => e.dataset.mode),
          label: (await page.textContent('#primary')).trim(),
          states: await page.$$eval('.file', (e) => e.map((x) => x.dataset.state)),
          subs: await page.$$eval('.file-meta', (e) => e.slice(0, 1).map((x) => x.textContent)),
          pairs: await page.$$eval('.file:first-child .fp-tag', (e) => e.map((x) => x.textContent)),
        };
        await page.click('#saveToggle');
        await page.fill('#folder', 'relative/' + 'very-long-folder-name-'.repeat(12));
        await page.click('#saveSubmit');
        await page.waitForSelector('#footNote:not([hidden])');
        out.note = { foot: await box(page, '#foot'), note: await box(page, '#footNote'), vh: 700, breaks: await breaks(page) };
        await page.screenshot({ path: `${shotDir}/note-375.png` });
        await page.context().close();
      }

      out.errors = errors;
      console.log(JSON.stringify(out));
      await browser.close();
    })().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
    """
)


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("layout-ui")
    files = tmp_path / "files"
    files.mkdir()
    names = []
    for i in range(MANY):
        buf = io.BytesIO()
        Image.fromarray(np.asarray(_photo(120, 90, seed=i + 1))).save(buf, "JPEG", exif=fixtures.exif_blob(1, True))
        (files / f"shot-{i:02d}.jpg").write_bytes(buf.getvalue())
        names.append(f"shot-{i:02d}.jpg")
    (files / LONG_NAME).write_bytes(fixtures.jpeg_everything())
    _photo(40, 40).save(files / "tiny.jpg", quality=90)  # too small for the camera-fingerprint option
    (files / "plain.png").write_bytes(_png(_photo(100, 80)))
    (files / "junk.bin").write_bytes(b"this is not an image" * 40)
    (files / "raw.cr2").write_bytes(b"II*\x00\x10\x00\x00\x00CR\x02\x00" + b"\x00" * 200)
    mixed = [*names, LONG_NAME, "tiny.jpg", "plain.png", "junk.bin", "raw.cr2"]
    script = tmp_path / "ui.js"
    script.write_text(SCRIPT)
    shots = os.environ.get("LAYOUT_UI_SCREENSHOTS") or str(tmp_path / "shots")
    os.makedirs(shots, exist_ok=True)
    env = {**os.environ, "PLAYWRIGHT_MODULE": PLAYWRIGHT, "CHROMIUM": CHROMIUM}
    with contextlib.ExitStack() as stack:
        urls = [_serve(stack, tmp_path, n) for n in range(16)]
        cmd = ["node", str(script), str(files), shots, json.dumps(mixed), *urls]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=900)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout.strip().splitlines()[-1])


WIDTHS = ["320", "375", "768", "1440"]


@pytest.mark.parametrize("width", WIDTHS)
def test_nothing_breaks_sideways_with_a_long_mixed_list(run, width):
    r = run["sizes"][width]
    assert r["count"] == f"{MANY + 5} images"
    assert r["breaksClosed"] == [] and r["breaksOpen"] == [], (r["breaksClosed"], r["breaksOpen"])


@pytest.mark.parametrize("width", WIDTHS)
def test_the_action_bar_stays_on_screen_wherever_the_list_is_scrolled(run, width):
    for where, f in run["sizes"][width]["foot"].items():
        assert 0 <= f["footTop"] < f["vh"] and f["footBottom"] <= f["vh"] + 1, (where, f)
        assert -1 <= f["listTop"] <= f["vh"] / 2, (where, f)  # the count and warnings stay at the top as well


@pytest.mark.parametrize("width", WIDTHS)
def test_warnings_are_one_line_until_opened(run, width):
    r = run["sizes"][width]
    assert r["panelHiddenCollapsed"] is True
    assert r["collapsed"]["height"] <= 60, r["collapsed"]
    assert "need a look" in r["reviewTitle"] and "images" in r["reviewTitle"]


@pytest.mark.parametrize("width", WIDTHS)
def test_opened_warnings_are_grouped_by_cause_and_capped(run, width):
    r = run["sizes"][width]
    assert r["open"] == "true"
    titles = [g["title"] for g in r["groups"]]
    assert len(titles) == len(set(titles)), titles  # one entry per cause, however many images share it
    assert "Can't be cleaned" in titles
    assert any("Scrub JPEG coefficients" in t or "Disrupt camera fingerprint" in t for t in titles)
    assert r["panel"]["height"] <= 0.4 * r["vh"] + 2, r["panel"]  # never more than 40% of the screen
    assert r["footWhileOpen"]["bottom"] <= r["vh"] + 1  # and the action bar is still on screen
    for g in r["groups"]:
        assert g["chips"] <= 3, g  # names are capped at three, the rest sit behind "+N more"
        if int(g["n"]) > 3:
            assert g["more"] == f"+{int(g['n']) - 3} more", g


@pytest.mark.parametrize("width", WIDTHS)
def test_plus_more_shows_every_name_then_folds_back(run, width):
    r = run["sizes"][width]
    first = r["groups"][0]
    big = [g for g in r["groups"] if int(g["n"]) > 3]
    assert big, "the many JPEGs should share at least one cause"
    if int(first["n"]) > 3:
        assert r["expandedChips"] == int(first["n"]) and r["foldedChips"] == 3
        assert r["expandedBreaks"] == [] and r["expandedPanel"]["height"] <= 0.4 * r["vh"] + 2


@pytest.mark.parametrize("width", WIDTHS)
def test_a_chip_takes_you_to_its_file(run, width):
    t = run["sizes"][width]["chipTarget"]
    assert t and t["selected"] == "true" and t["focused"], t
    assert t["top"] >= -1 and t["bottom"] <= t["vh"] + 1, t


def test_the_options_tray_never_outgrows_the_screen(run):
    assert run["tray"]["box"]["height"] <= 0.76 * run["tray"]["vh"] + 2, run["tray"]
    assert run["tray"]["breaks"] == []
    assert all(n["height"] <= 62 for n in run["trayNotes"]), run["trayNotes"]  # notes are held to two lines


def test_warnings_appear_with_a_problem_and_go_with_it(run):
    assert run["noWarnings"]["hidden"] is True
    assert run["withBad"]["hidden"] is False
    assert run["withBad"]["title"] == "1 thing needs a look · 2 images", run["withBad"]
    assert run["withBad"]["groups"] == ["Can't be cleaned"], run["withBad"]  # a RAW file and junk are one cause
    assert run["removedBad"] == {"hidden": True, "rows": 1}
    assert run["afterClear"] == {"hidden": True, "open": False}


def test_the_review_tray_works_from_the_keyboard(run):
    k = run["keys"]
    assert k["open"] == "true" and k["chipFocused"] and k["closed"] == "false" and k["focusBack"] == "reviewToggle"
    assert "warning" in k["live"] and "2 images" in k["live"]


def test_dark_mode_holds_together_on_a_phone(run):
    assert run["dark"]["breaks"] == []
    assert run["dark"]["bg"] != "rgb(233, 239, 236)"


def test_reduced_motion_stops_the_transitions(run):
    assert run["calm"].split(",")[0].strip() in ("0s", "0ms")


def test_a_cleaned_batch_and_a_long_error_stay_inside_the_action_bar(run):
    c = run["cleaned"]
    assert c["states"] == ["clean"] * 6 and c["mode"] == "download" and c["label"] == "Download all (.zip)"
    assert c["subs"][0].startswith("JPEG · ") and c["subs"][0].endswith("KB"), c["subs"]
    assert c["pairs"] == ["Before", "After"], c["pairs"]  # a cleaned row shows the hashes before and after
    n = run["note"]
    assert n["breaks"] == []
    assert n["foot"]["bottom"] <= n["vh"] + 1 and n["foot"]["height"] <= 0.55 * n["vh"] + 2, n
    assert n["note"]["bottom"] <= n["foot"]["bottom"] + 1


def test_the_page_logs_no_errors(run):
    assert run["errors"] == []
