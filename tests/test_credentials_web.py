"""The web app's Content Credentials: identity in memory only, signing per file, receipts, and the page itself."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import re
import threading
from html.parser import HTMLParser
from pathlib import Path

import pytest

from c2pa_support import Pki, foreign_credential, key_der, key_pem, pem_chain, photo_bytes, self_signed
from imgstrip import credentials, fixtures
from imgstrip.web import server as web
from test_web import Client

pytestmark = pytest.mark.usefixtures("creds")

STATIC = Path(web.__file__).parent / "static"


@pytest.fixture()
def app(tmp_path):
    session = web.Session(str(tmp_path / "work"))
    httpd, session = web.make_server(0, session)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield Client(httpd.server_address[1], session)
    httpd.shutdown()
    httpd.server_close()
    session.close()


def b64(data: bytes | None) -> str | None:
    return None if data is None else base64.b64encode(data).decode()


def identity_payload(signer, **kw):
    return {"cert": b64(signer.chain_pem), "key": b64(signer.key_pem), **kw}


@pytest.fixture
def recorded(monkeypatch):
    """Every response body the app sends during a test, to prove what never appears in one."""
    bodies: list[bytes] = []
    original = Client.request

    def request(self, *a, **kw):
        res, data = original(self, *a, **kw)
        bodies.append(data)
        return res, data

    monkeypatch.setattr(Client, "request", request)
    return bodies


def load(app, signer, **kw):
    status, res = app.json("POST", "/api/identity", identity_payload(signer, **kw))
    assert status == 200, res
    return res["identity"]


# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------


def test_info_reports_what_is_available_and_no_identity_at_first(app):
    status, info = app.json("GET", "/api/info")
    assert status == 200
    assert info["credentials"]["available"] is True and "JPEG" in info["credentials"]["formats"]
    assert "TIFF" not in info["credentials"]["formats"] and "BMP" not in info["credentials"]["formats"]
    assert info["identity"] is None and info["receipts"] == 0


def test_without_the_c2pa_support_everything_says_how_to_install_it(app, monkeypatch):
    monkeypatch.setattr(credentials, "available", lambda: False)
    status, info = app.json("GET", "/api/info")
    assert info["credentials"]["available"] is False and "pip install 'imgstrip[c2pa]'" in info["credentials"]["hint"]
    status, res = app.json("POST", "/api/identity", {"cert": "AAAA"})
    assert status == 501 and "imgstrip[c2pa]" in res["error"]
    status, res = app.json("POST", "/api/identity/test", {})
    assert status == 501


# --------------------------------------------------------------------------
# The identity: memory only, never shown again
# --------------------------------------------------------------------------


def test_an_identity_loads_is_remembered_by_the_server_and_can_be_forgotten(app, signer):
    identity = load(app, signer)
    assert identity["signer"] == "Test Newsroom" and identity["can_sign"] and identity["test"]
    assert {n["code"] for n in identity["notices"]} >= {"test_identity", "no_timestamp"}
    _, info = app.json("GET", "/api/info")
    assert info["identity"]["fingerprint"] == identity["fingerprint"], "a page reload finds it again"
    assert app.session.signing_state()[0] is not None
    status, res = app.json("DELETE", "/api/identity")
    assert status == 200 and res == {"ok": True}
    _, info = app.json("GET", "/api/info")
    assert info["identity"] is None and app.session.signing_state() == (None, None, None)


def test_trust_anchors_are_remembered_with_the_identity(app, signer):
    identity = load(app, signer, anchors=b64(signer.root_pem))
    assert identity["trust"] == "trusted" and identity["state"] == "Trusted"
    assert app.session.signing_state()[2] == credentials.load_anchors(signer.root_pem)


def test_the_key_never_appears_in_any_response_and_never_touches_the_disk(app, signer, recorded, tmp_path):
    secret = key_der(signer.identity)
    pem_text = signer.key_pem
    load(app, signer)
    app.clean(photo_bytes("PNG"), "photo.png", {"credentials": {"sign": True, "commit": True}})
    app.json("POST", "/api/receipts", {})
    app.json("GET", "/api/info")
    assert recorded
    for body in recorded:
        for forbidden in (secret, pem_text, b"PRIVATE KEY", secret.hex().encode(), base64.b64encode(secret)):
            assert forbidden not in body
    # and nothing on disk: the session folder and everything beside it
    for path in tmp_path.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert secret not in data and b"PRIVATE KEY" not in data, path


def test_a_passphrase_is_used_once_and_never_echoed(app, creds, recorded, tmp_path):
    locked = creds.make_test_identity("Locked", passphrase=b"open sesame please")
    payload = {"cert": b64(locked.chain_pem + locked.root_pem), "key": b64(locked.key_pem)}
    status, res = app.json("POST", "/api/identity", payload)
    assert status == 422 and res["code"] == "passphrase" and "encrypted" in res["error"]
    status, res = app.json("POST", "/api/identity", {**payload, "passphrase": "wrong guess"})
    assert (
        status == 422 and res["code"] == "passphrase" and "wrong" in res["error"] and "wrong guess" not in res["error"]
    )
    status, res = app.json("POST", "/api/identity", {**payload, "passphrase": "open sesame please"})
    assert status == 200 and res["identity"]["signer"] == "Locked"
    for body in recorded:
        assert b"open sesame please" not in body and b"wrong guess" not in body
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"open sesame please" not in path.read_bytes()


@pytest.mark.parametrize(
    "payload, status, text",
    [
        ({}, 400, "certificate is missing"),
        ({"cert": "!!not base64!!"}, 400, "could not be decoded"),
        ({"cert": 12}, 400, "base64"),
        ({"cert": b64(b"garbage")}, 422, ""),
        ({"cert": b64(b"x" * (300 << 10))}, 413, "too large"),
        ({"cert": "AAAA", "passphrase": 5}, 400, "passphrase must be text"),
        ({"cert": "AAAA", "key": b64(b"x" * (300 << 10))}, 413, "too large"),
    ],
)
def test_identity_requests_are_validated(app, payload, status, text):
    got, res = app.json("POST", "/api/identity", payload)
    assert got == status and text in res["error"]
    assert app.session.signing_state()[0] is None


def test_unusable_certificates_are_refused_in_plain_words_and_not_kept(app):
    own = self_signed()
    status, res = app.json("POST", "/api/identity", {"cert": b64(pem_chain(own) + key_pem(own))})
    assert status == 422 and "self-signed" in res["error"]
    expired = Pki().issue(
        not_before=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=9),
        not_after=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1),
    )
    status, res = app.json("POST", "/api/identity", {"cert": b64(pem_chain(expired) + key_pem(expired))})
    assert status == 422 and "expired" in res["error"]
    assert app.session.signing_state()[0] is None


def test_a_library_failure_never_echoes_its_message(app, signer, monkeypatch):
    def explode(*a, **kw):
        raise RuntimeError("leaked: " + key_der(signer.identity).hex())

    monkeypatch.setattr(credentials, "load_identity", explode)
    status, res = app.json("POST", "/api/identity", identity_payload(signer))
    assert status == 422 and "leaked" not in res["error"] and "could not be read" in res["error"]


def test_identity_endpoints_sit_behind_the_same_gates_as_everything_else(app, signer):
    body = json.dumps(identity_payload(signer)).encode()
    res, data = app.request("POST", "/api/identity", body, {"Content-Type": "application/json"}, auth=False)
    assert res.status == 403
    res, data = app.request(
        "POST", "/api/identity", body, {"Content-Type": "application/json", "Origin": "http://evil.example"}
    )
    assert res.status == 403
    res, data = app.request("POST", "/api/identity", body, {"Content-Type": "application/json"}, host="evil.example")
    assert res.status == 421
    res, data = app.request("DELETE", "/api/identity", None, {}, auth=False)
    assert res.status == 403
    res, data = app.request("POST", "/api/receipts", b"{}", {}, auth=False)
    assert res.status == 403
    assert app.session.signing_state()[0] is None


def test_the_test_identity_is_made_in_memory_and_flagged(app):
    status, res = app.json("POST", "/api/identity/test", {"organization": "Night Desk"})
    assert status == 200 and res["identity"]["signer"] == "Night Desk" and res["identity"]["test"] is True
    assert res["root_pem"].startswith("-----BEGIN CERTIFICATE-----") and "PRIVATE" not in json.dumps(res)
    assert any(n["code"] == "test_identity" for n in res["identity"]["notices"])
    status, res = app.json("POST", "/api/identity/test", {"organization": "x" * 65})
    assert status == 400
    status, res = app.json("POST", "/api/identity/test", {"trust_test_ca": True})
    assert res["identity"]["state"] == "Trusted"


# --------------------------------------------------------------------------
# Cleaning with credentials
# --------------------------------------------------------------------------


def clean_signed(app, data, name="photo.png", **cred):
    return app.clean(data, name, {"credentials": {"sign": True, **cred}})


def test_signing_needs_an_identity_first(app):
    res, body = app.upload("/api/clean", photo_bytes("PNG"), "photo.png", {"credentials": {"sign": True}})
    assert res.status == 409 and json.loads(body)["code"] == "identity"
    assert not app.session._items, "nothing is stored when signing was asked for and cannot happen"


def test_a_signed_result_carries_the_credential_and_downloads_intact(app, signer, creds):
    load(app, signer)
    r = clean_signed(app, photo_bytes("PNG"))
    c = r["credentials"]
    assert (
        c["signed"]
        and c["signer"] == "Test Newsroom"
        and c["pixel_integrity"] == "identical"
        and c["detail"] == "minimal"
    )
    assert c["label"].startswith("Unverified") and c["committed"] is False and c["state"] == "Valid"
    assert any("test/development certificate" in w for w in c["warnings"])
    res, data = app.request("GET", f"/api/result/{r['id']}")
    assert res.status == 200 and hashlib.sha256(data).hexdigest() == r["sha256"] and len(data) == r["bytes_out"]
    cred = creds.read_credential(data)
    assert cred.imgstrip and cred.pixels_only
    assert r["filename"] == "photo.clean.png" and r["pixels_exact"] is True and r["lossy"] is False


def test_without_the_tick_box_results_are_exactly_what_they_were(app, signer, creds):
    load(app, signer)
    for options in ({}, {"credentials": {"sign": False}}, {"credentials": None}):
        r = app.clean(photo_bytes("PNG"), "photo.png", options)
        assert r["credentials"] is None
        _, data = app.request("GET", f"/api/result/{r['id']}")
        assert not creds.read_credential(data).present


def test_pixel_claims_follow_the_options(app, signer):
    load(app, signer)
    jpg = photo_bytes("JPEG", quality=92)
    r = app.clean(jpg, "p.jpg", {"credentials": {"sign": True, "detail": "full"}})
    assert r["credentials"]["pixel_integrity"] == "equivalent" and r["credentials"]["operations"] == ["re-encoded"]
    assert r["credentials"]["detail"] == "full" and r["credentials"]["psnr_db"] > 30
    r = app.clean(jpg, "p.jpg", {"jpeg_lossless": True, "credentials": {"sign": True}})
    assert r["credentials"]["pixel_integrity"] == "identical"


def test_date_precision_and_commit_reach_the_credential(app, signer, creds):
    load(app, signer)
    r = clean_signed(app, photo_bytes("PNG"), date="minute", commit=True)
    _, data = app.request("GET", f"/api/result/{r['id']}")
    s = creds.read_credential(data).sanitization
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z", s["date"]) and "source_commitment" in s
    assert r["credentials"]["committed"] is True


@pytest.mark.parametrize(
    "cred",
    [
        {"sign": "yes"},
        {"sign": True, "date": "second"},
        {"sign": True, "detail": "all"},
        {"sign": True, "commit": "no"},
        "on",
    ],
)
def test_bad_credential_options_are_refused(app, signer, cred):
    load(app, signer)
    res, body = app.upload("/api/clean", photo_bytes("PNG"), "p.png", {"credentials": cred})
    assert res.status == 400 and not app.session._items


@pytest.mark.parametrize("fmt", ["BMP", "TIFF"])
def test_formats_that_cannot_carry_a_credential_are_refused_per_file(app, signer, fmt):
    load(app, signer)
    res, body = app.upload("/api/clean", photo_bytes("PNG"), "p.png", {"format": fmt, "credentials": {"sign": True}})
    assert res.status == 422 and f"{fmt} files cannot carry Content Credentials" in json.loads(body)["error"]
    assert not app.session._items


def test_the_original_credentials_are_reported_and_removed(app, signer):
    load(app, signer)
    src = foreign_credential(Pki("Camera Corp").issue(org="Camera Corp"), photo_bytes("PNG"), "PNG", ai=True)
    r = clean_signed(app, src)
    text = " ".join(r["warnings"])
    assert "carried Content Credentials (signed by Camera Corp)" in text and "declared the image AI-generated" in text
    _, data = app.request("GET", f"/api/result/{r['id']}")
    assert b"Camera Corp" not in data


def test_removing_the_identity_stops_signing_immediately(app, signer):
    load(app, signer)
    clean_signed(app, photo_bytes("PNG"))
    app.json("DELETE", "/api/identity")
    res, _ = app.upload("/api/clean", photo_bytes("PNG"), "p.png", {"credentials": {"sign": True}})
    assert res.status == 409


def test_signing_works_for_many_files_at_once(app, signer, creds):
    load(app, signer)
    results, errors = [], []

    def work(i):
        try:
            data = photo_bytes("PNG")
            r = clean_signed(app, data, f"p{i}.png")
            _, out = app.request("GET", f"/api/result/{r['id']}")
            results.append(creds.read_credential(out).imgstrip)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=work, args=(i,)) for i in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and results == [True] * 5


def test_scanning_a_signed_file_shows_its_credential(app, signer, creds):
    load(app, signer)
    r = clean_signed(app, photo_bytes("PNG"))
    _, data = app.request("GET", f"/api/result/{r['id']}")
    res, body = app.upload("/api/inspect", data, "signed.png")
    scan = json.loads(body)
    assert scan["clean"] is False, "a credential is still data beside the picture"
    assert scan["credential"]["imgstrip"] and scan["credential"]["signer"] == "Test Newsroom"
    res, body = app.upload("/api/inspect", photo_bytes("PNG"), "plain.png")
    assert json.loads(body)["credential"] is None


def test_a_credential_that_cannot_be_read_does_not_break_scanning(app, signer, monkeypatch):
    load(app, signer)
    r = clean_signed(app, photo_bytes("PNG"))
    _, data = app.request("GET", f"/api/result/{r['id']}")
    monkeypatch.setattr(credentials, "read_credential", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    res, body = app.upload("/api/inspect", data, "signed.png")
    assert res.status == 200 and json.loads(body)["credential"] is None


# --------------------------------------------------------------------------
# Receipts
# --------------------------------------------------------------------------


def test_receipts_are_downloaded_by_request_and_open_the_commitment(app, signer, creds):
    load(app, signer)
    source = photo_bytes("PNG")
    res, body = app.request("POST", "/api/receipts", b"{}", {})
    assert res.status == 404, "nothing to download before anything was committed"
    r = clean_signed(app, source, commit=True)
    _, info = app.json("GET", "/api/info")
    assert info["receipts"] == 1
    res, body = app.request("POST", "/api/receipts", b"{}", {})
    assert res.status == 200 and res.getheader("Content-Type") == "application/json"
    doc = json.loads(body)
    assert doc["imgstrip_receipts"] == 1 and len(doc["receipts"]) == 1 and "PRIVATE" in doc["warning"]
    _, signed = app.request("GET", f"/api/result/{r['id']}")
    assert creds.audit(signed, source, doc).ok


def test_receipts_survive_clearing_the_list_but_not_the_server(app, signer):
    load(app, signer)
    clean_signed(app, photo_bytes("PNG"), commit=True)
    app.json("POST", "/api/reset", {})
    assert app.session.receipts() and app.session.signing_state()[0] is not None
    app.session.close()
    assert app.session.receipts() == [] and app.session.signing_state()[0] is None


def test_no_receipt_without_a_commitment(app, signer):
    load(app, signer)
    clean_signed(app, photo_bytes("PNG"))
    assert app.session.receipts() == []


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------


class _Tree(HTMLParser):
    """Which elements sit inside which <form> / region of index.html."""

    def __init__(self):
        super().__init__()
        self.stack: list[tuple[str, dict]] = []
        self.found: list[tuple[str, dict, list[tuple[str, dict]]]] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.found.append((tag, a, list(self.stack)))
        if tag not in ("input", "br", "img", "link", "meta", "path", "circle", "use", "rect"):
            self.stack.append((tag, a))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


@pytest.fixture(scope="module")
def page():
    tree = _Tree()
    tree.feed((STATIC / "index.html").read_text())
    return tree


def inside(parents, **match):
    return any(
        all(a.get(k) == v or (k == "class" and v in (a.get("class") or "").split()) for k, v in match.items())
        for _t, a in parents
    )


def test_the_tick_box_is_a_switch_in_the_options_tray_and_off_by_default(page):
    (box,) = [(a, p) for t, a, p in page.found if t == "input" and a.get("name") == "c2pa"]
    attrs, parents = box
    assert attrs.get("type") == "checkbox" and attrs.get("role") == "switch" and "checked" not in attrs
    assert inside(parents, id="optionsForm"), "it is one of the options, like the others"
    assert attrs.get("aria-controls") == "credentials"


def test_nothing_secret_can_be_reached_by_the_options_form_or_its_storage(page):
    """The options form saves every named field to localStorage, so credentials inputs must live outside it, unnamed."""
    ids = {"cred-cert", "cred-key", "cred-pass", "cred-anchors"}
    secret_inputs = [(a, p) for t, a, p in page.found if t == "input" and a.get("id") in ids]
    assert {a["id"] for a, _ in secret_inputs} == ids
    for attrs, parents in secret_inputs:
        assert not inside(parents, id="optionsForm"), attrs["id"]
        assert "name" not in attrs, "unnamed fields are never serialised by form handlers"
    (pw,) = [a for a, _ in secret_inputs if a["id"] == "cred-pass"]
    assert pw["type"] == "password" and pw.get("autocomplete") == "off" and pw.get("spellcheck") == "false"
    forms = [a for t, a, _p in page.found if t == "form"]
    assert any(f.get("id") == "identityForm" and f.get("autocomplete") == "off" for f in forms)


def test_the_page_never_stores_the_signing_toggle_or_secrets(page):
    js = (STATIC / "app.js").read_text()
    assert "const VOLATILE = ['c2pa', 'c2pa_commit']" in js
    assert "JSON.stringify(state.options)" not in js, (
        "options must be saved through persistable(), which drops volatile ones"
    )
    assert js.count("persistable()") >= 2
    assert not re.search(
        r"(local|session)\.set\([^)]*(pass|cert|key)\b",
        js.replace("TOKEN_KEY", "").replace("FOLDER_KEY", "").replace("OPTIONS_KEY", ""),
    )
    assert "els.credPass.value = ''" in js, "the passphrase field is wiped as soon as the request has been made"
    assert "passphrase: els.credPass.value || null" in js


def test_the_page_stays_offline_and_inline_script_free(page):
    for name in ("index.html", "app.js", "app.css"):
        text = (STATIC / name).read_text()
        urls = re.findall(r"https?://[^\s\"')]+", text)
        assert all(u.startswith(("http://www.w3.org", "http://127.0.0.1")) for u in urls), (name, urls)
    html = (STATIC / "index.html").read_text()
    assert "onclick=" not in html and "<script>" not in html


def test_the_limits_section_tells_the_truth_about_credentials():
    html = (STATIC / "index.html").read_text()
    assert "Content Credentials vouch for processing only" in html
    for phrase in ("no trusted timestamp", "invalid once your certificate expires", "Unverified", "C2PA Trust List"):
        assert phrase in html


def test_the_credentials_panel_is_labelled_and_the_controls_have_labels(page):
    ids = {a.get("id") for t, a, _p in page.found if a.get("id")}
    labels = {a.get("for") for t, a, _p in page.found if t == "label" and a.get("for")}
    for control in ("cred-cert", "cred-key", "cred-pass", "cred-anchors", "cred-date", "cred-detail"):
        assert control in ids and control in labels, control
    (section,) = [a for t, a, _p in page.found if a.get("id") == "credentials"]
    assert section.get("aria-labelledby") == "credTitle" and "credTitle" in ids


def test_the_static_files_are_served_with_the_new_markup(app):
    res, body = app.request("GET", "/", auth=False)
    assert res.status == 200 and b'name="c2pa"' in body and b"identityForm" in body
    res, js = app.request("GET", "/static/app.js", auth=False)
    assert res.status == 200 and b"submitIdentity" in js


def test_fixtures_are_unaffected_by_the_credentials_code_paths(app, fx):
    """The ordinary clean path (no credentials) still removes every canary, with the identity loaded or not."""
    r = app.clean(fx["jpeg_everything.jpg"], "x.jpg", {})
    _, data = app.request("GET", f"/api/result/{r['id']}")
    assert fixtures.CANARY not in data and r["credentials"] is None
