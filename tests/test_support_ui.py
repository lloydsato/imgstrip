"""A real browser shows which options do not apply to the images in the list (skipped without Node, Playwright and Chromium).

Greyed out with nothing more: the option has nothing to act on. Greyed out with a yellow warning triangle: it cannot be
done for the image. Both come back when the image goes, and neither is ever sent to the server.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import textwrap
import threading

import numpy as np
import pytest
from PIL import Image

from imgstrip.web import server as web
from test_credentials_ui import CHROMIUM, PLAYWRIGHT

pytestmark = pytest.mark.skipif(not (PLAYWRIGHT and CHROMIUM), reason="needs node, playwright and chromium")

SCRIPT = textwrap.dedent(
    r"""
    const { chromium } = require(process.env.PLAYWRIGHT_MODULE);
    (async () => {
      const [dir, shotDir, ...urls] = process.argv.slice(2);
      const browser = await chromium.launch({ executablePath: process.env.CHROMIUM, args: ['--no-sandbox'] });
      const errors = [];
      const out = {};
      const NAMES = ['quality', 'icc', 'deep', 'jpeg_lossless', 'scrub_lsb', 'scrub_dct', 'jpeg_canonical',
                     'scrub_prnu', 'random_name', 'c2pa', 'auto_orient', 'optimize'];

      // Everything the page shows about each option, read from the DOM.
      const snap = (page) => page.evaluate((names) => {
        const res = {};
        for (const n of names) {
          const input = document.querySelector(`[name=${n}]`);
          if (!input) continue;
          const root = input.closest(input.tagName === 'SELECT' ? '.field' : '.switch-row');
          const note = root.querySelector('.support');
          const label = input.tagName === 'SELECT' ? root.querySelector('label') : root.querySelector('.switch-title');
          res[n] = {
            disabled: input.disabled,
            checked: input.type === 'checkbox' ? input.checked : input.value,
            support: root.dataset.support || '',
            note: note && !note.hidden ? note.textContent.replace(/\s+/g, ' ').trim() : '',
            triangle: !!(note && !note.hidden && note.querySelector('use[href="#i-warn"]')),
            opacity: Number(getComputedStyle(label).opacity) * Number(getComputedStyle(input.tagName === 'SELECT' ? root.querySelector('.select') : root.querySelector('.switch-track')).opacity),
            describedby: input.getAttribute('aria-describedby') || '',
          };
        }
        const fmt = document.querySelector('#opt-format');
        res.format = {
          options: Array.from(fmt.options).map((o) => ({ value: o.value, text: o.textContent, disabled: o.disabled })),
          note: (document.querySelector('#support-format') || {}).hidden === false ? document.querySelector('#support-format').textContent.replace(/\s+/g, ' ').trim() : '',
          triangle: !!document.querySelector('#support-format:not([hidden]) use[href="#i-warn"]'),
        };
        return res;
      }, NAMES);

      const rows = (page) => page.$$eval('.file', (els) => els.map((e) => ({
        state: e.dataset.state,
        name: e.querySelector('.file-name').textContent,
        sub: e.querySelector('.file-meta').textContent,
        note: e.querySelector('.file-more-note').textContent.replace(/\s+/g, ' ').trim(),
        status: e.querySelector('.file-side').innerText.replace(/\s+/g, ' ').trim(),  // what is shown, not the hidden Download link
      })));

      async function open(name, url, { route, scheme = 'light', viewport = { width: 900, height: 1500 }, switches = [], format = '' } = {}) {
        const ctx = await browser.newContext({ viewport, colorScheme: scheme });
        const page = await ctx.newPage();
        page.on('pageerror', (e) => errors.push(name + ' pageerror: ' + e.message));
        page.on('console', (m) => {
          // The server answers 422 to a file that is not an image: the browser logs that by itself, and it is expected.
          if (m.type() === 'error' && !(name === 'junk' && m.text().includes('422'))) errors.push(name + ' console: ' + m.text());
        });
        page.requests = [];
        page.on('request', (r) => { if (r.url().includes('/api/')) page.requests.push(new URL(r.url()).pathname); });
        if (route) await page.route('**/api/info', async (r) => {
          const res = await r.fetch();
          const j = await res.json();
          route(j);
          await r.fulfill({ response: res, json: j });
        });
        await page.goto(url);
        await page.waitForSelector('#optionsToggle');
        await page.click('#optionsToggle');
        for (const s of switches) await page.click(`label.switch:has(input[name=${s}])`);
        if (format) await page.selectOption('#opt-format', format);
        return page;
      }
      const add = async (page, ...files) => {
        await page.setInputFiles('#picker', files.map((f) => `${dir}/${f}`));
        await page.waitForFunction(() => !document.querySelector('.file[data-state="scanning"]'), null, { timeout: 60000 });
        await page.waitForTimeout(150);
      };
      const cleanAll = async (page) => {
        await page.click('#primary');
        await page.waitForFunction(() => !document.querySelector('.file[data-state="cleaning"]') && document.querySelector('#primary').dataset.mode !== 'busy', null, { timeout: 120000 });
        await page.waitForTimeout(150);
      };
      const detail = async (page, i = 0) => {
        // The row you pick fills the detail pane (a full-screen sheet on a narrow screen).
        await page.click(`.file:nth-child(${i + 1}) .file-head`);
        await page.waitForTimeout(250);
        return page.$eval('#detailContent', (e) => e.textContent.replace(/\s+/g, ' ').trim());
      };
      let n = 0;
      const next = () => urls[n++];

      // A. nothing uploaded: nothing is ruled out
      {
        const page = await open('empty', next());
        out.empty = await snap(page);
        await page.screenshot({ path: `${shotDir}/empty.png`, fullPage: true });
        await page.close();
      }

      // B. a plain PNG, with switches on that do not apply to it; then clear and see them restored
      {
        const page = await open('png', next(), { switches: ['scrub_dct', 'jpeg_lossless'] });
        out.pngBefore = await snap(page);
        await add(page, 'plain.png');
        out.png = await snap(page);
        await page.screenshot({ path: `${shotDir}/png.png`, fullPage: true });
        out.pngPrefs = await page.evaluate(() => localStorage.getItem('imgstrip.options.v1'));
        await cleanAll(page);
        out.pngRows = await rows(page);
        out.pngSent = null;
        await page.click('#clear'); await page.click('#clear');
        await page.waitForTimeout(200);
        out.pngAfterClear = await snap(page);
        await page.close();
      }

      // C. a tiny JPEG: the camera-fingerprint scrub cannot be done; dark mode too
      {
        const page = await open('tiny', next(), { scheme: 'dark' });
        await add(page, 'tiny.jpg');
        out.tiny = await snap(page);
        await page.screenshot({ path: `${shotDir}/tiny-dark.png`, fullPage: true });
        out.tinyTriangleFill = await page.$eval('#support-scrub_prnu svg', (s) => getComputedStyle(s.querySelector('use').parentNode).display);
        await page.close();
      }

      // D. a JPEG, bit-exact mode on: pixel scrubs go away with a warning; off again and they return
      {
        const page = await open('exact', next());
        await add(page, 'photo.jpg');
        out.jpegPlain = await snap(page);
        await page.click('label.switch:has(input[name=jpeg_lossless])');
        out.jpegExact = await snap(page);
        await page.screenshot({ path: `${shotDir}/jpeg-exact.png`, fullPage: true });
        await page.click('label.switch:has(input[name=jpeg_lossless])');
        out.jpegBack = await snap(page);
        // keyboard: a disabled switch cannot take focus, so Tab skips it
        await page.click('label.switch:has(input[name=scrub_dct])');
        out.dctOn = await snap(page);
        await page.close();
      }

      // E. a JPEG saved as PNG: PNG options appear, JPEG ones go
      {
        const page = await open('jpg2png', next(), { format: 'PNG' });
        await add(page, 'photo.jpg');
        out.jpgAsPng = await snap(page);
        await page.selectOption('#opt-format', '');
        out.jpgSame = await snap(page);
        await page.close();
      }

      // F. a mixed list with the JPEG options on: nothing is refused, the PNG just skips them
      {
        const page = await open('mixed', next(), { switches: ['scrub_dct', 'scrub_prnu'] });
        await add(page, 'photo.jpg', 'plain.png');
        out.mixed = await snap(page);
        await cleanAll(page);
        out.mixedRows = await rows(page);
        out.mixedPng = await detail(page, 1);
        out.mixedJpg = await detail(page, 0);
        await page.close();
      }

      // G. a tiny and a normal JPEG with the fingerprint scrub on: usable, skipped for the tiny one, and said so
      {
        const page = await open('partial', next(), { switches: ['scrub_prnu'] });
        await add(page, 'tiny.jpg', 'photo.jpg');
        out.partial = await snap(page);
        await cleanAll(page);
        out.partialRows = await rows(page);
        out.partialTiny = await detail(page, 0);
        out.partialSent = page.requests.filter((r) => r === '/api/clean').length;
        await page.close();
      }

      // H. a 1-bit PNG: least-significant bits cannot be scrubbed
      {
        const page = await open('onebit', next(), { switches: ['scrub_lsb'] });
        await add(page, 'onebit.png');
        out.onebit = await snap(page);
        await cleanAll(page);
        out.onebitRows = await rows(page);
        await page.close();
      }

      // I. a long thin PNG with WebP chosen: WebP cannot hold it
      {
        const page = await open('thin', next(), { format: 'WEBP' });
        await add(page, 'thin.png');
        out.thin = await snap(page);
        await page.screenshot({ path: `${shotDir}/thin.png`, fullPage: true });
        await page.selectOption('#opt-format', 'JPEG');
        out.thinJpeg = await snap(page);
        await page.close();
      }

      // J. an image over the megapixel limit (limit lowered) and a file over the size limit: blocked, never uploaded
      {
        const page = await open('big', next(), { route: (j) => { j.limits.max_pixels = 2000; j.limits.max_upload = 4000; j.max_upload = 4000; } });
        await add(page, 'onebit.png', 'noise-big.png');
        out.big = await rows(page);
        out.bigRequests = page.requests.filter((r) => r === '/api/inspect' || r === '/api/preview' || r === '/api/clean').length;
        out.bigDetailPixels = await detail(page, 0);
        out.bigDetailBytes = await detail(page, 1);
        out.bigOptions = await snap(page);
        out.bigPrimary = await page.$eval('#primary', (e) => ({ mode: e.dataset.mode, busy: e.getAttribute('aria-disabled') }));
        await page.close();
      }

      // K. Deep scan past its pixel limit (limit lowered)
      {
        const page = await open('deep', next(), { route: (j) => { j.limits.deep_scan_max_pixels = 1000; } });
        await add(page, 'photo.jpg');
        out.deep = await snap(page);
        await page.close();
      }

      // L. a phone-width screen with notes showing: nothing overflows sideways
      {
        const page = await open('phone', next(), { viewport: { width: 380, height: 1800 } });
        await add(page, 'tiny.jpg');
        out.phone = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, width: document.documentElement.clientWidth }));
        await page.screenshot({ path: `${shotDir}/phone.png`, fullPage: true });
        await page.close();
      }

      // M. a file the server cannot read as an image at all: nothing is ruled out
      {
        const page = await open('junk', next());
        await add(page, 'junk.bin');
        out.junk = await snap(page);
        out.junkRows = await rows(page);
        await page.close();
      }

      // N. removing one of two images re-judges the rest
      {
        const page = await open('remove', next(), { switches: ['scrub_dct'] });
        await add(page, 'plain.png', 'photo.jpg');
        out.removeBoth = await snap(page);
        await page.click('.file:nth-child(1) .file-head');
        await page.click('#detailContent [data-key=remove]');
        await page.waitForTimeout(200);
        out.removeOne = await snap(page);
        await page.close();
      }

      out.errors = errors;
      console.log(JSON.stringify(out));
      await browser.close();
    })().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
    """
)


def _png(im):
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _photo(w, h, seed=1):
    rng = np.random.default_rng(seed)
    x = np.linspace(0, 255, w, dtype="float32")[None, :]
    y = np.linspace(0, 255, h, dtype="float32")[:, None]
    base = np.stack([np.broadcast_to(x, (h, w)), np.broadcast_to(y, (h, w)), np.full((h, w), 80.0)], 2)
    return Image.fromarray((base + rng.normal(0, 5, base.shape)).clip(0, 255).astype("uint8"))


def _serve(stack, tmp_path, n):
    session = web.Session(str(tmp_path / f"work{n}"))
    httpd, session = web.make_server(0, session)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    stack.callback(session.close)
    stack.callback(httpd.server_close)
    stack.callback(httpd.shutdown)
    return web.launch_url(type("H", (), {"server_address": ("127.0.0.1", httpd.server_address[1])})(), session.launch_key)


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("support-ui")
    files = tmp_path / "files"
    files.mkdir()
    _photo(120, 90).save(files / "photo.jpg", quality=90)
    _photo(40, 40).save(files / "tiny.jpg", quality=90)
    (files / "plain.png").write_bytes(_png(_photo(100, 80)))
    (files / "onebit.png").write_bytes(_png(_photo(64, 64).convert("L").convert("1")))
    (files / "thin.png").write_bytes(_png(Image.fromarray(np.tile(np.arange(20000, dtype="uint8"), (4, 1)))))
    (files / "noise-big.png").write_bytes(_png(Image.fromarray(np.random.default_rng(3).integers(0, 255, (100, 100, 3), dtype="uint8"))))
    (files / "junk.bin").write_bytes(b"this is not an image" * 40)
    script = tmp_path / "ui.js"
    script.write_text(SCRIPT)
    shots = os.environ.get("SUPPORT_UI_SCREENSHOTS") or str(tmp_path / "shots")
    os.makedirs(shots, exist_ok=True)
    env = {**os.environ, "PLAYWRIGHT_MODULE": PLAYWRIGHT, "CHROMIUM": CHROMIUM}
    with contextlib.ExitStack() as stack:
        urls = [_serve(stack, tmp_path, n) for n in range(16)]
        proc = subprocess.run(["node", str(script), str(files), shots, *urls], capture_output=True, text=True, env=env, timeout=600)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout.strip().splitlines()[-1])


def _greyed(o, kind=None):
    return o["disabled"] and o["support"] == (kind or o["support"]) and o["support"] in ("na", "unsupported") and o["opacity"] < 0.7


# ---------------------------------------------------------------------------


def test_nothing_is_greyed_out_before_an_image_is_added(run):
    for name, o in run["empty"].items():
        if name == "format":
            assert not any(opt["disabled"] for opt in o["options"])
            continue
        if name == "c2pa":
            continue  # its own availability follows the optional C2PA install, not the images
        assert not o["disabled"] and o["support"] == "" and o["note"] == "", name
        assert o["opacity"] == 1, name


def test_a_png_greys_out_what_has_nothing_to_act_on_without_warnings(run):
    p = run["png"]
    for name in ("jpeg_lossless", "scrub_dct", "jpeg_canonical", "quality", "icc", "auto_orient"):
        assert _greyed(p[name], "na"), name
        assert not p[name]["triangle"], name
        # the note is read out after the switch's own hint, never instead of it
        assert p[name]["note"] and f"support-{name}" in p[name]["describedby"].split(), name
    assert "JPEG files only" in p["jpeg_lossless"]["note"]
    assert "lossless" in p["quality"]["note"]
    # what does apply stays usable
    for name in ("scrub_lsb", "scrub_prnu", "optimize", "random_name", "deep"):
        assert not p[name]["disabled"] and p[name]["support"] == "", name


def test_a_greyed_out_switch_shows_off_and_comes_back_as_it_was_chosen(run):
    before, png, after = run["pngBefore"], run["png"], run["pngAfterClear"]
    assert before["scrub_dct"]["checked"] is True and before["jpeg_lossless"]["checked"] is True
    assert png["scrub_dct"]["checked"] is False and png["jpeg_lossless"]["checked"] is False
    assert after["scrub_dct"]["checked"] is True and after["jpeg_lossless"]["checked"] is True
    assert not after["scrub_dct"]["disabled"] and after["scrub_dct"]["note"] == ""
    prefs = json.loads(run["pngPrefs"])  # the choice itself was never touched
    assert prefs["scrub_dct"] is True and prefs["jpeg_lossless"] is True


def test_a_png_with_jpeg_switches_on_still_cleans(run):
    """The JPEG-only switches the user left on are not sent for a PNG, which used to be refused."""
    assert [r["state"] for r in run["pngRows"]] == ["clean"]


def test_unsupported_options_are_greyed_out_with_a_yellow_warning_triangle(run):
    o = run["tiny"]["scrub_prnu"]
    assert _greyed(o, "unsupported") and o["triangle"]
    assert "too small" in o["note"] and "48" in o["note"]
    assert run["tinyTriangleFill"] != "none"
    # the triangle is the yellow one, not the red-orange alert glyph
    sprite = open(os.path.join(os.path.dirname(web.__file__), "static", "index.html"), encoding="utf-8").read()
    assert 'id="i-warn"' in sprite and "#f6c21a" in sprite


def test_bit_exact_jpeg_greys_out_the_pixel_scrubs_with_warnings_and_they_come_back(run):
    plain, exact, back = run["jpegPlain"], run["jpegExact"], run["jpegBack"]
    assert not plain["scrub_lsb"]["disabled"] and not plain["scrub_prnu"]["disabled"] and not plain["quality"]["disabled"]
    for name in ("scrub_lsb", "scrub_prnu"):
        assert _greyed(exact[name], "unsupported") and exact[name]["triangle"], name
    assert "bit-exact" in exact["scrub_lsb"]["note"]
    assert _greyed(exact["quality"], "na") and not exact["quality"]["triangle"]
    for name in ("scrub_lsb", "scrub_prnu", "quality"):
        assert not back[name]["disabled"] and back[name]["support"] == "", name
    # a JPEG keeps its own switches
    assert not exact["jpeg_lossless"]["disabled"] and not exact["scrub_dct"]["disabled"]
    # scrubbing the coefficients is bit-exact too
    assert run["dctOn"]["scrub_lsb"]["support"] == "unsupported"


def test_saving_a_jpeg_as_png_swaps_which_options_apply(run):
    png, same = run["jpgAsPng"], run["jpgSame"]
    assert png["optimize"]["support"] == "" and same["optimize"]["support"] == "na"
    assert png["quality"]["support"] == "na" and same["quality"]["support"] == ""
    assert png["scrub_dct"]["support"] == "na" and same["scrub_dct"]["support"] == ""
    assert "rebuilt from its pixels" in png["scrub_dct"]["note"]


def test_a_mixed_list_keeps_the_options_that_work_for_some_and_every_image_cleans(run):
    m = run["mixed"]
    assert not m["scrub_dct"]["disabled"] and m["scrub_dct"]["support"] == "partial"
    assert m["scrub_dct"]["note"] == "Applies to 1 of 2 images." and not m["scrub_dct"]["triangle"]
    assert [r["state"] for r in run["mixedRows"]] == ["clean", "clean"]
    assert "Scrub JPEG coefficients: scrubbed" in run["mixedJpg"] or "coefficients" in run["mixedJpg"]
    assert "was not applied" not in run["mixedPng"]  # nothing to act on is not worth a warning


def test_an_option_skipped_for_some_images_says_so_in_the_row_and_the_detail(run):
    p = run["partial"]["scrub_prnu"]
    assert not p["disabled"] and p["support"] == "partial" and p["triangle"]
    assert "Skipped for tiny.jpg" in p["note"] and "too small" in p["note"]
    assert [r["state"] for r in run["partialRows"]] == ["clean", "clean"]
    assert "Disrupt camera fingerprint was not applied to this image" in run["partialTiny"]
    assert run["partialSent"] == 2


def test_one_bit_images_cannot_be_lsb_scrubbed(run):
    o = run["onebit"]["scrub_lsb"]
    assert _greyed(o, "unsupported") and o["triangle"] and "1-bit" in o["note"]
    assert [r["state"] for r in run["onebitRows"]] == ["clean"]
    assert "scrub not applied" not in run["onebitRows"][0]["status"].lower()  # it was never asked, so it is not reported as missed


def test_formats_an_image_cannot_be_saved_as_are_marked_and_the_choice_is_warned_about(run):
    thin = run["thin"]["format"]
    webp = next(o for o in thin["options"] if o["value"] == "WEBP")
    assert webp["disabled"] and "⚠" in webp["text"]
    assert thin["triangle"] and "16,383" in thin["note"] and "thin.png" in thin["note"]
    assert not next(o for o in thin["options"] if o["value"] == "JPEG")["disabled"]
    assert run["thinJpeg"]["format"]["note"] == "" and not run["thinJpeg"]["format"]["triangle"]


def test_images_over_a_limit_are_blocked_and_never_uploaded(run):
    pixels, size = run["big"]
    assert pixels["state"] == "unsupported" and "megapixel limit" in pixels["note"]
    assert size["state"] == "unsupported" and "3.9 KB limit" in size["note"]
    assert pixels["status"] == size["status"] == "Not supported"
    assert "can't be cleaned" in run["bigDetailPixels"] and "can't be cleaned" in run["bigDetailBytes"]
    # the over-size file was neither inspected, previewed nor cleaned; the other was only inspected and previewed
    assert run["bigRequests"] <= 3
    assert run["bigPrimary"]["busy"] == "true"  # nothing left to clean


def test_deep_scan_is_greyed_out_with_a_warning_past_its_pixel_limit(run):
    o = run["deep"]["deep"]
    assert _greyed(o, "unsupported") and o["triangle"] and "pixel scan is limited" in o["note"]


def test_the_notes_fit_a_phone_screen(run):
    assert run["phone"]["scroll"] <= run["phone"]["width"]


def test_an_unreadable_file_rules_nothing_out(run):
    assert [r["state"] for r in run["junkRows"]] == ["unsupported"]
    assert all(not o["disabled"] for k, o in run["junk"].items() if k not in ("format", "c2pa"))


def test_removing_an_image_judges_the_rest_again(run):
    assert run["removeBoth"]["scrub_dct"]["support"] == "partial"
    assert run["removeOne"]["scrub_dct"]["support"] == "" and run["removeOne"]["scrub_dct"]["checked"] is True


def test_the_page_logs_no_errors(run):
    assert run["errors"] == []
