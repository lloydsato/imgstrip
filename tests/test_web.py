"""The local web app: security gates, the inspect/clean/download/save round trip."""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import zipfile
from pathlib import Path

import pytest

from imgstrip import fixtures
from imgstrip.core import Options, heif_available
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

    def request(self, method, path, body=None, headers=None, *, auth=True, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        hdrs = {"Host": host or f"127.0.0.1:{self.port}"}
        if auth:
            hdrs["X-Imgstrip-Token"] = self.session.token
        hdrs.update(headers or {})
        conn.request(method, path, body=body, headers=hdrs)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res, data

    def json(self, method, path, payload=None, headers=None, **kw):
        body = json.dumps(payload).encode() if payload is not None else None
        res, data = self.request(method, path, body, {"Content-Type": "application/json", **(headers or {})}, **kw)
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


def test_page_is_a_static_shell_without_secrets(app):
    res, body = app.request("GET", "/", auth=False)
    assert res.status == 200
    assert app.session.token.encode() not in body and app.session.launch_key.encode() not in body
    assert b"{{" not in body
    assert res.getheader("Set-Cookie") is None
    assert "frame-ancestors 'none'" in res.getheader("Content-Security-Policy")
    assert res.getheader("X-Frame-Options") == "DENY"


def test_launch_key_is_single_use_and_rotates(app):
    issued = []
    app.session.on_launch = issued.append
    first = app.session.launch_key
    status, res = app.json("POST", "/api/session", {"key": "wrong"}, auth=False)
    assert status == 403 and issued == []
    old_token = app.session.token
    status, res = app.json("POST", "/api/session", {"key": first}, auth=False)
    assert status == 200 and res["token"] == app.session.token != old_token
    assert issued == [app.session.launch_key] and app.session.launch_key != first
    status, res = app.json("POST", "/api/session", {"key": first}, auth=False)
    assert status == 403 and "already been used" in res["error"]
    tab_one = res_token = app.session.token
    status, res = app.json("POST", "/api/session", {"key": issued[0]}, auth=False)
    assert status == 200 and res["token"] != res_token
    # the newest tab wins: the earlier tab's token now says it was replaced
    res, body = app.request("GET", "/api/info", auth=False, headers={"X-Imgstrip-Token": tab_one})
    assert res.status == 403 and json.loads(body)["code"] == "replaced"
    res, body = app.request("GET", "/api/info", auth=False, headers={"X-Imgstrip-Token": "never-issued"})
    assert res.status == 403 and json.loads(body)["code"] == "expired"


def test_launch_key_needs_same_origin(app):
    status, _ = app.json(
        "POST", "/api/session", {"key": app.session.launch_key}, auth=False, headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert status == 403


def test_non_ascii_secrets_are_refused_not_crashed(app):
    status, _ = app.json("POST", "/api/session", {"key": "\u00e9" * 10}, auth=False)
    assert status == 403
    res, body = app.upload("/api/inspect", fixtures.jpeg_everything(), headers={"X-Imgstrip-Token": "\u00e9"})
    assert res.status == 403, body


def test_error_responses_close_the_connection(app):
    """A refused request's unread body must never be parsed as a second request."""
    smuggled = f"GET /static/favicon.svg HTTP/1.1\r\nHost: 127.0.0.1:{app.port}\r\n\r\n".encode()
    raw = (
        f"POST /api/inspect HTTP/1.1\r\nHost: evil.example\r\nContent-Length: {len(smuggled)}\r\n\r\n".encode()
        + smuggled
    )
    with socket.create_connection(("127.0.0.1", app.port), timeout=10) as sock:
        sock.sendall(raw)
        received = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            received += chunk
    assert received.count(b"HTTP/1.1 ") == 1 and b" 421 " in received


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
    ],
)
def test_api_needs_token_and_same_origin(app, headers):
    res, body = app.upload("/api/inspect", fixtures.jpeg_everything(), headers=headers)
    assert res.status == 403, body
    res, body = app.request("GET", "/api/info", headers=headers)
    assert res.status == 403, body


def test_results_are_capabilities(app):
    """A result downloads by its unguessable id alone (plain links), nothing else does."""
    result = app.clean(fixtures.jpeg_everything())
    assert len(result["id"]) >= 32
    res, _ = app.request("GET", f"/api/result/{result['id']}", auth=False)
    assert res.status == 200
    res, _ = app.request("GET", "/api/info", auth=False)
    assert res.status == 403
    res, _ = app.request("GET", "/api/result/" + "A" * 32, auth=False)
    assert res.status == 404


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
        {"scrub_prnu": "yes"},
        {"scrub_prnu": 1},
        {"random_name": "yes"},
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


def test_write_new_can_copy_a_file_instead_of_holding_it_in_memory(tmp_path):
    source = tmp_path / "result.bin"
    content = os.urandom(3 * (1 << 20) + 17)  # more than one copy chunk
    source.write_bytes(content)
    dest = tmp_path / "out" / "copy.bin"
    web.write_new(dest, source)
    assert dest.read_bytes() == content
    with pytest.raises(FileExistsError):
        web.write_new(dest, source)
    assert [p.name for p in dest.parent.iterdir()] == ["copy.bin"]
    with pytest.raises(FileNotFoundError):
        web.write_new(tmp_path / "other.bin", tmp_path / "missing.bin")
    assert not (tmp_path / "other.bin").exists() and not [p for p in tmp_path.iterdir() if p.name.startswith(".imgstrip-")]


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
    assert [p.name for p in app.session.dir.iterdir()] == [web.PID_FILE]


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


# --------------------------------------------------------------------------
# Review fixes
# --------------------------------------------------------------------------


def test_only_one_zip_is_kept(app):
    a = app.clean(fixtures.jpeg_everything(), "a.jpg")
    b = app.clean(fixtures.jpeg_everything(), "b.jpg")
    _, z1 = app.json("POST", "/api/zip", {"ids": [a["id"], b["id"]]})
    _, z2 = app.json("POST", "/api/zip", {"ids": [a["id"], b["id"]]})
    res, _ = app.request("GET", f"/api/result/{z1['id']}")
    assert res.status == 404
    res, _ = app.request("GET", f"/api/result/{z2['id']}")
    assert res.status == 200


def test_deep_scan_skips_huge_images(app, monkeypatch):
    monkeypatch.setattr(web, "DEEP_SCAN_MAX_PIXELS", 1000)
    res, body = app.upload("/api/inspect?deep=1", fixtures.png_lsb_stego(), "memo.png")
    report = json.loads(body)
    assert not any("LSB" in f["label"] for f in report["findings"])
    assert any("not scanned" in n for n in report["notes"])


def test_unknown_containers_are_probed(app):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (32, 32), (10, 20, 30, 255)).save(buf, "ICO")
    res, body = app.upload("/api/inspect", buf.getvalue(), "icon.ico")
    report = json.loads(body)
    assert report["format"] is None and report["decodable"] == "ICO" and report["size"] == [32, 32]
    res, body = app.upload("/api/inspect", b"plain text, not an image" * 4, "notes.jpg")
    report = json.loads(body)
    assert report["format"] is None and report["decodable"] is None


def test_preview_scales_16_bit_images():
    import numpy as np
    from PIL import Image

    ramp = (np.linspace(1000, 60000, 64 * 64).reshape(64, 64)).astype(np.uint16)
    buf = io.BytesIO()
    Image.fromarray(ramp).save(buf, "TIFF")
    png = web.preview_bytes(buf.getvalue())
    pixels = np.asarray(Image.open(io.BytesIO(png)).convert("L"))
    assert pixels.min() < 30 and pixels.max() > 225


def test_preview_refuses_giant_images(monkeypatch):
    monkeypatch.setattr(web, "PREVIEW_MAX_PIXELS", 100)
    with pytest.raises(web.StripError):
        web.preview_bytes(fixtures.jpeg_everything())


def test_reveals_gps_without_coordinates_and_xmp_location():
    found = web.reveals([Finding("metadata", "GPS data", "1 GPS tags")])
    assert found == [{"key": "gps", "label": "GPS tags", "value": "1 GPS tags"}]
    found = web.reveals([Finding("metadata", "XMP", "320 bytes; contains city, GPS")])
    assert found == [{"key": "location", "label": "Location", "value": "recorded in the XMP data"}]
    both = web.reveals([Finding("metadata", "GPS location", "1.0, 2.0"), Finding("metadata", "XMP", "contains GPS")])
    assert both == [{"key": "location", "label": "Location", "value": "1.0, 2.0"}]


def test_write_new_without_hard_links_still_never_replaces(tmp_path, monkeypatch):
    def no_links(src, dst):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(web.os, "link", no_links)
    dest = tmp_path / "a.jpg"
    web.write_new(dest, b"one")
    assert dest.read_bytes() == b"one"
    with pytest.raises(FileExistsError):
        web.write_new(dest, b"two")
    assert dest.read_bytes() == b"one"
    assert [p.name for p in tmp_path.iterdir()] == ["a.jpg"]


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX only")
def test_stale_work_folders_are_swept(tmp_path, monkeypatch):
    monkeypatch.setattr(web.tempfile, "gettempdir", lambda: str(tmp_path))
    dead = tmp_path / "imgstrip-web-dead"
    dead.mkdir()
    (dead / web.PID_FILE).write_text(str(os.getpid()))  # a pid that looks alive, but no lock is held
    (dead / "result").write_bytes(b"clean pixels")
    live = web.Session(str(tmp_path / "imgstrip-web-live"))  # holds its lock, whatever its pid says
    (live.dir / web.PID_FILE).write_text("999999999")
    other = tmp_path / "unrelated"
    other.mkdir()
    linked = tmp_path / "imgstrip-web-link"
    linked.symlink_to(other)
    web.sweep_stale_sessions()
    assert not dead.exists() and live.dir.exists() and other.exists() and linked.is_symlink()
    live.close()


def _default_signal_dispositions() -> None:
    """Run in the child before exec: undo an ignored or blocked SIGHUP/SIGTERM inherited from the test runner.

    nohup, some CI runners and some process supervisors start the test process with SIGHUP ignored, and an
    ignored signal stays ignored across exec.  The server then (rightly) respects it, so the signal test below
    would test the runner's set-up instead of the server.
    """
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGHUP, signal.SIGTERM})


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="POSIX only")
@pytest.mark.parametrize("inherited", ["default", "ignored"])
@pytest.mark.parametrize("sig", ["SIGTERM", "SIGHUP"])
def test_signals_still_delete_cleaned_files(tmp_path, sig, inherited):
    """Closing the terminal or `kill` deletes the cleaned files, whatever SIGHUP disposition the runner has.

    ``inherited="ignored"`` reproduces a test run under nohup (finding F7): the test process itself ignores
    SIGHUP while it starts the server, and the test must pass all the same.
    """
    env = {**os.environ, "TMPDIR": str(tmp_path)}
    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN if inherited == "ignored" else signal.SIG_DFL)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "imgstrip.web", "--no-browser", "--port", "0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            preexec_fn=_default_signal_dispositions,
        )
    finally:
        signal.signal(signal.SIGHUP, previous)
    try:
        proc.stdout.readline()
        url = proc.stdout.readline().strip()
        port = int(urllib.parse.urlsplit(url).port)
        key = urllib.parse.urlsplit(url).fragment.split("=", 1)[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        conn.request("POST", "/api/session", json.dumps({"key": key}), {"Host": f"127.0.0.1:{port}"})
        token = json.loads(conn.getresponse().read())["token"]
        conn.request(
            "POST",
            "/api/clean",
            fixtures.jpeg_everything(),
            {"Host": f"127.0.0.1:{port}", "X-Imgstrip-Token": token, "X-Filename": "a.jpg", "X-Options": "{}"},
        )
        assert conn.getresponse().status == 200
        conn.close()
        assert list(tmp_path.glob("imgstrip-web-*/*"))
        proc.send_signal(getattr(signal, sig))
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    deadline = time.time() + 5
    while list(tmp_path.glob("imgstrip-web-*")) and time.time() < deadline:
        time.sleep(0.05)
    assert list(tmp_path.glob("imgstrip-web-*")) == []


def test_verification_wording_follows_what_was_proved(app):
    # a photo squeezed into a GIF palette is verified within a tolerance, not exactly
    gif = app.clean(fixtures.png_everything(), "photo.png", {"format": "gif"})
    assert gif["output_format"] == "GIF" and gif["lossy"]
    # a palette image fits a GIF exactly
    palette = app.clean(fixtures.png_palette_tricks(), "logo.png", {"format": "gif"})
    assert not palette["lossy"]
    # the LSB scrub changes pixels on purpose, but the PNG still decodes to exactly what was written
    scrubbed = app.clean(fixtures.png_everything(), "photo.png", {"scrub_lsb": True})
    assert scrubbed["scrubbed"] and not scrubbed["lossy"] and not scrubbed["pixels_exact"]
    jpeg = app.clean(fixtures.jpeg_everything(), "photo.jpg")
    assert jpeg["lossy"] and not jpeg["scrubbed"]


def _one_bit_png() -> bytes:
    import numpy as np
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(np.tile(np.array([[0, 255]], np.uint8), (8, 4)), "L").convert("1").save(buf, "PNG")
    return buf.getvalue()


def test_a_scrub_that_could_not_be_applied_is_reported_as_such(app):
    # A 1-bit PNG has no least-significant bits to randomise: the page must be able to tell it was not scrubbed.
    result = app.clean(_one_bit_png(), "mask.png", {"scrub_lsb": True})
    assert result["scrub_requested"] is True and result["lsb_scrub"] == "not_applied" and not result["scrubbed"]
    applied = app.clean(fixtures.png_everything(), "photo.png", {"scrub_lsb": True})
    assert applied["lsb_scrub"] == "applied"
    default = app.clean(fixtures.png_everything(), "photo.png")
    assert default["scrub_requested"] is False and default["lsb_scrub"] == "off"


def test_random_file_names(app):
    plain = app.clean(fixtures.png_everything(), "IMG_20240517_133700.png")
    assert plain["filename"] == "IMG_20240517_133700.clean.png"
    random_a = app.clean(fixtures.png_everything(), "IMG_20240517_133700.png", {"random_name": True})
    random_b = app.clean(fixtures.png_everything(), "IMG_20240517_133700.png", {"random_name": True})
    assert re.fullmatch(r"[0-9a-f]{16}\.png", random_a["filename"])
    assert random_a["filename"] != random_b["filename"]
    jpeg = app.clean(fixtures.jpeg_everything(), "IMG_20240517.JPEG", {"random_name": True, "format": "png"})
    assert re.fullmatch(r"[0-9a-f]{16}\.png", jpeg["filename"])


def test_sensor_fingerprint_option(app):
    default = app.clean(fixtures.png_everything(), "photo.png")
    assert default["prnu"] is False
    result = app.clean(fixtures.png_everything(), "photo.png", {"scrub_prnu": True})
    assert result["prnu"] is True and not result["pixels_exact"] and not result["lossy"]
    assert any("camera sensor fingerprint" in a for a in result["actions"])
    # A palette image cannot carry a fingerprint: skipped, and the page is told why.
    palette = app.clean(fixtures.png_palette_tricks(), "logo.png", {"scrub_prnu": True})
    assert palette["prnu"] is False and any("sensor-fingerprint" in w for w in palette["warnings"])
    # The bit-exact JPEG switch cannot honour it, so it fails instead of silently doing nothing.
    res, body = app.upload(
        "/api/clean", fixtures.jpeg_everything(), options={"scrub_prnu": True, "jpeg_lossless": True}
    )
    assert res.status == 422 and "cannot be combined" in json.loads(body)["error"]


def test_every_option_in_the_page_reaches_the_server():
    static = Path(web.__file__).parent / "static"
    html = (static / "index.html").read_text()
    js = (static / "app.js").read_text()
    form = html[html.index('id="optionsForm"') : html.index("</form>", html.index('id="optionsForm"'))]
    names = set(re.findall(r'<(?:input|select)[^>]*\bname="(\w+)"', form))
    assert {"scrub_lsb", "scrub_prnu", "jpeg_lossless", "auto_orient", "optimize", "icc", "format", "quality"} <= names
    send = js[js.index("function cleanOptions") : js.index("const optionsKey")]
    for name in names - {"deep"}:  # the deep scan is a scan setting, not a clean option
        assert re.search(rf"\b{name}\b", send), f"{name} is in the page but never sent to /api/clean"
        assert re.search(rf"\b{name}\b", js[: js.index("const OPTIONS_KEY")]), f"{name} has no default"


def test_pixels_scanned_flag(app, monkeypatch):
    res, body = app.upload("/api/inspect?deep=1", fixtures.png_lsb_stego(), "memo.png")
    assert json.loads(body)["pixels_scanned"] is True
    res, body = app.upload("/api/inspect", fixtures.png_lsb_stego(), "memo.png")
    assert json.loads(body)["pixels_scanned"] is False
    monkeypatch.setattr(web, "DEEP_SCAN_MAX_PIXELS", 1000)
    res, body = app.upload("/api/inspect?deep=1", fixtures.png_lsb_stego(), "memo.png")
    report = json.loads(body)
    assert report["deep"] is True and report["pixels_scanned"] is False


def test_deep_scan_size_comes_from_the_decoder(app, monkeypatch):
    """A simple lossless WebP has no size in the container summary; the cap must still apply."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (1, 2, 3)).save(buf, "WEBP", lossless=True)
    monkeypatch.setattr(web, "DEEP_SCAN_MAX_PIXELS", 1000)
    res, body = app.upload("/api/inspect?deep=1", buf.getvalue(), "a.webp")
    report = json.loads(body)
    assert report["pixels_scanned"] is False and any("not scanned" in n for n in report["notes"])


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="POSIX only")
def test_nohup_is_respected(tmp_path):
    env = {**os.environ, "TMPDIR": str(tmp_path)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "imgstrip.web", "--no-browser", "--port", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN),
    )
    try:
        proc.stdout.readline()
        proc.stdout.readline()
        proc.send_signal(signal.SIGHUP)
        time.sleep(0.5)
        assert proc.poll() is None  # still running, as nohup asked
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert list(tmp_path.glob("imgstrip-web-*")) == []


# --------------------------------------------------------------------------
# Resource limits
# --------------------------------------------------------------------------


def test_web_pixel_limit_is_lower_than_the_cli_default_and_reported_in_page_terms(app, monkeypatch):
    assert web.WEB_MAX_PIXELS < Options().max_pixels
    assert web.parse_options({}).max_pixels == web.WEB_MAX_PIXELS
    monkeypatch.setattr(web, "WEB_MAX_PIXELS", 1000)
    res, body = app.upload("/api/clean", fixtures.png_everything(), "big.png", {})
    assert res.status == 422
    message = json.loads(body)["error"]
    assert "limit" in message and "--max-pixels" not in message


def _connect(app):
    import socket

    return socket.create_connection(("127.0.0.1", app.port), timeout=10)


def _closed(sock) -> bool:
    """True when the server hangs up (after saying whatever it wants to) rather than waiting for more."""
    try:
        while sock.recv(4096):
            pass
        return True
    except OSError:
        return False


def test_idle_connection_is_dropped(app, monkeypatch):
    monkeypatch.setattr(web.Handler, "timeout", 1)
    sock = _connect(app)
    sock.sendall(b"POST /api/clean HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\npartial")
    assert _closed(sock), "a connection that stops sending is closed after the timeout"
    sock.close()


def test_connections_beyond_the_cap_are_closed_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(web.Server, "max_connections", 2)
    session = web.Session(str(tmp_path / "work"))
    httpd, session = web.make_server(0, session)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    held = []
    try:
        client = Client(httpd.server_address[1], session)
        for _ in range(2):
            sock = _connect(client)
            sock.sendall(b"GET /api/session HTTP/1.1\r\nHost: x\r\nX-Do-Not: finish")  # never finishes the request
            held.append(sock)
        import time

        time.sleep(0.3)
        extra = _connect(client)
        assert _closed(extra)
        extra.close()
        held.pop().close()  # freeing a slot lets a real request through again
        time.sleep(0.5)
        res, _ = client.request("GET", "/api/session")
        assert res.status in (200, 401, 403, 404)
    finally:
        for sock in held:
            sock.close()
        httpd.shutdown()
        httpd.server_close()
        session.close()


# --------------------------------------------------------------------------
# Streaming: uploads, stored results, ZIPs
# --------------------------------------------------------------------------


def _reference_zip(members: list[tuple[str, bytes]]) -> bytes:
    """The ZIP as the in-memory writer used to build it, to prove the streamed one is byte-identical."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, content in members:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.create_system = 3
            zf.writestr(info, content)
    return buf.getvalue()


def test_streamed_zip_is_byte_identical_to_the_in_memory_one(app):
    # Empty, tiny, exactly one copy chunk, just over it, and several chunks: every boundary of the 1 MiB loop.
    sizes = [0, 1, 4096, 1 << 20, (1 << 20) + 1, 3 * (1 << 20) + 17]
    members, ids = [], []
    for i, size in enumerate(sizes):
        content = os.urandom(size)
        ids.append(app.session.put(content, "same.bin" if i % 2 else f"f{i}.bin", "application/octet-stream"))
        members.append((content,))
    status, z = app.json("POST", "/api/zip", {"ids": ids})
    assert status == 200 and z["bytes"] > sum(sizes)
    res, body = app.request("GET", f"/api/result/{z['id']}")
    assert res.status == 200 and len(body) == z["bytes"] == int(res.getheader("Content-Length"))
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        assert zf.testzip() is None
        names = zf.namelist()
        assert len(set(names)) == len(sizes), "duplicate input names are made unique"
        assert [zf.read(n) for n in names] == [m[0] for m in members]
        assert {i.date_time for i in zf.infolist()} == {(1980, 1, 1, 0, 0, 0)}
    assert body == _reference_zip(list(zip(names, [m[0] for m in members], strict=True)))


def test_zip_of_one_empty_file_and_zip_leaves_no_partial_file_on_failure(app, monkeypatch):
    rid = app.session.put(b"", "empty.bin", "application/octet-stream")
    status, z = app.json("POST", "/api/zip", {"ids": [rid]})
    assert status == 200
    res, body = app.request("GET", f"/api/result/{z['id']}")
    assert zipfile.ZipFile(io.BytesIO(body)).read("empty.bin") == b""

    before = sorted(p.name for p in app.session.dir.iterdir())

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(web.shutil, "copyfileobj", boom)
    status, body = app.json("POST", "/api/zip", {"ids": [rid]})
    assert status == 500
    assert body["error"] == "Internal error (OSError).", "the type only: a message can quote a path"
    assert sorted(p.name for p in app.session.dir.iterdir()) == before, "a failed ZIP must not leave a stray file"
    # ... and the earlier ZIP is still the one on offer
    monkeypatch.undo()
    res, _ = app.request("GET", f"/api/result/{z['id']}")
    assert res.status == 200


def test_put_with_cleans_up_when_the_writer_fails(app):
    def write(fh):
        fh.write(b"partial")
        raise RuntimeError("writer died")

    before = sorted(p.name for p in app.session.dir.iterdir())
    with pytest.raises(RuntimeError):
        app.session.put_with("x.bin", "application/octet-stream", write)
    assert sorted(p.name for p in app.session.dir.iterdir()) == before
    rid = app.session.put(b"abc", "ok.bin", "application/octet-stream")
    assert app.session.get(rid).size == 3


def test_download_head_and_large_get(app):
    content = os.urandom(5 * (1 << 20) + 123)
    rid = app.session.put(content, "big.bin", "application/octet-stream")
    res, body = app.request("HEAD", f"/api/result/{rid}")
    assert res.status == 200 and body == b""
    assert int(res.getheader("Content-Length")) == len(content)
    assert "attachment" in res.getheader("Content-Disposition")
    assert res.getheader("Cache-Control") == "no-store"
    res, body = app.request("GET", f"/api/result/{rid}")
    assert hashlib.sha256(body).digest() == hashlib.sha256(content).digest() and len(body) == len(content)


def test_download_of_a_vanished_result_is_a_clean_error_not_a_torn_response(app):
    rid = app.session.put(b"data", "gone.bin", "application/octet-stream")
    app.session.get(rid).path.unlink()  # the session file disappears while the id is still known
    res, body = app.request("GET", f"/api/result/{rid}")
    assert res.status == 500 and "error" in json.loads(body)
    res, body = app.request("GET", f"/api/result/{'A' * 32}")
    assert res.status == 404


def test_download_keeps_working_after_the_session_deletes_it_mid_session(app):
    first = app.session.put(b"1" * 100, "a.bin", "application/octet-stream")
    assert app.request("GET", f"/api/result/{first}")[0].status == 200
    assert app.session.delete(first)
    assert app.request("GET", f"/api/result/{first}")[0].status == 404


@pytest.mark.parametrize("size", [1, (1 << 20) - 1, 1 << 20, (1 << 20) + 1, 3 * (1 << 20) + 5])
def test_upload_round_trips_at_chunk_boundaries(app, monkeypatch, size):
    seen = []
    real = web.inspect_payload
    monkeypatch.setattr(web, "inspect_payload", lambda data, deep: seen.append(data) or real(data, deep))
    payload = b"\xff\xd8\xff" + os.urandom(size - 3) if size > 3 else b"\xff\xd8\xff"[:size]
    res, _ = app.upload("/api/inspect", payload)
    assert res.status == 200
    assert seen == [payload] and isinstance(seen[0], bytes), "the handler gets the exact bytes, as immutable bytes"


def test_upload_exactly_at_the_limit_is_accepted_and_one_over_is_refused(app, monkeypatch):
    monkeypatch.setattr(web, "MAX_UPLOAD", 4096)
    ok, _ = app.upload("/api/inspect", b"\xff\xd8\xff" + b"0" * (4096 - 3))
    assert ok.status == 200
    over, body = app.upload("/api/inspect", b"\xff\xd8\xff" + b"0" * (4096 - 2))
    assert over.status == 413


def test_empty_and_malformed_content_length(app):
    for header, status in (("0", 400), ("-5", 400), ("abc", 400)):
        sock = _connect(app)
        request = (
            f"POST /api/inspect HTTP/1.1\r\nHost: 127.0.0.1:{app.port}\r\nX-Imgstrip-Token: {app.session.token}\r\n"
            f"Content-Length: {header}\r\n\r\n"
        )
        sock.sendall(request.encode())
        assert sock.recv(64).startswith(f"HTTP/1.1 {status}".encode()), header
        sock.close()


def test_upload_cut_short_is_refused_and_the_server_survives(app):
    sock = _connect(app)
    head = (
        f"POST /api/inspect HTTP/1.1\r\nHost: 127.0.0.1:{app.port}\r\nX-Imgstrip-Token: {app.session.token}\r\n"
        "Content-Length: 5000000\r\n\r\n"
    )
    sock.sendall(head.encode() + b"\xff\xd8\xff" + os.urandom(1 << 20))
    sock.shutdown(socket.SHUT_WR)  # the client gives up mid-upload
    reply = b""
    while chunk := sock.recv(4096):
        reply += chunk
    assert reply.startswith(b"HTTP/1.1 400") and b"cut short" in reply
    sock.close()
    res, _ = app.upload("/api/inspect", fixtures.jpeg_everything())
    assert res.status == 200, "one aborted upload must not affect the next request"


def test_chunked_upload_is_refused(app):
    sock = _connect(app)
    head = (
        f"POST /api/inspect HTTP/1.1\r\nHost: 127.0.0.1:{app.port}\r\nX-Imgstrip-Token: {app.session.token}\r\n"
        "Transfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n"
    )
    sock.sendall(head.encode())
    assert sock.recv(64).startswith(b"HTTP/1.1 411")
    sock.close()
