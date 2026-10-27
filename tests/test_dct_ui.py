"""A real browser shows the JPEG coefficient options honestly (skipped without Node, Playwright and Chromium)."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import textwrap
import threading

import pytest

import dct_embedders as E
from imgstrip.web import server as web
from test_credentials_ui import CHROMIUM, PLAYWRIGHT
from test_dct_codec import make
from test_web import Client

pytestmark = pytest.mark.skipif(not (PLAYWRIGHT and CHROMIUM), reason="needs node, playwright and chromium")


SCRIPT = textwrap.dedent(
    r"""
    const { chromium } = require(process.env.PLAYWRIGHT_MODULE);
    (async () => {
      const [jpeg, shot, ...urls] = process.argv.slice(2);  // one single-use launch link per scenario
      const browser = await chromium.launch({ executablePath: process.env.CHROMIUM, args: ['--no-sandbox'] });
      const errors = [];
      const text = (page, sel) => page.$$eval(sel, (els) => els.map((e) => e.textContent.replace(/\s+/g, ' ').trim()));
      const out = { runs: {} };

      async function run(name, switches, url) {
        const page = await browser.newPage({ viewport: { width: 1280, height: 1100 } });
        page.on('pageerror', (e) => errors.push(name + ' pageerror: ' + e.message));
        page.on('console', (m) => { if (m.type() === 'error') errors.push(name + ' console: ' + m.text()); });
        await page.goto(url);
        await page.waitForSelector('#optionsToggle');
        await page.click('#optionsToggle');
        if (name === 'default') {
          out.switches = {};
          for (const n of ['scrub_dct', 'jpeg_canonical', 'jpeg_lossless', 'scrub_lsb']) {
            out.switches[n] = await page.$eval(`input[name=${n}]`, (e) => e.checked);
          }
          out.hints = await text(page, '.switch-row:has(input[name=scrub_dct]) .hint, .switch-row:has(input[name=jpeg_canonical]) .hint');
          if (shot) await page.screenshot({ path: shot, fullPage: true });
        }
        for (const s of switches) await page.click(`label.switch:has(input[name=${s}])`);
        await page.setInputFiles('#picker', [jpeg]);
        await page.waitForSelector('.file[data-state="scanned"]', { timeout: 30000 });
        await page.click('#primary');
        await page.waitForSelector('.file[data-state="clean"]', { timeout: 60000 });
        await page.waitForTimeout(500);
        await page.click('.file-head');
        await page.waitForTimeout(300);
        const r = {
          status: await page.$eval('.file .file-side', (e) => e.innerText.replace(/\s+/g, ' ').trim()),
          detail: await page.$eval('#detailContent', (e) => e.innerText.replace(/\s+/g, ' ').trim()),
        };
        r.href = await page.$eval('a[download]', (a) => a.getAttribute('href'));
        out.runs[name] = r;
        await page.close();
      }

      await run('default', [], urls[0]);
      await run('scrub', ['scrub_dct'], urls[1]);
      await run('canonical', ['jpeg_canonical'], urls[2]);
      out.errors = errors;
      console.log(JSON.stringify(out));
      await browser.close();
    })().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
    """
)


def _serve(stack, tmp_path, n):
    """A started server and its launch link: each link works once, so every browser page gets its own."""
    session = web.Session(str(tmp_path / f"work{n}"))
    httpd, session = web.make_server(0, session)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    stack.callback(session.close)
    stack.callback(httpd.server_close)
    stack.callback(httpd.shutdown)
    url = web.launch_url(type("H", (), {"server_address": ("127.0.0.1", httpd.server_address[1])})(), session.launch_key)
    return Client(httpd.server_address[1], session), url


def test_the_page_offers_the_switches_off_and_reports_what_they_did(tmp_path):
    data, bits = E.make_stego(make(200, 150, seed=2, quality=85), "jsteg", 800)
    jpeg = tmp_path / "secret.jpg"
    jpeg.write_bytes(data)
    script = tmp_path / "ui.js"
    script.write_text(SCRIPT)
    shot = os.environ.get("DCT_UI_SCREENSHOT", "")
    env = {**os.environ, "PLAYWRIGHT_MODULE": PLAYWRIGHT, "CHROMIUM": CHROMIUM}
    with contextlib.ExitStack() as stack:
        servers = [_serve(stack, tmp_path, n) for n in range(3)]
        proc = subprocess.run(
            ["node", str(script), str(jpeg), shot, *(url for _, url in servers)],
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
        assert proc.returncode == 0, proc.stderr
        got = json.loads(proc.stdout.strip().splitlines()[-1])
        scrub_app, canonical_app = servers[1][0], servers[2][0]

        assert got["switches"] == {"scrub_dct": False, "jpeg_canonical": False, "jpeg_lossless": False, "scrub_lsb": False}
        assert any("Costs picture quality" in h for h in got["hints"])

        default = got["runs"]["default"]
        assert "JPEG coefficients" not in default["detail"] and "JPEG coefficients" not in default["status"]

        scrub = got["runs"]["scrub"]
        assert "Hidden data in the JPEG coefficients: scrubbed" in scrub["detail"]
        assert "dB from the original" in scrub["detail"] and "not proof that nothing is hidden" in scrub["detail"]
        assert scrub["status"].startswith("Verified")
        res, body = scrub_app.request("GET", scrub["href"])
        assert res.status == 200 and abs(E.read_back(body, "jsteg", bits) - 0.5) < 0.08

        canonical = got["runs"]["canonical"]
        assert "Hidden data in the JPEG coefficients: canonicalised" in canonical["detail"]
        assert "steganography stored in them" in canonical["detail"] and "survives" in canonical["detail"]
        res, body = canonical_app.request("GET", canonical["href"])
        assert E.read_back(body, "jsteg", bits) == 0.0

        assert got["errors"] == []
