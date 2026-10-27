"""A real browser drives the Content Credentials UI (skipped when Node, Playwright or Chromium are missing).

It checks what static tests cannot: that the toggle reveals the panel, that the passphrase and the signing
toggle never reach browser storage, that signing needs an identity, and that a signed file's credential is shown.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import textwrap
import threading
from pathlib import Path

import pytest

from c2pa_support import photo_bytes
from imgstrip.web import server as web
from test_web import Client


def _find_playwright() -> str | None:
    node = shutil.which("node")
    if not node:
        return None
    try:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return str(Path(root) / "playwright") if root and (Path(root) / "playwright").is_dir() else None


def _find_chromium() -> str | None:
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    for pattern in ("chromium", "chromium-*/chrome-linux/chrome", "chromium-*/chrome-linux64/chrome"):
        for hit in sorted(glob.glob(os.path.join(base, pattern))):
            if os.path.isfile(hit) and os.access(hit, os.X_OK):
                return hit
            inner = os.path.join(hit, "chrome")
            if os.path.isfile(inner) and os.access(inner, os.X_OK):
                return inner
    return None


PLAYWRIGHT, CHROMIUM = _find_playwright(), _find_chromium()
pytestmark = [
    pytest.mark.usefixtures("creds"),
    pytest.mark.skipif(not (PLAYWRIGHT and CHROMIUM), reason="needs node, playwright and chromium"),
]


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
      const [url, lockedChain, lockedKey, photo] = process.argv.slice(2);
      const browser = await chromium.launch({ executablePath: process.env.CHROMIUM, args: ['--no-sandbox'] });
      // wide enough for the two-pane layout, where the detail is a pane and not a sheet over the page
      const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
      const errors = [];
      page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
      page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
      const open = async (selector) => (await page.getAttribute(selector, 'data-open')) === 'true';
      const stored = () => page.evaluate(() => JSON.stringify([localStorage, sessionStorage].map((s) => Object.fromEntries(Object.entries(s)))));

      await page.goto(url);
      await page.waitForSelector('#optionsToggle');
      await page.click('#optionsToggle');
      out.initial = { checked: await page.isChecked('#c2paSwitch input'), panelOpen: await open('#credentials') };

      await page.click('#c2paSwitch');
      await page.waitForTimeout(300);
      out.ticked = { checked: await page.isChecked('#c2paSwitch input'), panelOpen: await open('#credentials'), text: (await page.textContent('#credIdentity')).trim() };

      // Clean without an identity: refused with a note, nothing sent
      await page.setInputFiles('#picker', photo);
      await page.waitForSelector('.file[data-state="scanned"]');
      await page.click('#primary');
      await page.waitForSelector('#footNote:not([hidden])');
      out.noIdentity = { note: (await page.textContent('#footNote')).trim(), state: await page.getAttribute('.file', 'data-state') };

      // A protected identity: wrong passphrase, then the right one
      await page.click('[data-key="load"]');
      await page.setInputFiles('#cred-cert', lockedChain);
      await page.setInputFiles('#cred-key', lockedKey);
      await page.fill('#cred-pass', 'WRONG-PASSPHRASE-CANARY');
      await page.click('#identityLoad');
      await page.waitForSelector('#identityError:not([hidden])');
      out.wrong = { error: (await page.textContent('#identityError')).trim(), passField: await page.inputValue('#cred-pass'), focus: await page.evaluate(() => document.activeElement.id) };
      await page.fill('#cred-pass', 'open sesame please');
      await page.click('#identityLoad');
      await page.waitForSelector('.cred-card');
      out.loaded = { name: (await page.textContent('.cred-name')).trim(), passField: await page.inputValue('#cred-pass'), formHidden: await page.getAttribute('#identityForm', 'hidden'), notes: (await page.textContent('.cred-notes')).replace(/\s+/g, ' ') };
      out.storage = await stored();

      // Sign the file
      await page.click('#primary');
      await page.waitForSelector('.file[data-state="clean"]', { timeout: 30000 });
      await page.click('.file-head');
      await page.waitForSelector('.credential');
      out.signed = { sub: (await page.textContent('.file-meta')).trim(), block: (await page.textContent('.credential')).replace(/\s+/g, ' ') };

      // Changing the identity or the settings marks the result as made with old options
      await page.selectOption('#cred-date', 'minute');
      out.stale = (await page.textContent('.file-side')).replace(/\s+/g, ' ');

      // Reload: the toggle is off again, the server still holds the identity
      await page.reload();
      await page.waitForSelector('#optionsToggle');
      await page.click('#optionsToggle');
      out.reloaded = { checked: await page.isChecked('#c2paSwitch input'), panelOpen: await open('#credentials') };
      await page.click('#c2paSwitch');
      await page.waitForSelector('.cred-card');
      out.afterReload = (await page.textContent('.cred-name')).trim();
      out.storageAfter = await stored();

      // Remove it
      await page.click('#credentials [data-key="remove"]');
      await page.waitForSelector('[data-key="load"]');
      out.removed = (await page.textContent('#credIdentity')).trim();

      // A test identity, with its warning
      await page.click('[data-key="test"]');
      await page.waitForSelector('.cred-card');
      out.testIdentity = (await page.textContent('.cred-card')).replace(/\s+/g, ' ');
      out.testRoot = await page.locator('text=Download test root').count();

      // Keyboard: the switch can be reached and toggled with the space bar
      await page.focus('#c2paSwitch input');
      await page.keyboard.press('Space');
      out.keyboard = await page.isChecked('#c2paSwitch input');
      out.errors = errors;
      console.log(JSON.stringify(out));
      await browser.close();
    })().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
    """
)


def test_the_credentials_ui_end_to_end(app, creds, tmp_path):
    t = creds.make_test_identity("Ink & Paper", "Editor One", passphrase=b"open sesame please")
    (tmp_path / "chain.pem").write_bytes(t.chain_pem + t.root_pem)
    (tmp_path / "key.pem").write_bytes(t.key_pem)
    (tmp_path / "photo.png").write_bytes(photo_bytes("PNG"))
    script = tmp_path / "ui.js"
    script.write_text(SCRIPT)
    url = web.launch_url(type("H", (), {"server_address": ("127.0.0.1", app.port)})(), app.session.launch_key)
    env = {**os.environ, "PLAYWRIGHT_MODULE": PLAYWRIGHT, "CHROMIUM": CHROMIUM}
    proc = subprocess.run(
        ["node", str(script), url, str(tmp_path / "chain.pem"), str(tmp_path / "key.pem"), str(tmp_path / "photo.png")],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])

    assert got["initial"] == {"checked": False, "panelOpen": False}, "off by default, panel hidden"
    assert (
        got["ticked"]["checked"]
        and got["ticked"]["panelOpen"]
        and "No signing identity loaded" in got["ticked"]["text"]
    )
    assert "Load a signing identity first" in got["noIdentity"]["note"] and got["noIdentity"]["state"] == "scanned"
    assert "wrong" in got["wrong"]["error"] and got["wrong"]["passField"] == "" and got["wrong"]["focus"] == "cred-pass"
    assert (
        got["loaded"]["name"] == "Ink & Paper"
        and got["loaded"]["passField"] == ""
        and "test/development certificate" in got["loaded"]["notes"]
    )
    # (the storage dump is JSON inside JSON, so the quotes of the saved options appear escaped)
    for secret in ("WRONG-PASSPHRASE-CANARY", "open sesame please", "PRIVATE KEY", 'c2pa\\":', "c2pa_commit"):
        assert secret not in got["storage"] and secret not in got["storageAfter"], secret
    assert "signed" in got["signed"]["sub"]
    assert (
        "Signed by Ink & Paper" in got["signed"]["block"]
        and "Public viewers will see: Unverified" in got["signed"]["block"]
    )
    assert "vouches for what you did to the file" in got["signed"]["block"]
    assert "old options" in got["stale"]
    assert got["reloaded"] == {"checked": False, "panelOpen": False}, (
        "signing is never silently on after a restart of the page"
    )
    assert got["afterReload"] == "Ink & Paper", "the identity stays in the server's memory, not the page's"
    assert "No signing identity loaded" in got["removed"]
    assert "test/development certificate" in got["testIdentity"] and got["testRoot"] == 1
    assert got["keyboard"] is False, "space toggled the switch off again"
    # the only console noise is the browser's own line for the deliberate wrong-passphrase attempt (a 422)
    assert [e for e in got["errors"] if "status of 422" not in e] == [], got["errors"]
    assert len(got["errors"]) == 1
