"""Hardening of 2.0.6: the findings of a security test of 2.0.5, each with the case that showed it.

* the record: a repeated JSON key, fields outside the signature, text that reads differently from its bytes, keys of no
  value, what the record keeps of the metadata it removed, and the protection of a passphrase-protected key;
* the inspector: counts in a HEIF or AVIF file that made it loop for minutes;
* the sandbox: limits, priority and CPU placement of other processes, caps a worker could lift, and a decoder that
  outlived the program that started it;
* the web app: folders in any home and credential folders, a link swapped in while saving, a second Host header, the
  launch key on a command line.
"""

from __future__ import annotations

import base64
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from imgstrip import fixtures, record, sandbox
from imgstrip import inspect as inspector
from imgstrip.cli import main
from imgstrip.core import StripError, strip_image
from imgstrip.web import server as web

from test_confinement import ALLOW, EPERM, PID, filt, full_confinement  # noqa: F401  (fixtures)
from test_record import make_body
from test_web import app  # noqa: F401  (fixture)

linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
needs_crypto = pytest.mark.skipif(not record.available(), reason="needs the cryptography library")


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def keypair():
    if not record.available():
        pytest.skip("needs the cryptography library")
    private, public, _ = record.generate_key()
    return record.load_signer(private), record.load_public(public)


def signed_text(keypair, **kw) -> str:
    return record.seal(make_body(**kw), keypair[0]).decode()


@needs_crypto
@pytest.mark.parametrize(
    "line, forged",
    [
        ('"operator": "A. Examiner"', '"operator": "Det. Forged"'),  # in the record
        ('"sha256": "' + "a" * 64 + '"', '"sha256": "' + "b" * 64 + '"'),  # inside input
        ('"algorithm": "ed25519"', '"algorithm": "ed25519"'),  # in the signature block
        ('"version": 1', '"version": 2'),
    ],
)
def test_a_repeated_key_never_verifies(keypair, line, forged):
    text = signed_text(keypair)
    assert text.count(line) >= 1
    shadowed = text.replace(line, f"{forged}, {line}", 1)  # the first value is the forged one, the last the signed one
    assert json.loads(shadowed)  # plain json.loads is happy with it
    verdict = record.verify(shadowed.encode(), keypair[1])
    assert verdict.status == record.INVALID and "repeats the key" in verdict.problems[0]


@needs_crypto
def test_a_record_that_repeats_nothing_still_verifies_as_trusted(keypair):
    assert record.verify(signed_text(keypair).encode(), keypair[1]).status == record.TRUSTED


@needs_crypto
def test_fields_outside_the_signed_record_are_refused(keypair):
    doc = json.loads(signed_text(keypair))
    doc["verdict"] = "TRUSTED BY COURT"
    verdict = record.verify(json.dumps(doc).encode(), keypair[1])
    assert verdict.status == record.INVALID and "outside the signed record" in verdict.problems[0]
    doc = json.loads(signed_text(keypair))
    doc["signature"]["comment"] = "approved"
    assert record.verify(json.dumps(doc).encode(), keypair[1]).status == record.INVALID
    doc = json.loads(signed_text(keypair))
    del doc["signature"]["algorithm"]
    assert record.verify(json.dumps(doc).encode(), keypair[1]).status == record.INVALID


@needs_crypto
def test_a_key_of_small_order_is_refused_whatever_it_signs():
    for raw in (b"\x01" + b"\x00" * 31, b"\x00" * 32):
        body = make_body()
        doc = {
            "record": body,
            "signature": {
                "algorithm": "ed25519",
                "key_id": record.key_id_of(raw),
                "public_key": base64.b64encode(raw).decode(),
                "value": base64.b64encode(b"\x01" + b"\x00" * 63).decode(),
            },
        }
        verdict = record.verify(json.dumps(doc).encode())
        assert verdict.status == record.INVALID and "no value" in verdict.problems[0]


@pytest.mark.parametrize("char", ["‮", "⁦", "‎", "​", "⁠", "﻿", "؜"])
def test_text_direction_marks_and_zero_width_characters_are_refused_in_case_and_operator(char):
    for what in ("case", "operator"):
        with pytest.raises(record.RecordError, match="text-direction"):
            make_body(**{what: f"Smith{char}Jones"})


def test_scripts_that_need_the_joiners_still_work():
    assert make_body(operator="می‌خواهم")["operator"] == "می‌خواهم"
    assert make_body(case="CASE-7 / 2026")["case"] == "CASE-7 / 2026"


@needs_crypto
def test_a_record_made_elsewhere_cannot_put_controls_on_a_terminal_or_page(keypair):
    body = make_body()
    body["case"] = "\x1b[2Jcase‮gpj"
    body["operator"] = "op​erator"
    body["input"]["name"] = "a\x07b.jpg"
    verdict = record.verify(record.seal(body, keypair[0]), keypair[1])
    shown = verdict.as_dict()
    text = json.dumps(shown, ensure_ascii=False)
    assert verdict.status == record.TRUSTED
    assert "\x1b" not in text and "‮" not in text and "​" not in text and "\x07" not in text
    assert "�" in shown["case"]


def test_the_record_keeps_what_was_removed_by_name_and_never_by_value():
    facts = record.result_facts(
        {
            "removed": [
                {"category": "metadata", "label": "GPS location", "detail": "37.774917, -122.419389"},
                {"category": "metadata", "label": "EXIF", "detail": "make=Canon, model=Canon EOS R5"},
            ]
        }
    )
    assert facts["removed"] == [
        {"category": "metadata", "label": "GPS location"},
        {"category": "metadata", "label": "EXIF"},
    ]
    assert "37.77" not in json.dumps(facts) and "Canon" not in json.dumps(facts)


def test_the_names_of_icc_profiles_stay_out_of_the_actions_and_warnings():
    facts = record.result_facts(
        {
            "actions": [
                "converted colours from ICC profile 'Display P3 of Jane's laptop' (RGB) to sRGB",
                "removed ICC profile 'Jane's scanner' without converting",
                "replaced ICC profile 'X' with a rebuilt anonymous profile",
                "re-encoded JPEG at quality 95",
            ],
            "warnings": ["ICC profile 'Odd (name)' removed (boom); colours may shift."],
        }
    )
    assert facts["actions"] == [
        "converted colours from ICC profile (RGB) to sRGB",
        "removed ICC profile without converting",
        "replaced ICC profile with a rebuilt anonymous profile",
        "re-encoded JPEG at quality 95",
    ]
    assert facts["warnings"] == ["ICC profile removed (boom); colours may shift."]


def test_a_record_written_by_the_command_line_holds_none_of_the_removed_text(tmp_path):
    photo = tmp_path / "in.jpg"
    photo.write_bytes(fixtures.jpeg_everything())
    out, rec = tmp_path / "out", tmp_path / "rec"
    out.mkdir()
    assert main([str(photo), "-o", str(out), "--record", str(rec)]) == 0
    files = list(rec.glob("*.imgstrip-record.json"))
    assert len(files) == 1
    text = files[0].read_text()
    assert "CANARY" not in text and "Jane" not in text  # nothing the original said about itself, profile names included
    assert json.loads(text)["record"]["result"]["removed"], "the record still says what kind of data was removed"


# --------------------------------------------------------------------------
# The key
# --------------------------------------------------------------------------

LABEL = "IMGSTRIP ENCRYPTED PRIVATE KEY"


def armor(head: dict) -> bytes:
    body = base64.b64encode(json.dumps(head, sort_keys=True).encode()).decode()
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return ("\n".join([f"-----BEGIN {LABEL}-----", *lines, f"-----END {LABEL}-----"]) + "\n").encode()


def header_of(pem: bytes) -> dict:
    return json.loads(base64.b64decode(b"".join(pem.splitlines()[1:-1])))


@needs_crypto
def test_a_protected_key_is_stretched_with_scrypt_and_opens_only_with_its_passphrase():
    private, public, key_id = record.generate_key(b"correct horse")
    assert private.startswith(b"-----BEGIN " + LABEL.encode())
    head = header_of(private)
    assert head["kdf"] == "scrypt" and head["n"] >= 1 << 17 and head["cipher"] == "chacha20poly1305"
    assert record.load_signer(private, b"correct horse").key_id == key_id
    with pytest.raises(record.PassphraseRequired, match="wrong passphrase"):
        record.load_signer(private, b"correct horsf")
    with pytest.raises(record.PassphraseRequired, match="protected"):
        record.load_signer(private)


@needs_crypto
def test_the_settings_of_a_protected_key_are_authenticated_and_capped():
    private, _, _ = record.generate_key(b"pw")
    head = header_of(private)
    weaker = {
        **head,
        "n": head["n"] // 2,
    }  # an attacker who could edit the file lowers the cost: the key no longer opens
    with pytest.raises(record.PassphraseRequired):
        record.load_signer(armor(weaker), b"pw")
    for bad in ({**head, "n": 1 << 30}, {**head, "n": 3}, {**head, "r": 64}, {**head, "p": 99}, {**head, "kdf": "md5"}):
        with pytest.raises(record.RecordError, match="not a readable"):
            record.load_signer(armor(bad), b"pw")
    with pytest.raises(record.RecordError):
        record.load_signer(f"-----BEGIN {LABEL}-----\n!!!!\n-----END {LABEL}-----\n".encode(), b"pw")


@needs_crypto
def test_keys_made_by_earlier_versions_still_open():
    from cryptography.hazmat.primitives import serialization as s
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    old = key.private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.BestAvailableEncryption(b"x"))
    assert b"ENCRYPTED PRIVATE KEY" in old
    assert record.load_signer(old, b"x").public_raw == record._raw_public(key.public_key())
    plain, _, kid = record.generate_key()
    assert plain.startswith(b"-----BEGIN PRIVATE KEY-----")
    assert record.load_signer(plain, b"a stray passphrase").key_id == kid


# --------------------------------------------------------------------------
# The inspector
# --------------------------------------------------------------------------


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def heif_with(iloc: bytes) -> bytes:
    ftyp = box(b"ftyp", b"heic" + b"\0\0\0\0" + b"heic")
    hdlr = box(b"hdlr", b"\0" * 8 + b"pict" + b"\0" * 13)
    pitm = box(b"pitm", b"\0\0\0\0" + struct.pack(">H", 1))
    infe = box(b"infe", b"\2\0\0\0" + struct.pack(">HH4s", 1, 0, b"hvc1") + b"\0")
    iinf = box(b"iinf", b"\0\0\0\0\0\1" + infe)
    return ftyp + box(b"meta", b"\0\0\0\0" + hdlr + pitm + iinf + box(b"iloc", iloc))


HOSTILE_ILOC = {
    "version 2, four billion items": b"\x02\0\0\0\x44\x00" + struct.pack(">I", 0xFFFFFFFF),
    "version 1, 65535 items": b"\x01\0\0\0\x44\x00" + struct.pack(">H", 0xFFFF),
    "one item with 65535 extents": b"\x01\0\0\0\x44\x00" + struct.pack(">H", 1) + struct.pack(">HHHH", 1, 0, 0, 0xFFFF),
    "extents that take no bytes": b"\x01\0\0\0\x00\x00" + struct.pack(">H", 1) + struct.pack(">HHHH", 1, 0, 0, 0xFFFF),
    "table cut short": b"\x01\0\0\0\x44\x00" + struct.pack(">H", 1) + b"\0\1",
}


@pytest.mark.parametrize("name", list(HOSTILE_ILOC))
def test_a_hostile_item_location_table_is_refused_at_once(name):
    data = heif_with(HOSTILE_ILOC[name])
    start = time.monotonic()
    report = inspector.inspect_bytes(data)
    assert time.monotonic() - start < 2
    assert report.error and report.error.startswith("malformed HEIF")


def test_an_item_location_table_that_fits_its_box_is_read():
    one = b"\x01\0\0\0\x44\x00" + struct.pack(">H", 1) + struct.pack(">HHHH", 1, 0, 0, 1) + struct.pack(">II", 100, 50)
    report = inspector.inspect_bytes(heif_with(one))
    assert not (report.error or "").startswith("malformed HEIF: the item location")


REPRO = Path(__file__).parent / "regressions"


@pytest.mark.parametrize("name", ["iloc_loop_avif.bin", "iloc_loop_heic.bin"])
def test_the_files_that_held_the_inspector_for_minutes_are_refused_at_once(name):
    data = (REPRO / name).read_bytes()
    start = time.monotonic()
    assert inspector.inspect_bytes(data).error
    with pytest.raises(StripError):
        strip_image(data)
    assert time.monotonic() - start < 20


def test_the_extents_of_one_item_cannot_add_up_to_more_than_the_file():
    d = b"x" * 100
    ctx = {"iloc": {1: (0, [(0, 100)] * 50)}}
    with pytest.raises(inspector._Malformed):
        inspector._heif_item_data(d, ctx, 1)
    assert inspector._heif_item_data(d, {"iloc": {1: (0, [(0, 60), (60, 40)])}}, 1) == d


# --------------------------------------------------------------------------
# The sandbox
# --------------------------------------------------------------------------

OWN_PID = ("sched_setparam", "sched_setscheduler", "sched_setaffinity", "sched_setattr", "migrate_pages", "move_pages")


def test_other_processes_cannot_be_given_other_limits_priority_or_cpus(filt):  # noqa: F811
    for name in OWN_PID:
        assert filt(name, 0) == ALLOW, name  # the caller
        assert filt(name, PID + 1) == EPERM, name
        assert filt(name, 1) == EPERM, name
    for name, process in (("setpriority", 0), ("ioprio_set", 1)):  # argument 1 names the process (2.0.7: and 0 the kind)
        assert filt(name, process, 0, 5) == ALLOW
        assert filt(name, process, PID + 1, 5) == EPERM
        assert filt(name, 2, 1, 5) == EPERM
    assert filt("prlimit64", 0, 7) == ALLOW  # setrlimit, on itself
    assert filt("prlimit64", PID, 7) == ALLOW
    assert filt("prlimit64", PID + 1, 7) == EPERM
    assert filt("prlimit64", 1, 7) == EPERM


def test_the_calls_a_decoder_still_needs_for_itself_pass(filt):  # noqa: F811
    assert filt("prlimit64", 0, 7, 0, 0) == ALLOW
    assert filt("sched_setaffinity", 0, 8, 0) == ALLOW


def test_a_confined_worker_cannot_touch_other_processes_but_can_manage_itself(tmp_path, full_confinement):  # noqa: F811
    victim = tmp_path / "victim"
    victim.write_text("keep")
    limits = sandbox.probe_limits(str(tmp_path), str(victim))
    for what in ("other_limits", "other_priority", "other_cpus"):
        assert limits[what] in ("not tried",) or limits[what].startswith("no"), (what, limits[what])
    assert limits["other_limits"].startswith("no")
    assert limits["own_limit"] == "yes" and limits["own_priority"] in ("yes", "not tried")
    soft, hard = limits["cpu"]
    assert soft == hard != -1, limits["cpu"]  # the worker cannot lift its CPU cap


@linux
def test_a_worker_has_equal_soft_and_hard_limits_for_cpu_and_memory():
    import resource

    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:  # a worker
        status = 1
        try:
            os.close(read)
            sandbox._restrict(sandbox._read_roots(), 256 << 20)
            os.write(
                write,
                json.dumps(
                    {"cpu": resource.getrlimit(resource.RLIMIT_CPU), "data": resource.getrlimit(resource.RLIMIT_DATA)}
                ).encode(),
            )
            status = 0
        finally:
            os._exit(status)
    os.close(write)
    raw = b""
    while chunk := os.read(read, 4096):
        raw += chunk
    os.close(read)
    assert os.waitpid(pid, 0)[1] == 0
    limits = json.loads(raw)
    for name in ("cpu", "data"):
        soft, hard = limits[name]
        assert soft == hard != resource.RLIM_INFINITY, (name, limits[name])


def test_a_small_file_is_not_given_the_time_of_the_largest_picture():
    assert sandbox._deadline(150_000_000, 1_700) < 150
    assert sandbox._deadline(150_000_000, 1_700) < sandbox._deadline(150_000_000, 10 << 20)
    assert sandbox._deadline(1_000_000, 10**9) <= sandbox._TIMEOUT


def _state(pid: int):
    """(state, parent) of a process, or None when it is gone."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except OSError:
        return None
    return None if fields[0] in ("Z", "X") else (fields[0], int(fields[1]))


def _children(pid: int) -> list[int]:
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and (state := _state(int(entry.name))) and state[1] == pid:
            found.append(int(entry.name))
    return found


@linux
def test_a_decoder_stops_when_the_program_that_started_it_is_killed():
    script = textwrap.dedent(
        """
        import threading, time
        from imgstrip import sandbox

        def job():
            try:
                sandbox._call("_hang", b"", {}, 1 << 16, timeout=600)
            except Exception:
                pass

        threading.Thread(target=job, daemon=True).start()
        time.sleep(600)
        """
    )
    main_process = subprocess.Popen([sys.executable, "-c", script])
    try:
        zygote = worker = None
        for _ in range(200):  # the zygote starts, then forks the worker that hangs
            kids = _children(main_process.pid)
            if kids and (grand := _children(kids[0])):
                zygote, worker = kids[0], grand[0]
                break
            time.sleep(0.1)
        assert zygote and worker, "the job never started"
        os.kill(main_process.pid, signal.SIGKILL)  # not a clean exit: nothing gets to tidy up
        main_process.wait()
        for _ in range(100):
            if _state(zygote) is None and _state(worker) is None:
                break
            time.sleep(0.1)
        assert _state(zygote) is None, "the zygote is still running"
        assert _state(worker) is None, "the worker is still running"
    finally:
        if main_process.poll() is None:
            main_process.kill()
        for pid in (zygote, worker):
            if pid and _state(pid):
                os.kill(pid, signal.SIGKILL)


# --------------------------------------------------------------------------
# The web app
# --------------------------------------------------------------------------


def test_credential_folders_are_refused_wherever_they_are(tmp_path):
    for name in (".ssh", ".gnupg", ".aws", ".kube"):
        assert web.refused_folder(str(tmp_path / "elsewhere" / name / "x")), name
    assert web.refused_folder(str(tmp_path / "elsewhere" / "Pictures")) is None


def test_hidden_folders_in_every_home_the_system_knows_are_refused(tmp_path, monkeypatch):
    import pwd

    alice = tmp_path / "srv" / "alice"
    alice.mkdir(parents=True)
    entry = pwd.struct_passwd(("alice", "x", 4242, 4242, "", str(alice), "/bin/sh"))
    monkeypatch.setattr(pwd, "getpwall", lambda: [entry])
    monkeypatch.setenv("HOME", str(tmp_path / "somewhere-else"))  # as when HOME is set differently from the account's
    assert web.refused_folder(str(alice / ".cache" / "x"))
    assert web.refused_folder(str(alice / ".config" / "autostart"))
    assert web.refused_folder(str(alice / "Pictures")) is None


@linux
def test_a_folder_swapped_for_a_link_after_the_check_is_not_followed(app, tmp_path):  # noqa: F811
    a = app.clean(fixtures.jpeg_everything(), "x.jpg")
    safe, other = tmp_path / "safe", tmp_path / "other"
    safe.mkdir()
    other.mkdir()
    fd, base = web._pin_folder(safe)
    try:
        moved = tmp_path / "moved"
        safe.rename(moved)  # what an attacker does between the check and the write
        safe.symlink_to(other, target_is_directory=True)
        result = web._save_into(app.session, [a["id"]], safe, base)
    finally:
        os.close(fd)
    assert result["files"][0]["ok"]
    assert list(other.iterdir()) == []
    assert (moved / "x.clean.jpg").exists()


@linux
def test_a_link_to_a_credential_folder_found_when_the_folder_is_opened_is_refused(tmp_path):
    (tmp_path / "keys" / ".ssh").mkdir(parents=True)
    link = tmp_path / "pictures"
    link.symlink_to(tmp_path / "keys" / ".ssh", target_is_directory=True)
    with pytest.raises(web.ApiError, match="Cannot save into"):
        web._pin_folder(link)
    assert list((tmp_path / "keys" / ".ssh").iterdir()) == []


def raw_status(port: int, request: bytes) -> int:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(request)
        return int(sock.recv(4096).split(b" ", 2)[1])


def test_one_host_header_and_a_path_only_target(app):  # noqa: F811
    host = f"Host: 127.0.0.1:{app.port}\r\n"
    assert raw_status(app.port, f"GET / HTTP/1.1\r\n{host}Connection: close\r\n\r\n".encode()) == 200
    assert (
        raw_status(app.port, f"GET / HTTP/1.1\r\n{host}Host: evil.example\r\nConnection: close\r\n\r\n".encode()) == 421
    )
    assert (
        raw_status(app.port, f"GET / HTTP/1.1\r\nHost: evil.example\r\n{host}Connection: close\r\n\r\n".encode()) == 421
    )
    absolute = f"GET http://127.0.0.1:{app.port}/ HTTP/1.1\r\n{host}Connection: close\r\n\r\n".encode()
    assert raw_status(app.port, absolute) == 421


def test_the_launch_key_goes_to_the_browser_in_a_private_page_where_nothing_checks_the_user(tmp_path, monkeypatch):
    session = web.Session(str(tmp_path / "work"))
    httpd, session = web.make_server(0, session)
    try:
        url = web.launch_url(httpd, session.launch_key)
        monkeypatch.setattr(web, "_on_linux", lambda: False)  # not sys.platform: other modules read that
        target = web.open_target(session, httpd, url)
        assert target.startswith("file://") and session.launch_key not in target  # nothing secret for `ps` to show
        page = session.dir / web.LAUNCH_PAGE
        assert oct(page.stat().st_mode & 0o777) == "0o600"
        text = page.read_text()
        assert 'http-equiv="refresh"' in text and url in text
        assert session.redeem(session.launch_key)
        assert not page.exists()  # the key is spent, and the page goes with it
        monkeypatch.setattr(web, "_on_linux", lambda: True)
        assert web.open_target(session, httpd, url) == url  # Linux refuses other users' connections itself
    finally:
        httpd.server_close()
        session.close()
