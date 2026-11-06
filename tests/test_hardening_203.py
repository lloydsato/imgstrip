"""Hardening of 2.0.3 outside the sandbox itself (that is tests/test_confinement.py): the web app's gates, the folders it
saves into, the limits and the in-place safety of the command line, the inspector's budgets, the checks on what a
worker answers, and the pins of the release workflow."""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import socket
import stat
import subprocess
import sys
import threading
import zlib
from pathlib import Path

import pytest

from imgstrip import cli, core, fileio, fixtures, oracles, sandbox
from imgstrip import inspect as inspector
from imgstrip.cli import main
from imgstrip.core import StripError
from imgstrip.inspect import inspect_bytes
from imgstrip.web import server as web

from test_web import Client

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def app(tmp_path):
    session = web.Session(str(tmp_path / "work"))
    httpd, session = web.make_server(0, session)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield Client(httpd.server_address[1], session)
    httpd.shutdown()
    httpd.server_close()
    session.close()


@pytest.fixture
def settings(monkeypatch):
    """Leave the process-wide settings the command line changes as they were."""
    monkeypatch.setattr(cli, "_MAX_BYTES", cli._MAX_BYTES)
    monkeypatch.setattr(core, "_ACCEPT", core._ACCEPT)
    monkeypatch.setattr(sandbox, "_ACCEPTED", sandbox._ACCEPTED)
    monkeypatch.setattr(sandbox, "_REQUIRE", sandbox._REQUIRE)
    monkeypatch.setattr(sandbox, "_DEFAULT", sandbox._DEFAULT)
    yield
    monkeypatch.undo()
    sandbox.set_accepted(None)  # zygotes started under an --accept list would otherwise outlive the test


# --------------------------------------------------------------------------
# Web: Content-Length, Connection, who may connect
# --------------------------------------------------------------------------


def raw_request(app, headers: list[str], body: bytes = b"") -> tuple[int, dict[str, str], bytes]:
    """Send a request line by line as written (http.client would tidy the headers we want to get wrong)."""
    lines = [
        "POST /api/inspect HTTP/1.1",
        f"Host: 127.0.0.1:{app.port}",
        f"X-Imgstrip-Token: {app.session.token}",
        *headers,
        "",
        "",
    ]
    with socket.create_connection(("127.0.0.1", app.port), timeout=15) as sock:
        sock.sendall("\r\n".join(lines).encode("utf-8") + body)
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except (ConnectionResetError, TimeoutError):
                break
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks)
    head, _, payload = raw.partition(b"\r\n\r\n")
    status_line, *rest = head.decode("latin-1").split("\r\n")
    fields = {k.lower(): v for k, _, v in (r.partition(": ") for r in rest)}
    return int(status_line.split()[1]), fields, payload


@pytest.mark.parametrize(
    "value",
    ["+5", "5 5", "5_0", "-1", "0x10", "5.0", "٥", "9" * 19, "9" * 400, ""],
)
def test_content_length_must_be_plain_digits(app, value):
    status, _, _ = raw_request(app, [f"Content-Length: {value}"], b"hello")
    assert status == 400


def test_duplicate_content_length_is_refused(app):
    status, _, _ = raw_request(app, ["Content-Length: 5", "Content-Length: 5"], b"hello")
    assert status == 400


def test_a_huge_but_well_formed_content_length_is_too_large_not_an_allocation(app):
    status, _, _ = raw_request(app, ["Content-Length: " + "9" * 18])
    assert status == 413


def test_chunked_uploads_are_refused(app):
    status, _, _ = raw_request(app, ["Transfer-Encoding: chunked"], b"5\r\nhello\r\n0\r\n\r\n")
    assert status == 411


def test_an_ordinary_upload_still_works(app):
    data = fixtures.jpeg_everything()
    status, _, body = raw_request(app, [f"Content-Length: {len(data)}", "X-Filename: a.jpg"], data)
    assert status == 200 and json.loads(body)["format"] == "JPEG"


def test_every_response_closes_the_connection(app):
    res, _ = app.request("GET", "/api/info")
    assert res.status == 200 and res.getheader("Connection") == "close"
    res, _ = app.request("GET", "/api/info", auth=False)
    assert res.status in (401, 403) and res.getheader("Connection") == "close"
    res, _ = app.request("GET", "/no-such-page")
    assert res.status == 404 and res.getheader("Connection") == "close"


def test_the_peer_uid_of_a_real_connection_is_ours():
    if not os.path.exists("/proc/net/tcp"):
        pytest.skip("no /proc/net/tcp")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    try:
        conn, address = listener.accept()
        try:
            assert web.peer_uid(address, port) == os.getuid()
            assert web.peer_uid(("127.0.0.1", 1), port) is None  # no such connection
            assert web.peer_uid(("::1", 1, 0, 0), port) is None
            assert web.peer_uid("garbage", port) is None
        finally:
            conn.close()
    finally:
        client.close()
        listener.close()


def test_connections_of_other_users_are_refused(app, monkeypatch):
    calls = []
    monkeypatch.setattr(web, "peer_uid", lambda *a: calls.append(a) or os.getuid() + 1)
    with pytest.raises(OSError):  # closed without an answer (http.client.RemoteDisconnected is a ConnectionError)
        app.request("GET", "/api/info")
    assert calls


def test_a_client_that_hangs_up_leaves_no_stack_trace(capsys):
    server = web.Server.__new__(web.Server)
    for exc in (BrokenPipeError(), ConnectionResetError(), TimeoutError()):
        try:
            raise exc
        except OSError:
            server.handle_error(None, ("127.0.0.1", 1))
    assert capsys.readouterr().err == ""
    try:
        raise ValueError("a real bug")
    except ValueError:
        server.handle_error(None, ("127.0.0.1", 1))
    assert "a real bug" in capsys.readouterr().err


def test_verify_request_rules(monkeypatch):
    server = web.Server.__new__(web.Server)
    server.server_address = ("127.0.0.1", 4242)
    server.any_user = False
    monkeypatch.setattr(web, "peer_uid", lambda *a: os.getuid())
    assert server.verify_request(None, ("127.0.0.1", 1))
    monkeypatch.setattr(web, "peer_uid", lambda *a: os.getuid() + 7)
    assert not server.verify_request(None, ("127.0.0.1", 1))
    server.any_user = True  # --any-user
    assert server.verify_request(None, ("127.0.0.1", 1))
    server.any_user = False
    monkeypatch.setattr(web, "peer_uid", lambda *a: None)  # cannot be found out: the token and key are the gate
    assert server.verify_request(None, ("127.0.0.1", 1))


def test_the_flags_of_the_web_app_exist():
    out = subprocess.run(
        [sys.executable, "-m", "imgstrip.web", "--help"], capture_output=True, text=True, cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    ).stdout
    for flag in ("--any-user", "--require-sandbox", "--no-sandbox", "--c2pa-cert"):
        assert flag in out


# --------------------------------------------------------------------------
# Web: where it will save, what it reports, a failing sandbox
# --------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    return h


def test_refused_folders(home, tmp_path):
    for target in (
        "/etc",
        "/etc/cron.d",
        "/usr/local/bin",
        "/proc/self",
        str(home / ".ssh"),
        str(home / ".config" / "autostart"),
        str(home / ".gnupg" / "x"),
        str(home / "Library" / "LaunchAgents"),
        str(home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"),
        "/",
    ):
        assert web.refused_folder(target), target
    for target in (str(home / "Pictures" / "clean"), str(tmp_path / "out"), str(home)):
        assert web.refused_folder(target) is None, target


def test_a_link_to_a_hidden_folder_is_refused_by_its_real_location(home, tmp_path):
    (home / ".ssh").mkdir()
    link = tmp_path / "innocent"
    link.symlink_to(home / ".ssh", target_is_directory=True)
    assert web.refused_folder(str(link))
    (home / "ok").mkdir()
    fine = tmp_path / "fine"
    fine.symlink_to(home / "ok", target_is_directory=True)
    assert web.refused_folder(str(fine)) is None
    startup = tmp_path / "startup-link"
    startup.symlink_to("/etc", target_is_directory=True)
    assert web.refused_folder(str(startup))


@pytest.mark.parametrize("where", ["/etc/imgstrip-test", "~/.ssh", "~/.config/autostart", "~/Library/LaunchAgents"])
def test_save_refuses_places_that_start_programs(app, home, where):
    a = app.clean(fixtures.jpeg_everything())
    status, res = app.json("POST", "/api/save", {"ids": [a["id"]], "folder": where})
    assert status == 400 and "Cannot save into" in res["error"]
    assert not (home / ".ssh").exists() and not (home / ".config").exists() and not (home / "Library").exists()
    assert not Path("/etc/imgstrip-test").exists()


def test_save_through_a_link_to_a_hidden_folder_writes_nothing(app, home, tmp_path):
    (home / ".ssh").mkdir()
    link = tmp_path / "pictures"
    link.symlink_to(home / ".ssh", target_is_directory=True)
    a = app.clean(fixtures.jpeg_everything())
    status, _ = app.json("POST", "/api/save", {"ids": [a["id"]], "folder": str(link)})
    assert status == 400
    assert list((home / ".ssh").iterdir()) == []


def test_save_into_an_ordinary_folder_still_works(app, home):
    a = app.clean(fixtures.jpeg_everything(), "x.jpg")
    status, res = app.json("POST", "/api/save", {"ids": [a["id"]], "folder": "~/Pictures/clean"})
    assert status == 200 and res["files"][0]["ok"]
    assert (home / "Pictures" / "clean" / "x.clean.jpg").exists()


def test_info_reports_the_sandbox(app):
    status, info = app.json("GET", "/api/info")
    assert status == 200 and info["version"]
    box = info["sandbox"]
    assert isinstance(box, dict) and {"landlock", "seccomp", "full"} <= set(box)


def test_inspect_says_so_when_the_sandbox_cannot_read_the_file(app, monkeypatch):
    def boom(data, sandboxed=None):
        raise StripError("the decoder sandbox is not available")

    monkeypatch.setattr(web, "inspect_isolated", boom)
    res, body = app.upload("/api/inspect", fixtures.jpeg_everything())
    assert res.status == 422 and "sandbox" in json.loads(body)["error"]


def make_identity_files(tmp_path) -> Path:
    pytest.importorskip("c2pa")
    pytest.importorskip("cryptography")
    ids = tmp_path / "ids"
    assert main(["--c2pa-test-identity", str(ids)]) == 0
    return ids


def startup_args(ids: Path, **kw) -> argparse.Namespace:
    base = dict(
        c2pa_cert=str(ids / "test-signer-chain.pem"),
        c2pa_key=str(ids / "test-signer-key.pem"),
        c2pa_passphrase_file=None,
        c2pa_trust_anchors=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_startup_identity_is_loaded_without_the_browser(tmp_path):
    ids = make_identity_files(tmp_path)
    session = web.Session(str(tmp_path / "work"))
    try:
        web.load_startup_identity(session, startup_args(ids), argparse.ArgumentParser())
        assert session.signing_state()[0] is not None
    finally:
        session.close()


@pytest.mark.parametrize(
    "change",
    [
        dict(c2pa_cert=None),
        dict(c2pa_cert="/does/not/exist"),
        dict(c2pa_key="/does/not/exist"),
        dict(c2pa_trust_anchors="/does/not/exist"),
        dict(c2pa_passphrase_file="/does/not/exist"),
    ],
)
def test_startup_identity_errors_exit_with_2(tmp_path, change):
    ids = make_identity_files(tmp_path)
    session = web.Session(str(tmp_path / "work"))
    try:
        with pytest.raises(SystemExit) as exc:
            web.load_startup_identity(session, startup_args(ids, **change), argparse.ArgumentParser(prog="t"))
        assert exc.value.code == 2
        assert session.signing_state()[0] is None
    finally:
        session.close()


# --------------------------------------------------------------------------
# The inspector's budgets
# --------------------------------------------------------------------------


def png_with(chunks: bytes) -> bytes:
    return fixtures._png_insert_before_idat(fixtures.png_everything(), chunks)


MANY = b"".join(fixtures._png_chunk(b"teXt", b"k\x00v") for _ in range(2000))


def test_a_file_made_of_very_many_small_chunks_runs_out_of_time(monkeypatch):
    data = png_with(MANY)
    assert inspect_bytes(data).error is None  # within the normal budget
    monkeypatch.setattr(inspector, "_BASE_SECONDS", 0.0)
    monkeypatch.setattr(inspector, "_SECONDS_PER_MB", 0.0)
    report = inspect_bytes(data)
    assert report.error and "too long" in report.error


def test_the_budget_does_not_leak_between_files(monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(inspector, "_BASE_SECONDS", 0.0)
        m.setattr(inspector, "_SECONDS_PER_MB", 0.0)
        assert inspect_bytes(png_with(MANY)).error
    assert inspect_bytes(png_with(MANY)).error is None
    assert inspect_bytes(fixtures.jpeg_everything()).error is None


def test_compressed_text_is_counted_as_it_is_inflated(monkeypatch):
    bomb = fixtures._png_chunk(b"zTXt", b"k\x00\x00" + zlib.compress(b"\x00" * (200 << 10), 9))
    data = png_with(bomb)
    assert inspect_bytes(data).error is None
    monkeypatch.setattr(inspector, "_TEXT_BUDGET", 50 << 10)
    report = inspect_bytes(data)
    assert report.error and "too much" in report.error


# --------------------------------------------------------------------------
# What a worker answers is checked
# --------------------------------------------------------------------------

SAMPLES = ["jpeg_everything", "png_everything", "png_palette_tricks", "png_hidden_under_alpha"]
FULL = sandbox.status().get("full")


@pytest.mark.skipif(not FULL, reason="no complete sandbox here")
@pytest.mark.parametrize("name", SAMPLES)
def test_the_sandboxed_inspector_agrees_with_the_one_in_this_process(name):
    data = getattr(fixtures, name)()
    here, there = inspect_bytes(data), sandbox.inspect(data)
    assert (there.format, there.error) == (here.format, here.error)
    assert [(f.category, f.label, f.detail) for f in there.findings] == [
        (f.category, f.label, f.detail) for f in here.findings
    ]


def report_with(**changes):
    from imgstrip.inspect import Finding, Report

    report = Report(format="PNG", findings=[Finding("metadata", "x", "y")], error=None, info={})
    for k, v in changes.items():
        setattr(report, k, v)
    return report


def test_a_good_report_is_accepted():
    assert sandbox._accept_report(report_with()).format == "PNG"


@pytest.mark.parametrize(
    "changes",
    [
        dict(format="EXE"),
        dict(findings="none"),
        dict(findings=[("metadata", "x", "y")]),
        dict(error=5),
        dict(error="x" * (sandbox._MAX_TEXT + 1)),
        dict(info={"anything": 1}),
        dict(info={"size": (1 << 40, 5)}),
        dict(info={"size": "big"}),
        dict(info=[]),
    ],
)
def test_a_tampered_report_is_refused(changes):
    with pytest.raises(StripError):
        sandbox._validated(sandbox._accept_report, report_with(**changes))


def test_something_that_is_not_a_report_is_refused():
    for value in (None, {"format": "PNG"}, "report", 5, [report_with()]):
        with pytest.raises(StripError):
            sandbox._validated(sandbox._accept_report, value)


@pytest.mark.parametrize(
    "value",
    [None, [], {}, {"credential": 5}, {"credential": {}}, {"credential": {"present": "yes"}}, {"a": 1, "b": 2}],
)
def test_a_tampered_credential_is_refused(value):
    with pytest.raises(StripError):
        sandbox._validated(sandbox._accept_credential, value)


def test_an_error_text_from_the_worker_becomes_the_matching_exception():
    from imgstrip import credentials

    with pytest.raises(credentials.CredentialError):
        sandbox._accept_credential({"error": "the manifest is damaged"})
    with pytest.raises(credentials.CredentialsUnavailable):
        sandbox._accept_credential({"unavailable": "not installed"})


@pytest.mark.skipif(not FULL, reason="no complete sandbox here")
def test_credentials_read_in_the_sandbox_equal_those_read_here():
    pytest.importorskip("c2pa")
    pytest.importorskip("cryptography")
    from imgstrip import credentials

    for name in ("jpeg_everything", "png_everything"):
        data = getattr(fixtures, name)()
        assert credentials.read_credential_isolated(data, sandboxed=True) == credentials.read_credential(data)


# --------------------------------------------------------------------------
# Command line: limits, kinds of file, --accept, --backup-suffix
# --------------------------------------------------------------------------


@pytest.fixture
def jpg(tmp_path):
    p = tmp_path / "photo.jpg"
    p.write_bytes(fixtures.jpeg_everything())
    return p


def failed_messages(capsys) -> dict[str, str]:
    report = json.loads(capsys.readouterr().out)
    return {Path(f["file"]).name: f["message"] for f in report["files"] if f["status"] == "failed"}


def test_max_bytes_refuses_a_large_file(jpg, settings, capsys):
    size = jpg.stat().st_size
    assert main(["--check", "--json", "--max-bytes", str(size - 1), str(jpg)]) == 1
    assert "max-bytes" in failed_messages(capsys)["photo.jpg"]
    assert main(["--check", "--json", "--max-bytes", str(size), str(jpg)]) == 3  # 3: findings


def test_max_bytes_applies_to_standard_input(jpg, settings, monkeypatch, capsys):
    import io

    class FakeStdin:
        buffer = io.BytesIO(jpg.read_bytes())

    monkeypatch.setattr(sys, "stdin", FakeStdin)
    assert main(["--check", "--json", "--max-bytes", "100", "-"]) == 1
    assert "max-bytes" in json.loads(capsys.readouterr().out)["files"][0]["message"]


@pytest.mark.parametrize("value", ["0", "-5", "abc", "1.5"])
def test_max_bytes_must_be_a_positive_whole_number(jpg, settings, value):
    with pytest.raises(SystemExit) as exc:
        main(["--check", "--max-bytes", value, str(jpg)])
    assert exc.value.code == 2


def test_a_directory_is_not_a_file(tmp_path, settings, capsys):
    assert main(["--check", "--json", str(tmp_path)]) == 1
    assert "directory" in failed_messages(capsys)[tmp_path.name]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes")
def test_a_named_pipe_is_refused_without_waiting_for_a_writer(tmp_path, settings, capsys):
    fifo = tmp_path / "pipe.jpg"
    os.mkfifo(fifo)
    assert main(["--check", "--json", str(fifo)]) == 1
    assert "not a regular file" in failed_messages(capsys)["pipe.jpg"]


@pytest.mark.skipif(not os.path.exists("/dev/zero"), reason="no devices")
def test_a_device_is_refused(settings):
    with pytest.raises(OSError) as exc:
        cli._read("/dev/zero")
    assert exc.value.errno == errno.EINVAL


def test_accept_lets_listed_formats_through_and_refuses_the_rest(tmp_path, settings, capsys):
    jpg_file = tmp_path / "a.jpg"
    jpg_file.write_bytes(fixtures.jpeg_everything())
    png_file = tmp_path / "b.png"
    png_file.write_bytes(fixtures.png_everything())
    assert main(["--check", "--json", "--accept", "jpeg", str(jpg_file), str(png_file)]) == 1
    messages = failed_messages(capsys)
    assert set(messages) == {"b.png"} and "not accepted" in messages["b.png"]


def test_accept_understands_the_usual_names_and_case(tmp_path, settings):
    for spelling in ("JPG", "jpeg", "Jpeg,PNG", " jpg , png "):
        p = tmp_path / "a.jpg"
        p.write_bytes(fixtures.jpeg_everything())
        assert main(["--check", "--accept", spelling, str(p)]) == 3, spelling  # 3: findings, not refused


@pytest.mark.parametrize("value", ["exe", "jpeg,exe", "", ",", "  "])
def test_accept_with_an_unknown_format_is_a_usage_error(jpg, settings, value):
    with pytest.raises(SystemExit) as exc:
        main(["--check", "--accept", value, str(jpg)])
    assert exc.value.code == 2


def test_accept_is_also_applied_inside_the_sandbox(settings):
    core.set_accepted(["JPEG"])
    sandbox.set_accepted(["JPEG"])
    with pytest.raises(StripError):
        core.check_accepted(fixtures.png_everything())
    if FULL:
        with pytest.raises(StripError):
            sandbox.frames(fixtures.png_everything(), 1_000_000)
        assert sandbox.frames(fixtures.jpeg_everything(), 1_000_000)


def test_in_place_with_a_backup_keeps_the_original_bytes(jpg, settings):
    original = jpg.read_bytes()
    assert main(["--in-place", "--backup-suffix", ".orig", str(jpg)]) == 0
    backup = jpg.with_name("photo.jpg.orig")
    assert backup.read_bytes() == original
    assert jpg.read_bytes() != original and inspect_bytes(jpg.read_bytes()).clean


def test_a_backup_suffix_needs_in_place(jpg, settings):
    with pytest.raises(SystemExit) as exc:
        main(["--backup-suffix", ".orig", str(jpg)])
    assert exc.value.code == 2


def test_a_backup_suffix_is_not_a_path(jpg, settings):
    with pytest.raises(SystemExit) as exc:
        main(["--in-place", "--backup-suffix", "/../x", str(jpg)])
    assert exc.value.code == 2


def test_an_existing_backup_is_never_overwritten_and_the_original_is_left(jpg, settings, capsys):
    original = jpg.read_bytes()
    backup = jpg.with_name("photo.jpg.orig")
    backup.write_bytes(b"precious")
    assert main(["--in-place", "--backup-suffix", ".orig", str(jpg)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert backup.read_bytes() == b"precious" and jpg.read_bytes() == original


def test_a_failed_read_back_leaves_the_original_and_no_temporary_file(jpg, settings, monkeypatch):
    original = jpg.read_bytes()
    monkeypatch.setattr(fileio, "_same_content", lambda tmp, data: False)
    assert main(["--in-place", str(jpg)]) == 1
    assert jpg.read_bytes() == original
    assert [p.name for p in jpg.parent.iterdir()] == ["photo.jpg"]


def test_replace_atomic_verifies_and_cleans_up(tmp_path, monkeypatch):
    dest = tmp_path / "f.bin"
    dest.write_bytes(b"old")
    fileio.replace_atomic(dest, b"new", verify=True)
    assert dest.read_bytes() == b"new"
    monkeypatch.setattr(fileio, "_same_content", lambda tmp, data: False)
    with pytest.raises(OSError):
        fileio.replace_atomic(dest, b"newer", verify=True)
    assert dest.read_bytes() == b"new" and [p.name for p in tmp_path.iterdir()] == ["f.bin"]


def test_replace_atomic_keeps_the_mode_and_owner_of_the_file_it_replaces(tmp_path):
    dest = tmp_path / "f.bin"
    dest.write_bytes(b"old")
    dest.chmod(0o640)
    fileio.replace_atomic(dest, b"new", mode=0o640, like=dest)
    st = dest.stat()
    assert stat.S_IMODE(st.st_mode) == 0o640 and st.st_uid == os.getuid()
    assert dest.read_bytes() == b"new"


def test_replace_atomic_copies_from_a_path(tmp_path):
    src, dest = tmp_path / "s", tmp_path / "d"
    src.write_bytes(b"x" * 3_000_000)
    fileio.replace_atomic(dest, src, verify=True)
    assert dest.read_bytes() == src.read_bytes()


def test_no_sandbox_and_require_sandbox_contradict_each_other(jpg, settings, monkeypatch):
    with pytest.raises(SystemExit) as exc:
        main(["--no-sandbox", "--require-sandbox", str(jpg)])
    assert exc.value.code == 2
    monkeypatch.setenv("IMGSTRIP_REQUIRE_SANDBOX", "1")
    with pytest.raises(SystemExit) as exc:
        main(["--no-sandbox", str(jpg)])
    assert exc.value.code == 2
    assert not jpg.with_name("photo.clean.jpg").exists()


def test_the_web_app_refuses_the_same_contradiction():
    done = subprocess.run(
        [sys.executable, "-m", "imgstrip.web", "--no-sandbox", "--require-sandbox", "--no-browser"],
        capture_output=True, text=True, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, timeout=60,
    )
    assert done.returncode == 2 and "contradict" in done.stderr


def test_no_sandbox_still_works(jpg, settings):
    assert main(["--no-sandbox", str(jpg)]) == 0
    assert inspect_bytes(jpg.with_name("photo.clean.jpg").read_bytes()).clean


# --------------------------------------------------------------------------
# The checking programs and the process
# --------------------------------------------------------------------------


def test_an_independent_reader_gets_no_secrets(monkeypatch):
    monkeypatch.setenv("IMGSTRIP_C2PA_PASSPHRASE", "hunter2")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    env = oracles.clean_environment()
    assert "IMGSTRIP_C2PA_PASSPHRASE" not in env and "AWS_SECRET_ACCESS_KEY" not in env


def test_oracles_run_without_stdin_and_with_the_clean_environment(monkeypatch):
    monkeypatch.setenv("IMGSTRIP_C2PA_PASSPHRASE", "hunter2")
    out = oracles._run([sys.executable, "-c", "import os,sys;print(sorted(os.environ));print(repr(sys.stdin.read()))"])
    assert b"IMGSTRIP_C2PA_PASSPHRASE" not in out and out.rstrip().endswith(b"''")


def test_a_program_in_the_current_directory_is_not_used(tmp_path, monkeypatch):
    prog = tmp_path / "exiftool"
    prog.write_text("#!/bin/sh\necho hi\n")
    prog.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", f".{os.pathsep}{tmp_path}")
    assert oracles.which("exiftool") is None


def run_py(code: str, cwd) -> subprocess.CompletedProcess:
    # Not -I: it would ignore PYTHONPATH and import whatever imgstrip is installed.  The working directory is an empty
    # one, so nothing in it can shadow a module.
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=cwd,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": os.environ.get("PATH", "")},
    )


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc")
def test_harden_process_closes_core_dumps_and_ptrace(tmp_path):
    done = run_py(
        "from imgstrip import sandbox; import resource\n"
        "sandbox.harden_process()\n"
        "print(resource.getrlimit(resource.RLIMIT_CORE))\n"
        "import ctypes; print(ctypes.CDLL(None).prctl(3, 0, 0, 0, 0))\n",
        tmp_path,
    )
    assert done.returncode == 0, done.stderr
    lines = done.stdout.split("\n")
    assert lines[0] == "(0, 0)" and lines[1] == "0"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc")
def test_loading_a_signing_identity_hardens_the_process(tmp_path):
    ids = make_identity_files(tmp_path)
    done = run_py(
        "import argparse\n"
        "from imgstrip import cli\n"
        f"cli.load_signing_identity(argparse.Namespace(c2pa_cert={str(ids / 'test-signer-chain.pem')!r}, "
        f"c2pa_key={str(ids / 'test-signer-key.pem')!r}, c2pa_passphrase_file=None))\n"
        "import ctypes; print(ctypes.CDLL(None).prctl(3, 0, 0, 0, 0))\n",
        tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "0"


# --------------------------------------------------------------------------
# Release workflow
# --------------------------------------------------------------------------

WORKFLOWS = ROOT / ".github" / "workflows"


@pytest.mark.skipif(not WORKFLOWS.is_dir(), reason="not a source checkout")
def test_the_publishing_action_is_pinned_to_a_commit_and_the_builder_to_a_version():
    text = (WORKFLOWS / "publish.yml").read_text()
    assert re.search(r"uses: pypa/gh-action-pypi-publish@[0-9a-f]{40}\b", text)
    assert re.search(r"pip install build==\d+\.\d+\.\d+\b", text)
    assert "attestations: true" in text
    assert "id-token: write" in text


@pytest.mark.skipif(not WORKFLOWS.is_dir(), reason="not a source checkout")
def test_the_weekly_audit_only_reports():
    text = (WORKFLOWS / "audit.yml").read_text()
    assert "schedule:" in text and "pip-audit" in text
    assert "contents: read" in text and "contents: write" not in text
    assert not (ROOT / ".github" / "dependabot.yml").exists()
