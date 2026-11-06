"""A real browser drives the Sharing / Evidence / Custom choice in the options tray (skipped without Node, Playwright, Chromium).

The tray opens on three presets instead of thirteen switches. These tests check what a static test cannot: that the
full list is hidden until Custom is chosen, that Sharing and Evidence really set every option, that what a person set up
by hand survives a reload (and an older save is read as Custom), that Evidence is never carried to the next launch, and
that the Deep scan switch and the case and operator text are not part of a preset.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
import threading

import pytest

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
    const out = {};
    (async () => {
      const [url] = process.argv.slice(2);
      const browser = await chromium.launch({ executablePath: process.env.CHROMIUM, args: ['--no-sandbox'] });
      const context = await browser.newContext({ viewport: { width: 1280, height: 1000 } });
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
      page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
      const pick = async (v) => { await page.locator('input[name=preset][value=' + v + ']').dispatchEvent('click'); await page.waitForTimeout(350); };
      const preset = () => page.evaluate(() => document.querySelector('input[name=preset]:checked').value);
      const checked = (name) => page.evaluate((n) => document.querySelector('[name=' + n + ']').checked, name);
      const visible = (sel) => page.isVisible(sel);
      const saved = () => page.evaluate(() => JSON.parse(localStorage.getItem('imgstrip.options.v1') || '{}'));
      const ready = async () => {
        await page.waitForSelector('#optionsToggle');
        await page.waitForFunction(() => document.querySelector('#recKey').children.length > 0);
      };

      await page.goto(url);
      await ready();
      await page.click('#optionsToggle');
      await page.waitForTimeout(350);

      // 1. the tray opens on the presets alone
      out.first = { preset: await preset(), switchVisible: await visible('#opt-scrub_lsb'), selectVisible: await visible('#opt-format'),
                    presetVisible: await visible('#preset'), recordPanel: await visible('#record'), toggle: await page.textContent('#optionsToggle') };

      // 2. Custom shows everything
      await pick('custom');
      out.custom = { switchVisible: await visible('#opt-scrub_lsb'), selectVisible: await visible('#opt-format'), toggle: (await page.textContent('#optionsToggle')).trim() };

      // 3. set things up by hand, then Sharing puts every option back (Deep scan and the case text stay)
      await page.locator('#opt-scrub_lsb').dispatchEvent('click');
      await page.locator('#opt-random_name').dispatchEvent('click');
      await page.selectOption('#opt-format', { index: 1 });
      await page.locator('#opt-deep').dispatchEvent('click');
      await page.locator('#opt-record').dispatchEvent('click');
      await page.waitForTimeout(350);
      await page.fill('#rec-case', 'KEEP-ME');
      out.handSet = { lsb: await checked('scrub_lsb'), random: await checked('random_name'), format: await page.inputValue('#opt-format'), saved: await saved() };
      await pick('sharing');
      out.sharing = { preset: await preset(), lsb: await checked('scrub_lsb'), random: await checked('random_name'), format: await page.inputValue('#opt-format'),
                      deep: await checked('deep'), record: await checked('record'), switchVisible: await visible('#opt-scrub_lsb'), saved: await saved(), toggle: (await page.textContent('#optionsToggle')).trim() };
      await pick('custom');
      out.backToCustom = { lsb: await checked('scrub_lsb'), format: await page.inputValue('#opt-format') };

      // 4. Evidence: the standard settings plus the record; the case text is still there
      await page.locator('#opt-scrub_alpha').dispatchEvent('click');
      await pick('evidence');
      out.evidence = { preset: await preset(), alpha: await checked('scrub_alpha'), record: await checked('record'), recordPanel: await visible('#record'),
                       caseText: await page.inputValue('#rec-case'), saved: await saved(), deep: await checked('deep') };

      // 5. a reload: a hand-made setup is kept, Evidence is not
      await page.click('#optionsToggle'); // close
      await pick('custom');
      await page.locator('#opt-scrub_lsb').dispatchEvent('click');
      await page.reload();
      await ready();
      out.reloadCustom = { preset: await preset(), lsb: await checked('scrub_lsb') };
      await pick('evidence');
      await page.reload();
      await ready();
      out.reloadEvidence = { preset: await preset(), record: await checked('record'), caseText: await page.inputValue('#rec-case'), saved: await saved() };

      // 6. an older save (no preset): defaults mean Sharing, anything else means Custom
      await page.evaluate(() => localStorage.setItem('imgstrip.options.v1', JSON.stringify({ format: '', icc: 'convert', auto_orient: true, scrub_lsb: false })));
      await page.reload();
      await ready();
      out.oldDefaults = { preset: await preset() };
      await page.evaluate(() => localStorage.setItem('imgstrip.options.v1', JSON.stringify({ scrub_lsb: true, icc: 'drop' })));
      await page.reload();
      await ready();
      out.oldChanged = { preset: await preset(), lsb: await checked('scrub_lsb'), icc: await page.inputValue('#opt-icc') };
      await page.evaluate(() => localStorage.setItem('imgstrip.options.v1', JSON.stringify({ preset: 'evidence', record: true })));
      await page.reload();
      await ready();
      out.forgedEvidence = { preset: await preset(), record: await checked('record') };

      out.errors = errors;
      console.log(JSON.stringify(out));
      await browser.close();
    })().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
    """
)


def test_the_presets_end_to_end(app, tmp_path):
    script = tmp_path / "ui.js"
    script.write_text(SCRIPT)
    url = web.launch_url(type("H", (), {"server_address": ("127.0.0.1", app.port)})(), app.session.launch_key)
    env = {**os.environ, "PLAYWRIGHT_MODULE": PLAYWRIGHT, "CHROMIUM": CHROMIUM}
    proc = subprocess.run(["node", str(script), url], capture_output=True, text=True, env=env, timeout=240)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])

    assert got["first"]["preset"] == "sharing" and got["first"]["presetVisible"] is True
    assert got["first"]["switchVisible"] is False and got["first"]["selectVisible"] is False and got["first"]["recordPanel"] is False
    assert got["first"]["toggle"] == "Options"
    assert got["custom"] == {"switchVisible": True, "selectVisible": True, "toggle": "Options · Custom"}

    assert got["handSet"]["lsb"] and got["handSet"]["random"] and got["handSet"]["format"] != ""
    assert got["handSet"]["saved"]["preset"] == "custom" and got["handSet"]["saved"]["scrub_lsb"] is True
    s = got["sharing"]
    assert s["preset"] == "sharing" and s["lsb"] is False and s["random"] is False and s["format"] == ""
    assert s["deep"] is True, "Deep scan is not part of a preset"
    assert s["record"] is False, "Sharing turns the record off"
    assert s["switchVisible"] is False and s["toggle"] == "Options"
    assert s["saved"]["preset"] == "sharing" and s["saved"]["scrub_lsb"] is False
    assert got["backToCustom"] == {"lsb": False, "format": ""}, "Sharing reset the options; Custom starts from there"

    e = got["evidence"]
    assert e["preset"] == "evidence" and e["alpha"] is False, "Evidence starts from the standard settings"
    assert e["record"] is True and e["recordPanel"] is True and e["caseText"] == "KEEP-ME" and e["deep"] is True
    assert e["saved"]["preset"] == "sharing" and "record" not in e["saved"], "Evidence is saved as Sharing, with no record"

    assert got["reloadCustom"] == {"preset": "custom", "lsb": True}
    assert got["reloadEvidence"] == {"preset": "sharing", "record": False, "caseText": "", "saved": got["reloadEvidence"]["saved"]}
    assert got["reloadEvidence"]["saved"]["preset"] == "sharing"
    assert got["oldDefaults"] == {"preset": "sharing"}
    assert got["oldChanged"] == {"preset": "custom", "lsb": True, "icc": "drop"}
    assert got["forgedEvidence"] == {"preset": "sharing", "record": False}, "a saved Evidence or record switch is never believed"
    assert got["errors"] == []
