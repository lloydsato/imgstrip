"""The local web app: security gates, the inspect/clean/download/save round trip."""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import threading
import urllib.parse
import zipfile

import pytest

from imgstrip import fixtures
from imgstrip.core import heif_available
from imgstrip.inspect import Finding, inspect_bytes
from imgstrip.web import server as web


@pytest.fixture()
def app(tmp_path):
    session = web.Session(str(tmp_path / "work"))
    httpd, session = web.make_server(0, session)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield Client(httpd.server_address[1], session)
    httpd.shutdown()
    httpd.server_close()
    session.close()


class Client:
    def __init__(self, port: int, session: web.Session):
        self.port = port
        self.session = session
        self.cookie = f"imgstrip_{port}={session.cookie_value}"

    def request(self, method, path, body=None, headers=None, *, auth=True, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        hdrs = {"Host": host or f"127.0.0.1:{self.port}"}
        if auth:
            hdrs.update({"Cookie": self.cookie, "X-Imgstrip-Token": self.session.token})
        hdrs.update(headers or {})
        conn.request(method, path, body=body, headers=hdrs)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res, data

    def json(self, method, path, payload=None, **kw):
        body = json.dumps(payload).encode() if payload is not None else None
        res, data = self.request(method, path, body, {"Content-Type": "application/json"}, **kw)
        return res.status, json.loads(data)

    def upload(self, path, data, name="photo.jpg", options=None, headers=None, **kw):
        hdrs = {"X-Filename": urllib.parse.quote(name), "Content-Type": "application/octet-stream"}
        if options is not None:
            hdrs["X-Options"] = urllib.parse.quote(json.dumps(options))
        hdrs.update(headers or {})
        return self.request("POST", path, data, hdrs, **kw)

    def clean(self, data, name="photo.jpg", options=None):
        res, body = self.upload("/api/clean", data, name, options or {})
        assert res.status == 200, body
        return json.loads(body)


# --------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------


def test_page_needs_the_launch_link(app):
    res, body = app.request("GET", "/", auth=False)
    assert res.status == 403
    assert b"link printed in the terminal" in body
    res, _ = app.request("GET", "/?k=wrong", auth=False)
    assert res.status == 403
    res, _ = app.request("GET", f"/?k={app.session.token}", auth=False)
    assert res.status == 303
    cookie = res.getheader("Set-Cookie")
    assert app.session.cookie_value in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    res, body = app.request("GET", "/", auth=False, headers={"Cookie": cookie.split(";")[0]})
    assert res.status == 200
    assert app.session.token.encode() in body
    assert b"{{TOKEN}}" not in body and b"{{VERSION}}" not in body
    assert "frame-ancestors 'none'" in res.getheader("Content-Security-Policy")
    assert res.getheader("X-Frame-Options") == "DENY"


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1", "127.0.0.1:1", "localhost.evil.example:8765"])
def test_foreign_host_header_is_refused(app, host):
    res, _ = app.request("GET", "/", host=host)
    assert res.status == 421
    res, _ = app.upload("/api/inspect", fixtures.jpeg_everything(), host=host)
    assert res.status == 421


def test_localhost_names_are_accepted(app):
    for host in (f"localhost:{app.port}", f"[::1]:{app.port}"):
        res, _ = app.request("GET", "/api/info", host=host)
        assert res.status == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Imgstrip-Token": "wrong"},
        {"X-Imgstrip-Token": ""},
        {"Origin": "http://evil.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Cookie": "imgstrip_1=nope"},
    ],
)
def test_api_writes_need_token_cookie_and_same_origin(app, headers):
    res, body = app.upload("/api/inspect", fixtures.jpeg_everything(), headers=headers)
    assert res.status == 403, body


def test_api_reads_need_the_cookie(app):
    result = app.clean(fixtures.jpeg_everything())
    res, _ = app.request("GET", f"/api/result/{result['id']}", auth=False)
    assert res.status == 403
    res, _ = app.request("GET", "/api/info", auth=False)
    assert res.status == 403


def test_same_origin_requests_pass(app):
    origin = f"http://127.0.0.1:{app.port}"
    res, _ = app.upload(
        "/api/inspect", fixtures.jpeg_everything(), headers={"Origin": origin, "Sec-Fetch-Site": "same-origin"}
    )
    assert res.status == 200


def test_static_files_are_whitelisted(app):
    res, body = app.request("GET", "/static/app.css", auth=False)
    assert res.status == 200 and res.getheader("Content-Type").startswith("text/css") and body
    for path in ("/static/../server.py", "/static/index.html", "/static/fonts/OFL.txt", "/static/%2e%2e/server.py"):
        res, _ = app.request("GET", path, auth=False)
        assert res.status in (403, 404), path


def test_oversized_upload_is_refused_without_reading_it(app, monkeypatch):
    monkeypatch.setattr(web, "MAX_UPLOAD", 1000)
    res, body = app.upload("/api/inspect", b"\xff\xd8\xff" + b"0" * 2000)
    assert res.status == 413
    assert "limit" in json.loads(body)["error"]


# --------------------------------------------------------------------------
# Inspect
# --------------------------------------------------------------------------


def test_inspect_reports_and_summarises(app):
    res, body = app.upload("/api/inspect", fixtures.jpeg_everything())
    assert res.status == 200
    report = json.loads(body)
    assert report["format"] == "JPEG" and report["size"] == [160, 120]
    labels = {f["label"] for f in report["findings"]}
    assert {"EXIF", "GPS location", "Embedded EXIF thumbnail", "XMP"} <= labels
    keys = {r["key"]: r["value"] for r in report["reveals"]}
    assert keys["location"] == "37.422039, -122.083994"
    assert keys["camera"] == "CANARY-MAKE Corp CANARY-MODEL X1"
    assert keys["time"] == "17 May 2024, 13:37"
    assert "preview" in keys and "trailer" in keys
    assert report["reveals"][0]["key"] == "location"


def test_deep_scan_finds_lsb_payload(app):
    data = fixtures.png_lsb_stego()
    res, body = app.upload("/api/inspect", data, "memo.png")
    assert not any("LSB" in f["label"] for f in json.loads(body)["findings"])
    res, body = app.upload("/api/inspect?deep=1", data, "memo.png")
    report = json.loads(body)
    assert report["deep"] is True
    assert any("LSB steganography" in f["label"] for f in report["findings"])


def test_inspect_unknown_container(app):
    res, body = app.upload("/api/inspect", b"not an image at all" * 10, "x.jpg")
    report = json.loads(body)
    assert res.status == 200 and report["format"] is None and report["findings"] == []


def test_inspect_empty_upload(app):
    res, body = app.upload("/api/inspect", b"")
    assert res.status == 400


def test_preview_is_a_pixels_only_png(app):
    res, body = app.upload("/api/preview", fixtures.jpeg_everything())
    assert res.status == 200 and res.getheader("Content-Type") == "image/png"
    assert body.startswith(b"\x89PNG") and inspect_bytes(body).clean
    res, _ = app.upload("/api/preview", b"garbage" * 20)
    assert res.status == 422


@pytest.mark.skipif(not heif_available(), reason="pillow-heif not installed")
def test_preview_for_heic(app):
    data = fixtures.heic_everything()
    res, body = app.upload("/api/preview", data, "IMG_1.HEIC")
    assert res.status == 200 and body.startswith(b"\x89PNG")


# --------------------------------------------------------------------------
# Clean, download, zip, save
# --------------------------------------------------------------------------


def test_clean_then_download(app):
    data = fixtures.jpeg_everything()
    result = app.clean(data, "IMG_0001.JPG")
    assert result["filename"] == "IMG_0001.clean.JPG"
    assert result["input_format"] == "JPEG" and result["output_format"] == "JPEG"
    assert result["bytes_in"] == len(data)
    assert {f["label"] for f in result["removed"]} >= {"EXIF", "GPS location"}
    res, body = app.request("GET", f"/api/result/{result['id']}")
    assert res.status == 200 and res.getheader("Content-Type") == "image/jpeg"
    assert "attachment" in res.getheader("Content-Disposition")
    assert "IMG_0001.clean.JPG" in res.getheader("Content-Disposition")
    assert hashlib.sha256(body).hexdigest() == result["sha256"]
    assert len(body) == result["bytes_out"]
    assert inspect_bytes(body).clean
    assert b"CANARY" not in body


def test_clean_with_options(app):
    result = app.clean(fixtures.jpeg_everything(), "a.jpg", {"format": "png", "auto_orient": False})
    assert result["output_format"] == "PNG" and result["filename"] == "a.clean.png"
    result = app.clean(fixtures.jpeg_everything(), "b.jpg", {"jpeg_lossless": True})
    assert result["pixels_exact"] and not result["lossy"]
    assert "kept the compressed image data bit-for-bit (no re-encoding)" in result["actions"]
    result = app.clean(fixtures.jpeg_everything(), "c.jpg", {"quality": 80})
    assert "re-encoded JPEG at quality 80" in result["actions"] and result["lossy"]


@pytest.mark.parametrize(
    "options",
    [
        {"format": "exe"},
        {"quality": 0},
        {"quality": 101},
        {"quality": "95"},
        {"quality": True},
        {"icc": "keep"},
        {"scrub_lsb": "yes"},
        [],
    ],
)
def test_bad_options_are_rejected(app, options):
    res, body = app.upload("/api/clean", fixtures.jpeg_everything(), options=options)
    assert res.status == 400, body


def test_unreadable_file_fails_cleanly(app):
    res, body = app.upload("/api/clean", b"not an image" * 20, "x.jpg", options={})
    assert res.status == 422
    message = json.loads(body)["error"]
    assert message[0].isupper()


def test_camera_raw_is_refused(app):
    res, body = app.upload("/api/clean", fixtures.jpeg_everything(), "DSC_0001.NEF", options={})
    assert res.status == 422 and "RAW" in json.loads(body)["error"]


def test_filename_is_sanitised(app):
    result = app.clean(fixtures.jpeg_everything(), "../../etc/pass\x01wd.jpg")
    assert result["filename"] == "passwd.clean.jpg"


def test_zip_has_no_dates_and_unique_names(app):
    a = app.clean(fixtures.jpeg_everything(), "same.jpg")
    b = app.clean(fixtures.jpeg_everything(), "same.jpg")
    status, z = app.json("POST", "/api/zip", {"ids": [a["id"], b["id"]]})
    assert status == 200 and z["filename"] == "imgstrip-clean.zip"
    res, body = app.request("GET", f"/api/result/{z['id']}")
    assert res.getheader("Content-Type") == "application/zip"
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        infos = zf.infolist()
        assert [i.filename for i in infos] == ["same.clean.jpg", "same.clean (2).jpg"]
        assert all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in infos)
        assert all(i.extra == b"" and i.comment == b"" for i in infos)
        assert hashlib.sha256(zf.read(infos[0])).hexdigest() == a["sha256"]
    assert zf.comment == b""


def test_zip_with_unknown_ids(app):
    status, body = app.json("POST", "/api/zip", {"ids": ["A" * 24]})
    assert status == 404
    status, body = app.json("POST", "/api/zip", {"ids": ["../../x"]})
    assert status == 400
    status, body = app.json("POST", "/api/zip", {"ids": []})
    assert status == 400


def test_save_to_folder_never_overwrites(app, tmp_path):
    out = tmp_path / "out" / "nested"
    a = app.clean(fixtures.jpeg_everything(), "photo.jpg")
    status, res = app.json("POST", "/api/save", {"ids": [a["id"]], "folder": str(out)})
    assert status == 200 and res["files"][0]["ok"]
    first = (out / "photo.clean.jpg").read_bytes()
    assert hashlib.sha256(first).hexdigest() == a["sha256"]
    b = app.clean(fixtures.png_everything(), "photo.jpg", {"format": "jpeg"})
    status, res = app.json("POST", "/api/save", {"ids": [b["id"]], "folder": str(out)})
    assert res["files"][0]["name"] == "photo.clean (2).jpg"
    assert (out / "photo.clean.jpg").read_bytes() == first
    assert sorted(p.name for p in out.iterdir()) == ["photo.clean (2).jpg", "photo.clean.jpg"]


def test_save_folder_validation(app, tmp_path):
    a = app.clean(fixtures.jpeg_everything())
    status, res = app.json("POST", "/api/save", {"ids": [a["id"]], "folder": "relative/dir"})
    assert status == 400 and "full folder path" in res["error"]
    afile = tmp_path / "file.txt"
    afile.write_text("x")
    status, res = app.json("POST", "/api/save", {"ids": [a["id"]], "folder": str(afile)})
    assert status == 400 and "not a folder" in res["error"]
    status, res = app.json("POST", "/api/save", {"ids": [a["id"]], "folder": ""})
    assert status == 400


def test_write_new_refuses_existing(tmp_path):
    dest = tmp_path / "a.jpg"
    web.write_new(dest, b"one")
    with pytest.raises(FileExistsError):
        web.write_new(dest, b"two")
    assert dest.read_bytes() == b"one"
    assert [p.name for p in tmp_path.iterdir()] == ["a.jpg"]


def test_delete_and_reset(app):
    a = app.clean(fixtures.jpeg_everything())
    b = app.clean(fixtures.jpeg_everything())
    status, res = app.json("DELETE", f"/api/result/{a['id']}")
    assert status == 200 and res["ok"]
    res, _ = app.request("GET", f"/api/result/{a['id']}")
    assert res.status == 404
    status, res = app.json("POST", "/api/reset", {})
    assert status == 200
    res, _ = app.request("GET", f"/api/result/{b['id']}")
    assert res.status == 404
    assert list(app.session.dir.iterdir()) == []


def test_info(app):
    status, info = app.json("GET", "/api/info")
    assert status == 200
    assert "JPEG" in info["formats"] and "PNG" in info["formats"]
    assert info["version"]


def test_session_close_removes_workdir(tmp_path):
    s = web.Session(str(tmp_path / "w"))
    s.put(b"x", "a.png", "image/png")
    s.close()
    assert not (tmp_path / "w").exists()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def test_reveals_parses_values_containing_commas():
    detail = "11250 bytes; make=NIKON, model=COOLPIX P6000, software=Nikon Transfer 1.1 W, date=2008:11:01 21:15:07, taken=2008:10:22 16:28:39, body serial=12, 34, maker notes"
    found = web.reveals([Finding("metadata", "EXIF", detail), Finding("metadata", "GPS location", "43.4, 11.8")])
    got = {r["key"]: r["value"] for r in found}
    assert got == {
        "location": "43.4, 11.8",
        "camera": "NIKON COOLPIX P6000",
        "time": "22 Oct 2008, 16:28",
        "serial": "12, 34",
        "software": "Nikon Transfer 1.1 W",
    }
    # a model that already names the maker is not doubled
    found = web.reveals([Finding("metadata", "EXIF", "10 bytes; make=Canon, model=Canon EOS 40D")])
    assert found == [{"key": "camera", "label": "Camera", "value": "Canon EOS 40D"}]
    assert web.reveals([Finding("hidden", "Data after end of JPEG image", "29 bytes (ZIP archive)")]) == [
        {"key": "trailer", "label": "Appended data", "value": "29 bytes (ZIP archive)"}
    ]


def test_safe_filename():
    assert web.safe_filename("..%2F..%2Fetc%2Fpasswd") == "passwd"
    assert web.safe_filename("C:\\Users\\me\\IMG 1.jpg") == "IMG 1.jpg"
    assert web.safe_filename("") == "image"
    assert web.safe_filename("...") == "image"
    assert web.safe_filename('a<b>:"c|?*.png') == "a_b___c___.png"
    assert len(web.safe_filename("x" * 500 + ".jpg")) <= web.MAX_NAME


def test_clean_name():
    assert web.clean_name("photo.jpeg", "JPEG") == "photo.clean.jpeg"
    assert web.clean_name("photo.clean.jpg", "JPEG") == "photo.clean.jpg"
    assert web.clean_name("scan.tif", "PNG") == "scan.clean.png"
    assert web.clean_name("noext", "PNG") == "noext.clean.png"
