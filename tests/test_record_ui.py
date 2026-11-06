"""A real browser drives the processing-record UI (skipped when Node, Playwright or Chromium are missing).

It checks what static tests cannot: that the Evidence preset reveals the panel, that a key can be made and its file
saved, that the case, operator and passphrase never reach browser storage, that the record is offered apart from the
image, and that a downloaded record can be checked in the Check a record window (with and without the image), and an
edited one cannot.
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
      const pick = (v) => page.locator('input[name=preset][value=' + v + ']').dispatchEvent('click');
      const preset = () => page.evaluate(() => document.querySelector('input[name=preset]:checked').value);
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
      out.initial = { preset: await preset(), checked: await page.isChecked('#recordSwitch input'), panelOpen: await open('#record'), customOpen: await open('#custom'), toggle: await text('#optionsToggle') };

      await pick('evidence');
      await page.waitForTimeout(300);
      out.ticked = { preset: await preset(), checked: await page.isChecked('#recordSwitch input'), panelOpen: await open('#record'), customOpen: await open('#custom'), toggle: await text('#optionsToggle'), summary: await text('#presetSum'), key: await text('#recKey') };

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
      out.signed = { block: await text('.credential'), held: await text('#recHeld'), button: await text('#recDownload'), buttonVisible: await page.isVisible('#recDownload') };
      out.storage = await stored();

      // the record is its own download, and the image has none of it
      const rec = await saveDownload(() => page.click('#recDownload'), 'record.json');
      const img = await saveDownload(() => page.click('[data-key="download"]'), 'clean.jpg');
      out.files = { recName: rec.suggested, imgName: img.suggested, imgHasCase: fs.readFileSync(img.path).includes('CASE-2026-114') };

      // check it in its own window: intact (no image, then the cleaned image, then the original), a wrong image, edited
      await page.click('#checkOpen');
      out.dialogOpen = await page.evaluate(() => document.querySelector('#checkDialog').open);
      out.dialogFocus = await page.evaluate(() => document.activeElement.id);
      const checkWith = async (record, image) => {
        await page.setInputFiles('#rec-file', record || []);
        await page.setInputFiles('#rec-image', image || []);
        await page.evaluate(() => { document.querySelector('#recVerdict').replaceChildren(); });
        await page.click('#recCheck');
        await page.waitForFunction(() => document.querySelector('#recVerdict').textContent.length > 0);
        return text('#recVerdict');
      };
      out.checkOk = await checkWith(rec.path, null);
      out.checkClean = await checkWith(rec.path, img.path);
      out.checkOriginal = await checkWith(rec.path, photo);
      out.checkWrongImage = await checkWith(rec.path, rec.path);
      const edited = workdir + '/edited.json';
      fs.writeFileSync(edited, rec.text.replace('CASE-2026-114', 'CASE-2026-999'));
      out.checkBad = await checkWith(edited, null);
      out.checkNone = await checkWith(null, null);
      // Escape closes it and drops what was chosen
      await page.setInputFiles('#rec-file', rec.path);
      await page.keyboard.press('Escape');
      // the page resets the form in the dialog's close event, which the browser delivers a moment after the key
      await page.waitForFunction(() => !document.querySelector('#checkDialog').open && document.querySelector('#rec-file').files.length === 0 && document.querySelector('#recVerdict').children.length === 0);
      out.dialogClosed = { open: await page.evaluate(() => document.querySelector('#checkDialog').open), file: await page.evaluate(() => document.querySelector('#rec-file').files.length), verdict: await page.evaluate(() => document.querySelector('#recVerdict').children.length) };

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
      out.reloaded = { preset: await preset(), checked: await page.isChecked('#recordSwitch input'), panelOpen: await open('#record'), caseValue: await page.inputValue('#rec-case'), operatorValue: await page.inputValue('#rec-operator') };
      // the arrow keys move through the presets and leave the focus on the choice
      await page.focus('input[name=preset][value=sharing]');
      await page.keyboard.press('ArrowRight');
      await page.waitForSelector('.cred-card');
      out.arrow = { preset: await preset(), focus: await page.evaluate(() => document.activeElement.name + ':' + document.activeElement.value) };
      out.afterReload = await text('#recKey');

      // Custom keeps the record on and shows its switch; the space bar toggles it
      await pick('custom');
      await page.waitForTimeout(300);
      out.custom = { customOpen: await open('#custom'), panelOpen: await open('#record'), checked: await page.isChecked('#recordSwitch input') };
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

    assert got["initial"] == {"preset": "sharing", "checked": False, "panelOpen": False, "customOpen": False, "toggle": "Options"}
    assert got["ticked"]["preset"] == "evidence" and got["ticked"]["checked"] and got["ticked"]["panelOpen"]
    assert got["ticked"]["customOpen"] is False, "Evidence hides the full list of options"
    assert got["ticked"]["toggle"] == "Options · Evidence" and "signed record" in got["ticked"]["summary"]
    assert "No signing key loaded" in got["ticked"]["key"] and "unsigned" in got["ticked"]["key"]

    assert "Not signed" in got["unsigned"]["block"] and "never share it with the image" in got["unsigned"]["block"]
    assert "1 record ready" in got["unsigned"]["held"]

    assert got["keyMade"]["suggested"] == "imgstrip-record-key.pem" and got["keyMade"]["encrypted"] is True
    assert "Ed25519" in got["keyMade"]["card"] and got["keyMade"]["passField"] == "" and got["keyMade"]["formHidden"] is not None
    assert "old options" in got["stale"], "a new key makes the earlier clean stale"

    assert "Signed with key" in got["signed"]["block"] and "Not signed" not in got["signed"]["block"]
    assert "1 record ready" in got["signed"]["held"]
    assert got["signed"]["buttonVisible"] and got["signed"]["button"] == "Record", "the record has its own button in the action bar"

    assert got["files"]["recName"].endswith(record.SUFFIX) and got["files"]["imgName"].endswith(".jpg")
    assert got["files"]["imgHasCase"] is False

    assert got["dialogOpen"] is True and got["dialogFocus"] == "rec-file"
    assert "Trusted" in got["checkOk"] and "has not been edited" in got["checkOk"] and "CASE-2026-114" in got["checkOk"] and "Det. Example" in got["checkOk"]
    assert "No image was given" in got["checkOk"]
    assert "cleaned file this record describes" in got["checkClean"] and "No image was given" not in got["checkClean"]
    assert "original file this record describes" in got["checkOriginal"]
    assert "does not match the record" in got["checkWrongImage"] and "describes" not in got["checkWrongImage"]
    assert got["dialogClosed"] == {"open": False, "file": 0, "verdict": 0}
    assert "cannot be trusted" in got["checkBad"] and "changed after it was signed" in got["checkBad"]
    assert "Choose a record file first" in got["checkNone"]

    assert "No signing key loaded" in got["removed"]
    assert "wrong" in got["wrong"]["error"].lower() and got["wrong"]["passField"] == "" and got["wrong"]["focus"] == "rec-pass"
    assert "Ed25519" in got["loaded"]["card"] and got["loaded"]["passField"] == ""

    # nothing private reaches the browser's storage: not the passphrases, the key, the case or the operator
    for secret in ("WRONG-PASSPHRASE-CANARY", "correct horse battery", "PRIVATE KEY", "CASE-2026-114", "Det. Example", "record_case", "record_operator"):
        assert secret not in got["storage"] and secret not in got["storageAfter"], secret
    assert 'record\\":true' not in got["storageAfter"]

    assert got["reloaded"] == {"preset": "sharing", "checked": False, "panelOpen": False, "caseValue": "", "operatorValue": ""}
    assert got["arrow"] == {"preset": "evidence", "focus": "preset:evidence"}
    assert got["custom"] == {"customOpen": True, "panelOpen": True, "checked": True}, "Evidence then Custom keeps the record on"
    assert "Ed25519" in got["afterReload"], "the server still holds the key"
    assert got["keyboard"] is False, "the space bar toggles the switch"
    assert "never share it with the picture" in got["info"]
    # the only complaint is the browser noting the 422 that the deliberately wrong passphrase earned
    assert [e for e in got["errors"] if "status of 422" not in e] == [] and len(got["errors"]) <= 1
