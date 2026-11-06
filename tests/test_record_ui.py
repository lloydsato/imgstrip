"""A real browser drives the processing-record UI (skipped when Node, Playwright or Chromium are missing).

It checks what static tests cannot: that the switch reveals the panel, that a key can be made and its file saved,
that the case, operator and passphrase never reach browser storage, that the record is offered apart from the image,
and that a downloaded record can be checked on the page, and an edited one cannot.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import textwrap
import threading

import pytest
from PIL import Image

from imgstrip import record
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
    const fs = require('fs');
    const out = {};
    (async () => {
      const [url, photo, workdir] = process.argv.slice(2);
      const browser = await chromium.launch({ executablePath: process.env.CHROMIUM, args: ['--no-sandbox'] });
      const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1280, height: 1000 } });
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
      page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
      const open = async (selector) => (await page.getAttribute(selector, 'data-open')) === 'true';
      const flip = () => page.locator('#recordSwitch input').dispatchEvent('click');
      const stored = () => page.evaluate(() => JSON.stringify([localStorage, sessionStorage].map((s) => Object.fromEntries(Object.entries(s)))));
      const text = async (sel) => (await page.textContent(sel)).replace(/\s+/g, ' ').trim();
      const saveDownload = async (trigger, name) => {
        const [dl] = await Promise.all([page.waitForEvent('download'), trigger()]);
        const target = workdir + '/' + name;
        await dl.saveAs(target);
        return { suggested: dl.suggestedFilename(), path: target, text: fs.readFileSync(target, 'utf8') };
      };

      await page.goto(url);
      await page.waitForSelector('#optionsToggle');
      await page.waitForFunction(() => document.querySelector('#recKey').children.length > 0);
      await page.click('#optionsToggle');
      out.initial = { checked: await page.isChecked('#recordSwitch input'), panelOpen: await open('#record') };

      await flip();
      await page.waitForTimeout(300);
      out.ticked = { checked: await page.isChecked('#recordSwitch input'), panelOpen: await open('#record'), key: await text('#recKey'), hints: await text('#recHints') };

      await page.fill('#rec-case', 'CASE-2026-114');
      await page.fill('#rec-operator', 'Det. Example');

      // an unsigned record first
      await page.setInputFiles('#picker', photo);
      await page.waitForSelector('.file[data-state="scanned"]');
      await page.click('#primary');
      await page.waitForSelector('.file[data-state="clean"]', { timeout: 30000 });
      await page.click('.file-head');
      await page.waitForSelector('.credential');
      out.unsigned = { block: await text('.credential'), held: await text('#recHeld') };

      // make a key (protected); the private key file is saved by the browser
      await page.locator('#recKey [data-key="new"]').dispatchEvent('click');
      await page.waitForSelector('#recNewForm:not([hidden])');
      await page.fill('#rec-newpass', 'correct horse battery');
      const priv = await saveDownload(() => page.click('#recNewMake'), 'private.pem');
      await page.waitForSelector('.cred-card');
      out.keyMade = { suggested: priv.suggested, encrypted: priv.text.includes('ENCRYPTED PRIVATE KEY'), card: await text('#recKey'), passField: await page.inputValue('#rec-newpass'), formHidden: await page.getAttribute('#recNewForm', 'hidden') };

      // the earlier record was made without this key
      out.stale = await text('.file-side');
      await page.click('#primary');
      await page.waitForFunction(() => document.querySelector('.credential') && document.querySelector('.credential').textContent.includes('Signed with key'), null, { timeout: 30000 });
      out.signed = { block: await text('.credential'), held: await text('#recHeld') };
      out.storage = await stored();

      // the record is its own download, and the image has none of it
      const rec = await saveDownload(() => page.click('[data-key="download-record"]'), 'record.json');
      const img = await saveDownload(() => page.click('[data-key="download"]'), 'clean.jpg');
      out.files = { recName: rec.suggested, imgName: img.suggested, imgHasCase: fs.readFileSync(img.path).includes('CASE-2026-114') };

      // check it on the page: intact, then edited
      await page.setInputFiles('#rec-file', rec.path);
      await page.click('#recCheck');
      await page.waitForFunction(() => document.querySelector('#recVerdict').textContent.length > 0);
      out.checkOk = await text('#recVerdict');
      const edited = workdir + '/edited.json';
      fs.writeFileSync(edited, rec.text.replace('CASE-2026-114', 'CASE-2026-999'));
      await page.setInputFiles('#rec-file', edited);
      await page.click('#recCheck');
      await page.waitForFunction(() => document.querySelector('#recVerdict').textContent.includes('cannot be trusted'));
      out.checkBad = await text('#recVerdict');
      await page.setInputFiles('#rec-file', []);
      await page.click('#recCheck');
      out.checkNone = await text('#recVerdict');

      // the key can be removed; the next clean is stale again and unsigned
      await page.locator('#record [data-key="remove"]').dispatchEvent('click');
      await page.waitForSelector('#recKey [data-key="load"]');
      out.removed = await text('#recKey');

      // load the key again from its file, wrong passphrase then the right one
      await page.click('#recKey [data-key="load"]');
      await page.setInputFiles('#rec-keyfile', priv.path);
      await page.fill('#rec-pass', 'WRONG-PASSPHRASE-CANARY');
      await page.click('#recKeyLoad');
      await page.waitForSelector('#recKeyError:not([hidden])');
      out.wrong = { error: await text('#recKeyError'), passField: await page.inputValue('#rec-pass'), focus: await page.evaluate(() => document.activeElement.id) };
      await page.fill('#rec-pass', 'correct horse battery');
      await page.click('#recKeyLoad');
      await page.waitForSelector('.cred-card');
      out.loaded = { card: await text('#recKey'), passField: await page.inputValue('#rec-pass') };
      out.storageAfter = await stored();

      // a reload: the page forgets the switch and the case, the server still holds the key
      await page.reload();
      await page.waitForSelector('#optionsToggle');
      await page.waitForFunction(() => document.querySelector('#recKey').children.length > 0);
      await page.click('#optionsToggle');
      out.reloaded = { checked: await page.isChecked('#recordSwitch input'), panelOpen: await open('#record'), caseValue: await page.inputValue('#rec-case'), operatorValue: await page.inputValue('#rec-operator') };
      await flip();
      await page.waitForSelector('.cred-card');
      out.afterReload = await text('#recKey');

      // keyboard
      await page.focus('#recordSwitch input');
      await page.keyboard.press('Space');
      out.keyboard = await page.isChecked('#recordSwitch input');

      // the (i) button explains it
      await page.click('[data-info="record"]');
      out.info = await text('#infoTip');
      out.errors = errors;
      console.log(JSON.stringify(out));
      await browser.close();
    })().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
    """
)


def test_the_record_ui_end_to_end(app, tmp_path):
    buf = io.BytesIO()
    exif = Image.Exif()
    exif[0x010F] = "Camera Maker"
    Image.new("RGB", (64, 48), (30, 120, 60)).save(buf, "JPEG", exif=exif)
    (tmp_path / "photo.jpg").write_bytes(buf.getvalue())
    work = tmp_path / "dl"
    work.mkdir()
    script = tmp_path / "ui.js"
    script.write_text(SCRIPT)
    url = web.launch_url(type("H", (), {"server_address": ("127.0.0.1", app.port)})(), app.session.launch_key)
    env = {**os.environ, "PLAYWRIGHT_MODULE": PLAYWRIGHT, "CHROMIUM": CHROMIUM}
    proc = subprocess.run(
        ["node", str(script), url, str(tmp_path / "photo.jpg"), str(work)], capture_output=True, text=True, env=env, timeout=240
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])

    assert got["initial"] == {"checked": False, "panelOpen": False}, "off by default, panel hidden"
    assert got["ticked"]["checked"] and got["ticked"]["panelOpen"]
    assert "No signing key loaded" in got["ticked"]["key"] and "unsigned" in got["ticked"]["hints"]

    assert "Not signed" in got["unsigned"]["block"] and "never share it with the image" in got["unsigned"]["block"]
    assert "1 record ready" in got["unsigned"]["held"]

    assert got["keyMade"]["suggested"] == "imgstrip-record-key.pem" and got["keyMade"]["encrypted"] is True
    assert "Ed25519" in got["keyMade"]["card"] and got["keyMade"]["passField"] == "" and got["keyMade"]["formHidden"] is not None
    assert "old options" in got["stale"], "a new key makes the earlier clean stale"

    assert "Signed with key" in got["signed"]["block"] and "Not signed" not in got["signed"]["block"]
    assert "1 record ready" in got["signed"]["held"]

    assert got["files"]["recName"].endswith(record.SUFFIX) and got["files"]["imgName"].endswith(".jpg")
    assert got["files"]["imgHasCase"] is False

    assert "has not been edited" in got["checkOk"] and "CASE-2026-114" in got["checkOk"] and "Det. Example" in got["checkOk"]
    assert "cannot be trusted" in got["checkBad"] and "changed after it was signed" in got["checkBad"]
    assert "Choose a record file first" in got["checkNone"]

    assert "No signing key loaded" in got["removed"]
    assert "wrong" in got["wrong"]["error"].lower() and got["wrong"]["passField"] == "" and got["wrong"]["focus"] == "rec-pass"
    assert "Ed25519" in got["loaded"]["card"] and got["loaded"]["passField"] == ""

    # nothing private reaches the browser's storage: not the passphrases, the key, the case or the operator
    for secret in ("WRONG-PASSPHRASE-CANARY", "correct horse battery", "PRIVATE KEY", "CASE-2026-114", "Det. Example", "record_case", "record_operator"):
        assert secret not in got["storage"] and secret not in got["storageAfter"], secret
    assert 'record\\":true' not in got["storageAfter"]

    assert got["reloaded"] == {"checked": False, "panelOpen": False, "caseValue": "", "operatorValue": ""}
    assert "Ed25519" in got["afterReload"], "the server still holds the key"
    assert got["keyboard"] is False, "the space bar toggles the switch"
    assert "never share it with the picture" in got["info"]
    # the only complaint is the browser noting the 422 that the deliberately wrong passphrase earned
    assert [e for e in got["errors"] if "status of 422" not in e] == [] and len(got["errors"]) <= 1
