"""The web app's processing record: the key lives in memory only, one record per cleaned image, kept apart from it."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import threading
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from imgstrip import record
from imgstrip.web import server as web
from test_web import Client

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


def photo() -> bytes:
    exif = Image.Exif()
    exif[0x010F] = "Camera Maker"
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (9, 99, 199)).save(buf, "JPEG", exif=exif)
    return buf.getvalue()


b64 = lambda data: base64.b64encode(data).decode()  # noqa: E731
sha = lambda data: hashlib.sha256(data).hexdigest()  # noqa: E731
KEEP = {"record": {"keep": True, "case": "WEB-1", "operator": "Op One"}}


def new_key(app, passphrase=None):
    status, res = app.json("POST", "/api/record/key/new", {"passphrase": passphrase})
    assert status == 200, res
    return res


def download(app, rid):
    res, data = app.request("GET", f"/api/result/{rid}", auth=False)
    assert res.status == 200
    return data


# --------------------------------------------------------------------------
# The key
# --------------------------------------------------------------------------


def test_info_says_whether_records_can_be_signed_and_that_no_key_is_loaded_at_first(app):
    status, info = app.json("GET", "/api/info")
    assert status == 200 and info["record"]["available"] is True and info["record"]["key"] is None
    assert "imgstrip[record]" in info["record"]["hint"]


def test_a_new_key_is_returned_once_and_only_its_public_half_is_shown_again(app):
    res = new_key(app)
    assert "PRIVATE KEY" in res["private_pem"] and res["protected"] is False
    assert res["key"]["key_id"] and "PUBLIC KEY" in res["key"]["public_pem"] and "PRIVATE" not in json.dumps(res["key"])
    status, info = app.json("GET", "/api/info")
    assert info["record"]["key"]["key_id"] == res["key"]["key_id"]
    assert "PRIVATE" not in json.dumps(info)
    assert record.load_signer(res["private_pem"].encode()).key_id == res["key"]["key_id"]


def test_a_key_can_be_loaded_from_a_file_with_or_without_a_passphrase(app):
    private, _, key_id = record.generate_key(b"open sesame")
    status, res = app.json("POST", "/api/record/key", {"key": b64(private)})
    assert status == 422 and res["code"] == "passphrase" and app.session.record_signer() is None
    status, res = app.json("POST", "/api/record/key", {"key": b64(private), "passphrase": "nope"})
    assert status == 422 and res["code"] == "passphrase" and "wrong" in res["error"].lower()
    status, res = app.json("POST", "/api/record/key", {"key": b64(private), "passphrase": "open sesame"})
    assert status == 200 and res["key"]["key_id"] == key_id and app.session.record_signer().key_id == key_id
    assert "open sesame" not in json.dumps(res)


@pytest.mark.parametrize("payload", [{}, {"key": ""}, {"key": 5}, {"key": "!!"}, {"key": b64(b"junk")}, {"key": b64(b"x" * (300 << 10))}])
def test_bad_key_uploads_are_refused_politely(app, payload):
    status, res = app.json("POST", "/api/record/key", payload)
    assert status in (400, 413, 422) and res["error"] and app.session.record_signer() is None


def test_a_public_key_is_not_accepted_as_the_signing_key(app):
    _, public, _ = record.generate_key()
    status, res = app.json("POST", "/api/record/key", {"key": b64(public)})
    assert status == 422 and "private key" in res["error"]


def test_the_key_can_be_removed_and_is_forgotten_when_the_session_closes(app):
    new_key(app)
    status, res = app.json("DELETE", "/api/record/key")
    assert status == 200 and app.session.record_signer() is None
    new_key(app)
    app.session.close()
    assert app.session.record_signer() is None


def test_the_private_key_is_never_written_to_the_session_folder(app):
    new_key(app, "a passphrase")
    app.clean(photo(), options=KEEP)
    for path in Path(app.session.dir).rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert b"PRIVATE KEY" not in data and b"a passphrase" not in data


def test_without_the_cryptography_library_the_page_is_told_how_to_get_it_and_unsigned_records_still_work(app, monkeypatch):
    monkeypatch.setattr(record, "available", lambda: False)
    status, info = app.json("GET", "/api/info")
    assert info["record"]["available"] is False
    for path in ("/api/record/key", "/api/record/key/new", "/api/record/verify"):
        status, res = app.json("POST", path, {"key": "AAAA", "record": "AAAA"})
        assert status == 501 and "imgstrip[record]" in res["error"]
    result = app.clean(photo(), options=KEEP)
    assert result["record"]["signed"] is False


# --------------------------------------------------------------------------
# Cleaning with a record
# --------------------------------------------------------------------------


def test_no_record_is_made_unless_asked_for(app):
    new_key(app)
    result = app.clean(photo())
    assert result["record"] is None


def test_a_cleaned_image_comes_with_a_signed_record_that_matches_it(app):
    key = new_key(app)["key"]
    original = photo()
    result = app.clean(original, name="IMG_7.jpg", options=KEEP)
    info = result["record"]
    assert info["signed"] is True and info["key_id"] == key["key_id"] and info["short"] == key["short"]
    assert info["id"] != result["id"] and info["filename"].endswith(record.SUFFIX)
    cleaned = download(app, result["id"])
    document = download(app, info["id"])
    verdict = record.verify(document, record.load_public(key["public_pem"].encode()))
    assert verdict.status == record.TRUSTED
    body = verdict.body
    assert body["case"] == "WEB-1" and body["operator"] == "Op One"
    assert body["input"] == {"name": "IMG_7.jpg", "sha256": sha(original), "bytes": len(original), "format": "JPEG"}
    assert body["output"]["sha256"] == sha(cleaned) == result["after"]["sha256"] and body["output"]["name"] == result["filename"]
    assert body["input"]["sha256"] == result["before"]["sha256"]
    assert any(r["label"] == "EXIF" for r in body["result"]["removed"]) and body["result"]["verified_by"]
    assert info["filename"] == record.file_name(result["filename"], sha(cleaned))


def test_the_record_is_not_in_the_image_and_the_image_is_identical_without_it(app):
    new_key(app)
    data = photo()
    with_record = app.clean(data, options=KEEP)
    without = app.clean(data)
    assert download(app, with_record["id"]) == download(app, without["id"])
    assert b"WEB-1" not in download(app, with_record["id"]) and b"imgstrip-record" not in download(app, with_record["id"])


def test_without_a_key_the_record_is_unsigned_and_says_so(app):
    result = app.clean(photo(), options=KEEP)
    info = result["record"]
    assert info["signed"] is False and info["key_id"] is None and info["short"] is None
    assert record.verify(download(app, info["id"])).status == record.UNSIGNED


def test_the_record_follows_the_options_that_were_used(app):
    result = app.clean(photo(), options={**KEEP, "format": "png", "icc": "drop", "scrub_lsb": True})
    body = json.loads(download(app, result["record"]["id"]))["record"]
    assert body["options"]["format"] == "PNG" and body["options"]["icc"] == "drop" and body["options"]["scrub_lsb"] is True
    assert body["output"]["format"] == "PNG" and body["input"]["format"] == "JPEG"


def test_random_names_are_used_in_the_record_for_the_output_but_the_input_keeps_its_name(app):
    result = app.clean(photo(), name="IMG_8.jpg", options={**KEEP, "random_name": True})
    body = json.loads(download(app, result["record"]["id"]))["record"]
    assert body["input"]["name"] == "IMG_8.jpg" and body["output"]["name"] == result["filename"] != "IMG_8.jpg"


def test_a_key_loaded_after_the_clean_does_not_sign_the_earlier_record(app):
    result = app.clean(photo(), options=KEEP)
    new_key(app)
    assert record.verify(download(app, result["record"]["id"])).status == record.UNSIGNED


@pytest.mark.parametrize(
    "bad, message",
    [
        ({"keep": "yes"}, "keep"),
        ({"keep": True, "case": 5}, "case reference"),
        ({"keep": True, "operator": ["x"]}, "operator name"),
        ({"keep": True, "case": "x" * 201}, "longer than"),
        ({"keep": True, "case": "a\x00b"}, "control"),
        ("yes", "object"),
    ],
)
def test_bad_record_requests_are_refused_before_anything_is_cleaned(app, bad, message):
    res, body = app.upload("/api/clean", photo(), "a.jpg", {"record": bad})
    assert res.status == 400 and message in json.loads(body)["error"]
    assert app.session._items == {}  # nothing was cleaned or stored


def test_record_off_or_missing_means_none(app):
    for raw in (None, {"keep": False, "case": "ignored"}):
        res, body = app.upload("/api/clean", photo(), "a.jpg", {"record": raw})
        assert res.status == 200 and json.loads(body)["record"] is None


def test_case_and_operator_are_optional_and_tidied(app):
    result = app.clean(photo(), options={"record": {"keep": True, "case": "  A \n B  ", "operator": ""}})
    body = json.loads(download(app, result["record"]["id"]))["record"]
    assert body["case"] == "A B" and body["operator"] is None


def test_a_failed_record_leaves_no_cleaned_file_behind(app, monkeypatch):
    def boom(*a, **kw):
        raise record.RecordError("boom")

    monkeypatch.setattr(record, "seal", boom)
    res, body = app.upload("/api/clean", photo(), "a.jpg", KEEP)
    assert res.status == 422 and "record" in json.loads(body)["error"]
    assert app.session._items == {}  # the cleaned image whose record failed was taken back


def test_a_cleaned_image_that_cannot_be_cleaned_makes_no_record(app):
    res, body = app.upload("/api/clean", b"not an image", "a.jpg", KEEP)
    assert res.status == 422


def test_records_are_deleted_like_any_result_and_with_the_session(app):
    result = app.clean(photo(), options=KEEP)
    rid = result["record"]["id"]
    status, res = app.json("DELETE", f"/api/result/{rid}")
    assert status == 200 and res["ok"] is True
    res, _ = app.request("GET", f"/api/result/{rid}", auth=False)
    assert res.status == 404


# --------------------------------------------------------------------------
# Records are kept apart from the images
# --------------------------------------------------------------------------


def test_the_images_zip_holds_no_record_and_the_records_zip_holds_only_records(app):
    new_key(app)
    results = [app.clean(photo(), name=f"p{i}.jpg", options=KEEP) for i in range(3)]
    status, images = app.json("POST", "/api/zip", {"ids": [r["id"] for r in results]})
    status, records = app.json("POST", "/api/zip", {"ids": [r["record"]["id"] for r in results], "records": True})
    assert images["filename"] == "imgstrip-clean.zip" and records["filename"] == "imgstrip-records.zip"
    names_i = zipfile.ZipFile(io.BytesIO(download(app, images["id"]))).namelist()
    names_r = zipfile.ZipFile(io.BytesIO(download(app, records["id"]))).namelist()
    assert len(names_i) == 3 and not any(n.endswith(record.SUFFIX) for n in names_i)
    assert len(names_r) == 3 and all(n.endswith(record.SUFFIX) for n in names_r)
    # both ZIPs stay available: making the second did not discard the first
    assert download(app, images["id"]) and download(app, records["id"])


def test_a_second_records_zip_replaces_the_first_records_zip_only(app):
    r = app.clean(photo(), options=KEEP)
    _, first = app.json("POST", "/api/zip", {"ids": [r["record"]["id"]], "records": True})
    _, images = app.json("POST", "/api/zip", {"ids": [r["id"]]})
    _, second = app.json("POST", "/api/zip", {"ids": [r["record"]["id"]], "records": True})
    assert app.session.get(first["id"]) is None and app.session.get(second["id"]) and app.session.get(images["id"])


def test_record_zip_entries_carry_no_dates(app):
    r = app.clean(photo(), options=KEEP)
    _, z = app.json("POST", "/api/zip", {"ids": [r["record"]["id"]], "records": True})
    info = zipfile.ZipFile(io.BytesIO(download(app, z["id"]))).infolist()[0]
    assert info.date_time == (1980, 1, 1, 0, 0, 0)


# --------------------------------------------------------------------------
# Checking a record
# --------------------------------------------------------------------------


def verify(app, document, pubkey=None):
    payload = {"record": b64(document)}
    if pubkey is not None:
        payload["pubkey"] = b64(pubkey)
    status, res = app.json("POST", "/api/record/verify", payload)
    assert status == 200, res
    return res


def test_a_record_is_checked_against_the_key_loaded_here(app):
    new_key(app)
    document = download(app, app.clean(photo(), options=KEEP)["record"]["id"])
    res = verify(app, document)
    assert res["status"] == "trusted" and res["ok"] is True and res["trusted_by"] == "loaded" and res["case"] == "WEB-1"
    assert res["input"]["sha256"] and res["output"]["sha256"] and res["short"]


def test_a_record_is_only_signature_intact_when_there_is_no_key_to_compare_with(app):
    new_key(app)
    document = download(app, app.clean(photo(), options=KEEP)["record"]["id"])
    app.json("DELETE", "/api/record/key")
    res = verify(app, document)
    assert res["status"] == "signed" and res["trusted_by"] is None and "not compared" in res["problems"][0]


def test_a_given_public_key_wins_over_the_loaded_one(app):
    new_key(app)
    document = download(app, app.clean(photo(), options=KEEP)["record"]["id"])
    _, other_public, _ = record.generate_key()
    res = verify(app, document, other_public)
    assert res["status"] == "invalid" and res["trusted_by"] == "given" and "different key" in res["problems"][0]
    mine = app.session.record_signer().public_pem()
    assert verify(app, document, mine)["status"] == "trusted"


def test_an_edited_record_is_reported_invalid(app):
    new_key(app)
    document = download(app, app.clean(photo(), options=KEEP)["record"]["id"])
    res = verify(app, document.replace(b"WEB-1", b"WEB-2"))
    assert res["status"] == "invalid" and res["ok"] is False and "changed after it was signed" in res["problems"][0]


def test_an_unsigned_record_is_reported_unsigned(app):
    document = download(app, app.clean(photo(), options=KEEP)["record"]["id"])
    res = verify(app, document)
    assert res["status"] == "unsigned" and res["ok"] is False


@pytest.mark.parametrize("payload", [{}, {"record": ""}, {"record": 3}, {"record": "!!"}, {"record": b64(b"x" * (600 << 10))}])
def test_bad_verify_requests_are_refused(app, payload):
    status, res = app.json("POST", "/api/record/verify", payload)
    assert status in (400, 413) and res["error"]


def test_something_that_is_not_a_record_is_reported_not_crashed_on(app):
    res = verify(app, b"just some text")
    assert res["status"] == "invalid" and "not an imgstrip record" in res["problems"][0]


def test_a_private_key_given_as_the_public_key_is_refused(app):
    private, _, _ = record.generate_key()
    status, res = app.json("POST", "/api/record/verify", {"record": b64(b"{}"), "pubkey": b64(private)})
    assert status == 422 and "PRIVATE" in res["error"]


def test_an_image_fingerprint_is_matched_to_the_cleaned_or_the_original_file(app):
    new_key(app)
    original = photo()
    result = app.clean(original, options=KEEP)
    document = download(app, result["record"]["id"])
    cleaned = download(app, result["id"])
    payload = {"record": b64(document)}
    status, res = app.json("POST", "/api/record/verify", {**payload, "image_sha256": sha(cleaned)})
    assert status == 200 and res["status"] == "trusted" and res["ok"] is True and res["image"] == "cleaned"
    status, res = app.json("POST", "/api/record/verify", {**payload, "image_sha256": sha(original).upper()})
    assert status == 200 and res["ok"] is True and res["image"] == "original", "upper-case hex is accepted"
    status, res = app.json("POST", "/api/record/verify", payload)
    assert res["image"] is None and res["problems"] == [], "no image given: nothing said about one"


def test_an_image_that_is_not_the_one_in_the_record_is_a_problem_even_when_the_record_is_sound(app):
    new_key(app)
    document = download(app, app.clean(photo(), options=KEEP)["record"]["id"])
    status, res = app.json("POST", "/api/record/verify", {"record": b64(document), "image_sha256": sha(b"some other picture")})
    assert status == 200 and res["status"] == "trusted", "the signature is still good"
    assert res["ok"] is False and res["image"] is None
    assert any("does not match the record" in p for p in res["problems"])


def test_an_invalid_record_is_not_also_blamed_for_an_image_it_cannot_describe(app):
    status, res = app.json("POST", "/api/record/verify", {"record": b64(b"just some text"), "image_sha256": sha(b"x")})
    assert status == 200 and res["status"] == "invalid" and res["image"] is None
    assert not any("does not match" in p for p in res["problems"])


@pytest.mark.parametrize("bad", ["abc", "z" * 64, "0" * 63, "0" * 65, 7, ["0" * 64], {"a": 1}])
def test_a_malformed_image_fingerprint_is_refused(app, bad):
    status, res = app.json("POST", "/api/record/verify", {"record": b64(b"{}"), "image_sha256": bad})
    assert status == 400 and "64 hexadecimal digits" in res["error"]


def test_the_image_itself_is_never_sent_only_its_fingerprint():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    body = js[js.index("async function submitRecordCheck") : js.index("function openCheck")]
    assert "image_sha256" in body and "sha256Hex(image)" in body
    assert "fileToBase64(image" not in body and "arrayBuffer" not in body, "the file is hashed in sha256Hex, not uploaded"


# --------------------------------------------------------------------------
# The gates still apply
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method, path",
    [("POST", "/api/record/key"), ("POST", "/api/record/key/new"), ("DELETE", "/api/record/key"), ("POST", "/api/record/verify")],
)
def test_the_record_endpoints_need_the_token_and_the_page_origin(app, method, path):
    res, _ = app.request(method, path, b"{}", {"Content-Type": "application/json"}, auth=False)
    assert res.status == 403
    res, _ = app.request(method, path, b"{}", {"Content-Type": "application/json", "Origin": "http://evil.example"})
    assert res.status == 403


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------


def test_the_page_has_the_record_switch_the_panel_and_the_forms():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for needle in ('name="record"', 'id="record"', 'id="rec-case"', 'id="rec-operator"', 'id="recKeyForm"', 'id="recNewForm"',
                   'id="recCheckForm"', 'id="recDownload"', 'data-info="record"'):  # fmt: skip
        assert needle in html, needle
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert len(ids) == len(set(ids)), "duplicate ids"
    assert re.search(r'aria-controls="record"', html)


def test_the_tray_opens_on_three_presets_and_keeps_the_full_list_behind_custom():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    form = html[html.index('id="optionsForm"') : html.index("</form>", html.index('id="optionsForm"'))]
    radios = re.findall(r'<input type="radio" name="preset" value="(\w+)"', form)
    assert radios == ["sharing", "evidence", "custom"]
    custom = form[form.index('id="custom"') :]
    for name in ("format", "quality", "icc", "scrub_lsb", "scrub_prnu", "jpeg_lossless", "auto_orient", "optimize", "record", "c2pa"):
        assert f'name="{name}"' in custom, f"{name} lives under Custom"
    assert 'name="preset"' not in custom
    assert len(re.findall(r'role="switch"', form)) == 11, "the eleven option switches (Deep scan is in the action bar, the credentials one in its own panel)"


def test_checking_a_record_has_a_window_of_its_own_not_a_place_in_the_tray():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    dialog = html[html.index("<dialog") : html.index("</dialog>")]
    for needle in ('id="recCheckForm"', 'id="rec-file"', 'id="rec-image"', 'id="rec-pub"', 'id="recVerdict"', 'id="checkClose"'):
        assert needle in dialog, needle
    options = html[html.index('id="options"') : html.index('id="body"')]
    assert "recCheckForm" not in options and "rec-file" not in options, "the tray no longer holds the checker"
    assert 'id="checkOpen"' in html[: html.index('id="options"')], "the button is in the header"
    assert re.search(r'id="recDownload"[^>]*hidden', html), "the records button sits in the action bar, hidden until there are records"
    assert html.index('id="recDownload"') > html.index('id="foot"')


def test_every_label_in_the_record_panel_points_at_a_field():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    for target in re.findall(r'<label[^>]* for="([^"]+)"', html):
        assert target in ids, target


def test_the_script_knows_the_record_option_the_label_and_the_explanation():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "record: 'Keep a processing record'" in js and re.search(r"\n\s+record: 'Writes a small separate file", js)
    volatile = re.search(r"const VOLATILE = \[([^\]]*)\]", js).group(1)
    for name in ("record", "record_case", "record_operator"):
        assert f"'{name}'" in volatile, "a case reference and an operator belong to one piece of work: never stored"


def test_the_record_text_fields_are_never_put_in_browser_storage_by_the_script():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "persistable = () =>" in js and "!VOLATILE.includes(k)" in js


def test_with_a_content_credential_the_record_describes_the_signed_file(app, signer):
    status, _ = app.json("POST", "/api/identity", {"cert": b64(signer.chain_pem), "key": b64(signer.key_pem)})
    assert status == 200
    result = app.clean(photo(), options={**KEEP, "credentials": {"sign": True}})
    signed = download(app, result["id"])
    body = json.loads(download(app, result["record"]["id"]))["record"]
    assert body["output"]["sha256"] == sha(signed) == result["after"]["sha256"]
    assert body["result"]["content_credentials"] is True


# --------------------------------------------------------------------------
# Loading the key at start-up, so it never passes through the browser
# --------------------------------------------------------------------------


def _startup(tmp_path, monkeypatch, *, passphrase=None, env=None, file_pass=None):
    import argparse

    private, _, key_id = record.generate_key(passphrase.encode() if passphrase else None)
    key = tmp_path / "k.pem"
    key.write_bytes(private)
    key.chmod(0o600)
    args = argparse.Namespace(record_key=str(key), record_passphrase_file=None)
    if file_pass is not None:
        pw = tmp_path / "pw"
        pw.write_text(file_pass + "\n")
        args.record_passphrase_file = str(pw)
    if env is not None:
        monkeypatch.setenv("IMGSTRIP_RECORD_PASSPHRASE", env)
    else:
        monkeypatch.delenv("IMGSTRIP_RECORD_PASSPHRASE", raising=False)
    return args, key_id


@pytest.mark.parametrize("how", ["none", "env", "file"])
def test_the_startup_key_is_loaded_into_the_session(app, tmp_path, monkeypatch, how):
    args, key_id = _startup(
        tmp_path, monkeypatch, passphrase=None if how == "none" else "pass phrase",
        env="pass phrase" if how == "env" else None, file_pass="pass phrase" if how == "file" else None,
    )  # fmt: skip
    import argparse

    web.load_startup_record_key(app.session, args, argparse.ArgumentParser())
    assert app.session.record_signer().key_id == key_id
    status, info = app.json("GET", "/api/info")
    assert info["record"]["key"]["key_id"] == key_id and "PRIVATE" not in json.dumps(info)


def test_a_wrong_or_missing_startup_passphrase_stops_the_app_with_a_clear_message(app, tmp_path, monkeypatch, capsys):
    import argparse

    args, _ = _startup(tmp_path, monkeypatch, passphrase="right", env="wrong")
    with pytest.raises(SystemExit) as exc:
        web.load_startup_record_key(app.session, args, argparse.ArgumentParser(prog="imgstrip-web"))
    assert exc.value.code == 2 and "wrong passphrase" in capsys.readouterr().err
    args, _ = _startup(tmp_path, monkeypatch, passphrase="right")
    with pytest.raises(SystemExit):
        web.load_startup_record_key(app.session, args, argparse.ArgumentParser(prog="imgstrip-web"))
    assert app.session.record_signer() is None


def test_the_web_command_line_refuses_a_passphrase_argument_and_a_passphrase_file_without_a_key(capsys):
    with pytest.raises(SystemExit) as exc:
        web.main(["--record-passphrase", "secret"])
    assert exc.value.code == 2 and "never accepted on the command line" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        web.main(["--record-passphrase-file", "pw"])
    assert exc.value.code == 2 and "needs --record-key" in capsys.readouterr().err
