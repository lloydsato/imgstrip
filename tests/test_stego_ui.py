"""A real browser shows the four steganography states honestly (skipped without Node, Playwright and Chromium).

Static tests cannot see what the page *says*: that a Deep scan which finds nothing reads "not detected" with its
caveat (never "nothing found"), that a kept payload withholds the seal, and that a scrub is reported as scrubbed.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
import threading

import numpy as np
import pytest
from PIL import Image

from imgstrip import fixtures
from imgstrip.web import server as web
from test_credentials_ui import CHROMIUM, PLAYWRIGHT
from test_web import Client

pytestmark = pytest.mark.skipif(not (PLAYWRIGHT and CHROMIUM), reason="needs node, playwright and chromium")


@pytest.fixture()
def app(tmp_path):
    session = web.Session(str(tmp_path / "work"))
    httpd, session = web.make_server(0, session)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield Client(httpd.server_address[1], session)
    httpd.shutdown()
    httpd.server_close()
    session.close()


SCRIPT = textwrap.dedent(
    r"""
    const { chromium } = require(process.env.PLAYWRIGHT_MODULE);
    (async () => {
      const [url, holiday, memo, small] = process.argv.slice(2);
      const browser = await chromium.launch({ executablePath: process.env.CHROMIUM, args: ['--no-sandbox'] });
      // wide enough for the two-pane layout: picking a row fills the detail pane beside the list
      const page = await browser.newPage({ viewport: { width: 1280, height: 1100 } });
      const errors = [];
      page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
      page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
      // page.$$eval is Playwright's DOM query (it runs a function in the page), not JavaScript eval()
      const text = (sel) => page.$$eval(sel, (els) => els.map((e) => e.textContent.replace(/\s+/g, ' ').trim()));
      // what a row shows on its right (its headline), and the line under its name
      const side = () => page.$$eval('.file .file-side', (els) => els.map((e) => e.innerText.replace(/\s+/g, ' ').trim()));
      const notes = () => text('.file .file-more-note');
      // the detail pane for each row in turn
      const details = async () => {
        const out = [];
        const n = (await page.$$('.file-head')).length;
        for (let i = 0; i < n; i++) {
          await page.click(`.file:nth-child(${i + 1}) .file-head`);
          await page.waitForTimeout(250);
          out.push(await page.$eval('#detailContent', (e) => e.innerText.replace(/\s+/g, ' ').trim()));
        }
        return out;
      };
      const states = (n) => page.waitForFunction((k) => document.querySelectorAll(`.file[data-state="${k.s}"]`).length === k.n, { s: n[0], n: n[1] }, { timeout: 30000 });
      const out = {};

      await page.goto(url);
      await page.waitForSelector('#optionsToggle');
      await page.click('#optionsToggle');

      // Without Deep scan the pixels are not scanned, and the row says so
      await page.setInputFiles('#picker', [small]);
      await states(['scanned', 1]);
      out.shallow = { side: (await side())[0], note: (await notes())[0] };

      // Deep scan: one clean photo, one with a payload
      await page.click('label.switch:has(input[name=deep])');
      await page.setInputFiles('#picker', [holiday, memo]);
      await states(['scanned', 3]);
      await page.waitForTimeout(500);
      out.scanned = await side();
      out.scannedNotes = await notes();
      out.scanDetail = await details();
      out.summary = (await text('#summary'))[0];

      // Clean: the clean photo gets its seal, the payload is kept and marked
      await page.click('#primary');
      await states(['clean', 3]);
      await page.waitForTimeout(500);
      out.cleaned = await side();
      out.cleanedNotes = await notes();
      out.cleanDetail = await details();
      out.footer = (await text('#summary'))[0];

      // Scrub: both are scrubbed, and the page says what that does and does not cover
      await page.click('label.switch:has(input[name=scrub_lsb])');
      await page.click('#primary');
      // the pane shows the last row picked (the payload): wait until it has been re-cleaned and says so
      await page.waitForFunction(() => !document.querySelector('.file[data-state="cleaning"]') && document.querySelector('#primary').dataset.mode !== 'busy' && /pixels: scrubbed/.test(document.querySelector('#detailContent').textContent), null, { timeout: 30000 });
      await page.waitForTimeout(800);
      out.scrubbed = await side();
      out.scrubDetail = await details();
      out.footerAfter = (await text('#summary'))[0];
      out.errors = errors;
      console.log(JSON.stringify(out));
      await browser.close();
    })().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
    """
)


def test_the_page_tells_the_four_states_apart(app, tmp_path):
    (tmp_path / "holiday.png").write_bytes(_png(fixtures.photo(240, 180, 2)))
    (tmp_path / "memo.png").write_bytes(fixtures.png_lsb_stego())
    (tmp_path / "small.png").write_bytes((tmp_path / "holiday.png").read_bytes())
    script = tmp_path / "ui.js"
    script.write_text(SCRIPT)
    url = web.launch_url(type("H", (), {"server_address": ("127.0.0.1", app.port)})(), app.session.launch_key)
    env = {**os.environ, "PLAYWRIGHT_MODULE": PLAYWRIGHT, "CHROMIUM": CHROMIUM}
    proc = subprocess.run(
        ["node", str(script), url, *(str(tmp_path / n) for n in ("holiday.png", "memo.png", "small.png"))],
        capture_output=True,
        text=True,
        env=env,
        timeout=240,
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])

    assert got["shallow"]["side"] == "No metadata found"
    assert "pixels were not scanned" in got["shallow"]["note"], "no Deep scan: the row says the pixels were not looked at"
    small, holiday, memo = got["scanned"]
    assert holiday == "No metadata found" and memo.startswith("Payload in pixels")
    assert "Not detected in the pixels" in got["scannedNotes"][1]
    holiday_detail, memo_detail = got["scanDetail"][1], got["scanDetail"][2]
    assert "In the pixels Nothing detected" in holiday_detail
    assert "not proof that nothing is hidden" in holiday_detail
    assert "In the pixels Payload found" in memo_detail and "can be a false alarm" in memo_detail
    # "Nothing found" is only ever said with its caveat: either the pixels were scanned and nothing was *detected*, or
    # they were not scanned at all
    for text in got["scanDetail"] + got["scanned"] + got["scannedNotes"] + [got["shallow"]["note"], got["summary"]]:
        assert "Nothing found" not in text or "ot detected" in text or "not scanned" in text, text
        assert "Nothing revealing" not in text

    cleaned = got["cleaned"]
    assert cleaned[1].startswith("Verified") and "Both checks passed" in got["cleanedNotes"][1]
    assert "Hidden data in the pixels: not detected" in got["cleanDetail"][1]
    assert cleaned[2].startswith("Payload kept"), "a flagged payload that nothing scrubbed earns no seal"
    assert "1 kept a pixel payload" in got["footer"]
    assert "detected (likely)" in got["cleanDetail"][2]

    assert all(s.startswith("Verified") for s in got["scrubbed"])
    scrub_detail = got["scrubDetail"][2]
    assert "Hidden data in the pixels: scrubbed" in scrub_detail and "original was flagged" in scrub_detail
    assert "does not cover other ways of hiding data" in scrub_detail
    assert "kept a pixel payload" not in got["footerAfter"]
    assert got["errors"] == []


def _png(arr: np.ndarray) -> bytes:
    import io

    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG")
    return buf.getvalue()


UNAPPLIED_SCRIPT = textwrap.dedent(
    r"""
    const { chromium } = require(process.env.PLAYWRIGHT_MODULE);
    (async () => {
      const [url, mask] = process.argv.slice(2);
      const browser = await chromium.launch({ executablePath: process.env.CHROMIUM, args: ['--no-sandbox'] });
      const page = await browser.newPage({ viewport: { width: 1280, height: 1100 } });
      const errors = [];
      page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
      const text = (sel) => page.$$eval(sel, (els) => els.map((e) => e.textContent.replace(/\s+/g, ' ').trim()));
      // The page is told nothing about the image's header (as when the server cannot read it), so it cannot rule the
      // scrub out in advance and sends it: the engine's own "not applied" answer is what it must then report honestly.
      // (With the facts, the switch is greyed out instead: see test_support_ui.)
      await page.route('**/api/inspect*', async (r) => {
        const res = await r.fetch();
        const j = await res.json();
        j.facts = null;
        await r.fulfill({ response: res, json: j });
      });
      await page.goto(url);
      await page.waitForSelector('#optionsToggle');
      await page.click('#optionsToggle');
      await page.click('label.switch:has(input[name=scrub_lsb])');
      await page.setInputFiles('#picker', [mask]);
      await page.waitForSelector('.file[data-state="scanned"]');
      await page.click('#primary');
      await page.waitForSelector('.file[data-state="clean"]');
      await page.waitForTimeout(600);
      await page.click('.file-head');
      await page.waitForTimeout(300);
      const out = {
        status: await page.$$eval('.file .file-side', (els) => els.map((e) => e.innerText.replace(/\s+/g, ' ').trim())),
        detail: [await page.$eval('#detailContent', (e) => e.innerText.replace(/\s+/g, ' ').trim())],
        seals: await page.$$eval('.file .file-side .seal', (els) => els.length),
        detailSeals: await page.$$eval('#detailContent .seal', (els) => els.length),
        announce: (await text('#announce'))[0],
        errors,
      };
      console.log(JSON.stringify(out));
      await browser.close();
    })().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
    """
)


def test_a_scrub_that_could_not_be_applied_earns_no_seal(app, tmp_path):
    import io

    buf = io.BytesIO()
    Image.fromarray(np.tile(np.array([[0, 255]], np.uint8), (8, 4)), "L").convert("1").save(buf, "PNG")
    (tmp_path / "mask.png").write_bytes(buf.getvalue())
    script = tmp_path / "ui2.js"
    script.write_text(UNAPPLIED_SCRIPT)
    url = web.launch_url(type("H", (), {"server_address": ("127.0.0.1", app.port)})(), app.session.launch_key)
    env = {**os.environ, "PLAYWRIGHT_MODULE": PLAYWRIGHT, "CHROMIUM": CHROMIUM}
    proc = subprocess.run(
        ["node", str(script), url, str(tmp_path / "mask.png")], capture_output=True, text=True, env=env, timeout=240
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got["status"][0].startswith("Scrub not applied")
    assert got["seals"] == 0 and got["detailSeals"] == 0, "no seal when the scrub that was asked for did not happen"
    assert "neither scanned nor destroyed" in got["detail"][0]
    assert "cleaned and verified" in got["announce"] and "1 image cleaned of metadata only" in got["announce"]
    assert got["errors"] == []
