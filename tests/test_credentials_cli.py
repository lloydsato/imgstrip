"""The command line: --c2pa signing, receipts, --c2pa-verify and audits, and the guarantees around them."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys

import pytest
from PIL import Image

from c2pa_support import Pki, foreign_credential, key_pem, pem_chain, photo_bytes, self_signed
from imgstrip.cli import main
from imgstrip.inspect import inspect_bytes, is_c2pa_finding

pytestmark = pytest.mark.usefixtures("creds")


@pytest.fixture
def ids(tmp_path, signer):
    d = tmp_path / "ids"
    d.mkdir()
    (d / "chain.pem").write_bytes(signer.chain_pem)
    (d / "key.pem").write_bytes(signer.key_pem)
    (d / "root.pem").write_bytes(signer.root_pem)
    os.chmod(d / "key.pem", 0o600)
    return d


@pytest.fixture
def photo(tmp_path):
    p = tmp_path / "photo.png"
    p.write_bytes(photo_bytes("PNG"))
    return p


def sign_args(ids, *extra):
    return ["--c2pa", "--c2pa-cert", str(ids / "chain.pem"), "--c2pa-key", str(ids / "key.pem"), *extra]


def read(creds, path):
    return creds.read_credential(path.read_bytes())


def exit_code(argv):
    try:
        return main(argv)
    except SystemExit as exc:
        return exc.code


# --------------------------------------------------------------------------
# Making a test identity
# --------------------------------------------------------------------------


def test_the_test_identity_files_are_created_safely(tmp_path, capsys, creds):
    out = tmp_path / "t"
    assert main(["--c2pa-test-identity", str(out), "--c2pa-test-name", "Night Desk"]) == 0
    names = sorted(p.name for p in out.iterdir())
    assert names == ["test-root-ca.pem", "test-signer-chain.pem", "test-signer-key.pem"]
    assert stat.S_IMODE((out / "test-signer-key.pem").stat().st_mode) == 0o600
    text = capsys.readouterr().out
    assert "UNTRUSTED" in text and "Night Desk" in text and "C2PA Trust List" in text
    ident = creds.load_identity(
        (out / "test-signer-chain.pem").read_bytes(), (out / "test-signer-key.pem").read_bytes()
    )
    assert ident.organization == "Night Desk" and ident.is_test
    assert exit_code(["--c2pa-test-identity", str(out)]) == 2, "never overwrite an existing identity"


# --------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------


def test_signing_writes_a_verified_credential_and_leaves_the_original_alone(creds, ids, photo, capsys):
    before = photo.read_bytes()
    assert main(sign_args(ids, str(photo))) == 0
    out, err = capsys.readouterr()
    signed = photo.with_name("photo.clean.png")
    assert photo.read_bytes() == before
    cred = read(creds, signed)
    assert cred.imgstrip and cred.pixels_only and cred.signer == "Test Newsroom"
    assert "signed: Test Newsroom" in out and "pixels identical to the file received" in out and "Unverified" in out
    assert "test/development certificate" in err and "'Unverified' warning" in err


def test_without_c2pa_nothing_changes_and_the_sdk_is_never_even_imported(tmp_path, photo):
    code = (
        "import sys; from imgstrip.cli import main; "
        f"rc = main([{str(photo)!r}]); "
        "assert rc == 0, rc; "
        "assert 'c2pa' not in sys.modules, 'the C2PA library was loaded without --c2pa'; "
        "assert 'imgstrip.credentials' not in sys.modules"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert not any(is_c2pa_finding(f) for f in inspect_bytes(photo.with_name("photo.clean.png").read_bytes()).findings)


def test_detail_and_date_options_reach_the_credential(creds, ids, photo, capsys):
    assert main(sign_args(ids, "--c2pa-date", "minute", "--c2pa-detail", "full", "--jpeg-lossless", str(photo))) == 0
    s = read(creds, photo.with_name("photo.clean.png")).sanitization
    assert len(s["date"]) == len("2026-10-15T13:37Z") and s["operations"] == []


def test_the_original_credentials_are_reported_as_removed(creds, ids, tmp_path, capsys):
    foreign = Pki("Camera Corp").issue(org="Camera Corp")
    src = tmp_path / "cam.png"
    src.write_bytes(foreign_credential(foreign, photo_bytes("PNG"), "PNG"))
    assert main(sign_args(ids, str(src))) == 0
    out = capsys.readouterr().out
    assert "carried Content Credentials (signed by Camera Corp)" in out and "not for where the image came from" in out
    assert b"Camera Corp" not in (tmp_path / "cam.clean.png").read_bytes()


@pytest.mark.parametrize("fmt", ["bmp", "tiff"])
def test_formats_that_cannot_carry_a_credential_fail_and_write_nothing(ids, photo, capsys, fmt):
    code = main(sign_args(ids, "--format", fmt, "-o", str(photo.parent / f"out.{fmt}"), str(photo)))
    assert code == 1
    assert not (photo.parent / f"out.{fmt}").exists()
    assert f"{fmt.upper()} files cannot carry Content Credentials" in capsys.readouterr().err


def test_dry_run_checks_everything_but_writes_nothing(ids, photo, tmp_path):
    receipts = tmp_path / "r.json"
    assert main(sign_args(ids, "--dry-run", "--c2pa-commit-source", "--c2pa-receipt", str(receipts), str(photo))) == 0
    assert not photo.with_name("photo.clean.png").exists() and not receipts.exists()


def test_in_place_replaces_the_file_with_the_signed_one(creds, ids, photo):
    assert main(sign_args(ids, "--in-place", str(photo))) == 0
    assert read(creds, photo).imgstrip and not photo.with_name("photo.clean.png").exists()


def test_signing_parallel_batches_in_the_parent_process(creds, ids, tmp_path):
    files = []
    for i in range(4):
        p = tmp_path / f"p{i}.png"
        Image.new("RGB", (40 + i, 30), (i * 40, 90, 200)).save(p)
        files.append(str(p))
    out = tmp_path / "out"
    assert main(sign_args(ids, "-j", "2", "-o", str(out), *files)) == 0
    for i in range(4):
        assert read(creds, out / f"p{i}.png").imgstrip


def test_stdin_to_stdout_carries_the_credential(creds, ids, signer):
    cmd = [
        sys.executable,
        "-m",
        "imgstrip",
        "-",
        "--c2pa",
        "--c2pa-cert",
        str(ids / "chain.pem"),
        "--c2pa-key",
        str(ids / "key.pem"),
    ]
    proc = subprocess.run(cmd, input=photo_bytes("PNG"), capture_output=True)
    assert proc.returncode == 0, proc.stderr
    assert creds.read_credential(proc.stdout).imgstrip
    assert b"test/development certificate" in proc.stderr


# --------------------------------------------------------------------------
# Usage errors
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra, message",
    [
        (["--c2pa-receipt", "r.json"], "--c2pa-receipt only works with --c2pa"),
        (["--c2pa-trust-anchors", "x.pem", "--check"], "only applies with"),
        (["--c2pa-original", "x.png"], "only applies with --c2pa-verify"),
        (["--c2pa-verify", "--c2pa-original", "x.png"], "needs --c2pa-receipt"),
        (["--c2pa-verify", "--c2pa-receipt", "r.json"], "needs --c2pa-original"),
        (["--c2pa-verify", "--check"], "separate modes"),
    ],
)
def test_option_misuse_is_a_usage_error(photo, capsys, extra, message):
    assert exit_code([*extra, str(photo)]) == 2
    assert message in capsys.readouterr().err


def test_a_commitment_and_its_receipt_go_together(ids, photo, capsys):
    assert exit_code([*sign_args(ids, "--c2pa-commit-source"), str(photo)]) == 2
    assert "needs --c2pa-receipt" in capsys.readouterr().err
    assert exit_code([*sign_args(ids, "--c2pa-receipt", "r.json"), str(photo)]) == 2
    assert "only makes sense with --c2pa-commit-source" in capsys.readouterr().err


@pytest.mark.parametrize(
    "flag", ["--c2pa-cert x", "--c2pa-key x", "--c2pa-date minute", "--c2pa-detail full", "--c2pa-passphrase-file x"]
)
def test_credential_options_without_the_toggle_are_refused(photo, capsys, flag):
    assert exit_code([*flag.split(), str(photo)]) == 2
    assert f"{flag.split()[0]} only works with --c2pa" in capsys.readouterr().err


def test_several_credential_options_without_the_toggle_are_named_together(photo, capsys):
    assert exit_code(["--c2pa-date", "minute", "--c2pa-detail", "full", str(photo)]) == 2
    assert "--c2pa-date, --c2pa-detail only work with --c2pa" in capsys.readouterr().err


def test_the_toggle_without_an_identity_is_refused(photo, capsys):
    assert exit_code(["--c2pa", str(photo)]) == 2
    assert "needs your signing certificate" in capsys.readouterr().err
    assert exit_code(["--c2pa", "--c2pa-cert", "/does/not/exist", str(photo)]) == 2
    assert "cannot read certificate" in capsys.readouterr().err


def test_c2pa_cannot_be_combined_with_checking(ids, photo, capsys):
    assert exit_code([*sign_args(ids), "--check", str(photo)]) == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_a_passphrase_is_never_accepted_on_the_command_line(ids, photo, capsys):
    for argv in (["--c2pa-passphrase", "hunter2"], ["--c2pa-passphrase=hunter2"]):
        assert exit_code([*sign_args(ids), *argv, str(photo)]) == 2
        err = capsys.readouterr().err
        assert "never accepted on the command line" in err and "IMGSTRIP_C2PA_PASSPHRASE" in err


def test_missing_sdk_is_a_clear_usage_error(ids, photo, capsys, monkeypatch, creds):
    monkeypatch.setattr(creds, "available", lambda: False)
    assert exit_code([*sign_args(ids), str(photo)]) == 2
    assert "pip install 'imgstrip[c2pa]'" in capsys.readouterr().err


def test_unusable_identities_are_refused_before_any_file_is_touched(tmp_path, photo, capsys, creds):
    own = self_signed()
    (tmp_path / "self.pem").write_bytes(pem_chain(own) + key_pem(own))
    os.chmod(tmp_path / "self.pem", 0o600)
    assert exit_code(["--c2pa", "--c2pa-cert", str(tmp_path / "self.pem"), str(photo)]) == 2
    assert "self-signed" in capsys.readouterr().err
    import datetime as dt

    expired = Pki().issue(
        not_before=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=9),
        not_after=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1),
    )
    (tmp_path / "old.pem").write_bytes(pem_chain(expired) + key_pem(expired))
    os.chmod(tmp_path / "old.pem", 0o600)
    assert exit_code(["--c2pa", "--c2pa-cert", str(tmp_path / "old.pem"), str(photo)]) == 2
    assert "expired" in capsys.readouterr().err
    assert not photo.with_name("photo.clean.png").exists()


# --------------------------------------------------------------------------
# Passphrases
# --------------------------------------------------------------------------


@pytest.fixture
def locked(tmp_path, signer, creds):
    d = tmp_path / "locked"
    t = creds.make_test_identity("Locked Desk", passphrase=b"open sesame")
    d.mkdir()
    (d / "chain.pem").write_bytes(t.chain_pem + t.root_pem)
    (d / "key.pem").write_bytes(t.key_pem)
    os.chmod(d / "key.pem", 0o600)
    return d


def test_the_passphrase_comes_from_the_environment_or_a_file_never_argv(
    creds, locked, photo, monkeypatch, tmp_path, capsys
):
    base = ["--c2pa", "--c2pa-cert", str(locked / "chain.pem"), "--c2pa-key", str(locked / "key.pem")]
    monkeypatch.delenv("IMGSTRIP_C2PA_PASSPHRASE", raising=False)
    assert exit_code([*base, str(photo)]) == 2
    err = capsys.readouterr().err
    assert "IMGSTRIP_C2PA_PASSPHRASE" in err and "never accepted on the command line" in err
    monkeypatch.setenv("IMGSTRIP_C2PA_PASSPHRASE", "wrong")
    assert exit_code([*base, str(photo)]) == 2
    assert "passphrase is wrong" in capsys.readouterr().err
    monkeypatch.setenv("IMGSTRIP_C2PA_PASSPHRASE", "open sesame")
    assert main([*base, str(photo)]) == 0
    assert read(creds, photo.with_name("photo.clean.png")).signer == "Locked Desk"
    monkeypatch.delenv("IMGSTRIP_C2PA_PASSPHRASE")
    pw = tmp_path / "pw.txt"
    pw.write_text("open sesame\n")
    assert main([*base, "--c2pa-passphrase-file", str(pw), "--force", str(photo)]) == 0


def test_a_missing_passphrase_asks_when_a_person_is_at_the_keyboard(creds, locked, photo, monkeypatch):
    import getpass

    monkeypatch.delenv("IMGSTRIP_C2PA_PASSPHRASE", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    asked = []
    monkeypatch.setattr(getpass, "getpass", lambda prompt: asked.append(prompt) or "open sesame")
    assert (
        main(["--c2pa", "--c2pa-cert", str(locked / "chain.pem"), "--c2pa-key", str(locked / "key.pem"), str(photo)])
        == 0
    )
    assert asked and "Passphrase" in asked[0]


def test_loose_key_permissions_are_warned_about_but_certificates_are_not(ids, photo, capsys):
    os.chmod(ids / "key.pem", 0o644)
    assert main(sign_args(ids, "--dry-run", str(photo))) == 0
    err = capsys.readouterr().err
    assert "key.pem can be read by other users" in err and "chmod 600" in err and "chain.pem can be read" not in err
    os.chmod(ids / "key.pem", 0o600)
    assert main(sign_args(ids, "--dry-run", str(photo))) == 0
    assert "can be read by other users" not in capsys.readouterr().err


# --------------------------------------------------------------------------
# Receipts and audits
# --------------------------------------------------------------------------


def test_receipts_are_private_incremental_and_never_overwritten(creds, ids, tmp_path, photo, capsys):
    receipts = tmp_path / "receipts.json"
    second = tmp_path / "second.png"
    Image.new("RGB", (50, 40), (10, 200, 30)).save(second)
    assert main(sign_args(ids, "--c2pa-commit-source", "--c2pa-receipt", str(receipts), str(photo))) == 0
    assert stat.S_IMODE(receipts.stat().st_mode) == 0o600
    doc = json.loads(receipts.read_text())
    assert doc["imgstrip_receipts"] == 1 and "PRIVATE" in doc["warning"] and len(doc["receipts"]) == 1
    assert main(sign_args(ids, "--c2pa-commit-source", "--c2pa-receipt", str(receipts), str(second))) == 0
    assert len(json.loads(receipts.read_text())["receipts"]) == 2, "appended, not replaced"
    stranger = tmp_path / "notes.json"
    stranger.write_text('{"my": "notes"}')
    assert (
        exit_code(sign_args(ids, "--c2pa-commit-source", "--c2pa-receipt", str(stranger), str(photo), "--force")) == 2
    )
    assert stranger.read_text() == '{"my": "notes"}', "a file that is not ours is left untouched"
    assert "not an imgstrip receipt file" in capsys.readouterr().err


def test_a_file_that_is_not_written_leaves_no_receipt(ids, tmp_path, photo):
    receipts = tmp_path / "receipts.json"
    photo.with_name("photo.clean.png").write_bytes(b"in the way")
    assert main(sign_args(ids, "--c2pa-commit-source", "--c2pa-receipt", str(receipts), str(photo))) == 1
    assert not receipts.exists()


def test_verify_and_audit_round_trip(creds, ids, tmp_path, photo, capsys):
    receipts = tmp_path / "receipts.json"
    assert main(sign_args(ids, "--c2pa-commit-source", "--c2pa-receipt", str(receipts), str(photo))) == 0
    signed = photo.with_name("photo.clean.png")
    capsys.readouterr()
    assert main(["--c2pa-verify", str(signed)]) == 0
    out = capsys.readouterr().out
    assert (
        out.startswith("ok ") and "signed by Test Newsroom" in out and "pixels: identical to the file received" in out
    )
    assert "the file holds nothing but the picture and this credential" in out and "sealed commitment" in out
    assert main(["--c2pa-verify", "--c2pa-trust-anchors", str(ids / "root.pem"), str(signed)]) == 0
    assert "credential: Trusted" in capsys.readouterr().out
    assert main(["--c2pa-verify", "--c2pa-original", str(photo), "--c2pa-receipt", str(receipts), str(signed)]) == 0
    assert "audit: the original matches" in capsys.readouterr().out
    other = tmp_path / "other.png"
    Image.new("RGB", (96, 72), (1, 2, 3)).save(other)
    assert main(["--c2pa-verify", "--c2pa-original", str(other), "--c2pa-receipt", str(receipts), str(signed)]) == 3
    assert "this is not the original" in capsys.readouterr().out


def test_verify_exit_codes_for_everything_that_is_not_a_clean_imgstrip_credential(creds, ids, tmp_path, photo, capsys):
    assert main(sign_args(ids, str(photo))) == 0
    signed = photo.with_name("photo.clean.png")
    capsys.readouterr()
    assert main(["--c2pa-verify", str(photo)]) == 3
    assert "no Content Credentials" in capsys.readouterr().out
    broken = tmp_path / "broken.png"
    data = bytearray(signed.read_bytes())
    data[data.index(b"IDAT") + 12] ^= 1
    broken.write_bytes(bytes(data))
    assert main(["--c2pa-verify", str(broken)]) == 3
    assert "problem: assertion.dataHash.mismatch" in capsys.readouterr().out
    foreign = tmp_path / "foreign.png"
    foreign.write_bytes(foreign_credential(Pki("Cam").issue(org="Cam"), photo_bytes("PNG"), "PNG"))
    assert main(["--c2pa-verify", str(foreign)]) == 3
    assert "not an imgstrip credential" in capsys.readouterr().out
    assert main(["--c2pa-verify", str(tmp_path / "missing.png")]) == 1


def test_verify_walks_directories_and_speaks_json(creds, ids, tmp_path, photo, capsys):
    assert main(sign_args(ids, "-o", str(tmp_path / "signed") + os.sep, str(photo))) == 0
    (tmp_path / "signed" / "notes.txt").write_text("not an image")
    capsys.readouterr()
    assert main(["--c2pa-verify", "-r", "--json", str(tmp_path / "signed")]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["summary"] == {"verified": 1, "unverified": 0, "skipped": 1, "failed": 0}
    cred = next(f for f in doc["files"] if f["status"] == "verified")["credential"]
    assert cred["imgstrip"] and cred["pixels_only"] and cred["sanitization"]["scope"] == "processing"


def test_check_shows_a_files_credential_and_still_reports_it_as_extra_data(creds, ids, photo, capsys):
    assert main(sign_args(ids, str(photo))) == 0
    capsys.readouterr()
    assert main(["--check", str(photo.with_name("photo.clean.png"))]) == 3
    out = capsys.readouterr().out
    assert "C2PA content credentials" in out and "credential: Unverified" in out and "signed by Test Newsroom" in out
    assert main(["--check", str(photo)]) == 0, "a file with no credential is unaffected"


# --------------------------------------------------------------------------
# JSON reports never carry secrets
# --------------------------------------------------------------------------


def test_json_reports_describe_the_signing_without_any_secret(creds, ids, photo, capsys, signer):
    receipts = photo.parent / "r.json"
    assert main(sign_args(ids, "--json", "--c2pa-commit-source", "--c2pa-receipt", str(receipts), str(photo))) == 0
    raw = capsys.readouterr().out
    doc = json.loads(raw)
    rec = doc["files"][0]
    assert (
        rec["credentials"]["signed"]
        and rec["credentials"]["committed"]
        and rec["credentials"]["pixel_integrity"] == "identical"
    )
    assert (
        doc["credentials"]["identity"]["signer"] == "Test Newsroom" and doc["credentials"]["identity"]["test"] is True
    )
    forbidden = [
        "PRIVATE KEY",
        "_data",
        "_evidence",
        "_receipt",
        "salt",
        "source_pixels_sha256",
        signer.key_pem.decode().splitlines()[1],
    ]
    for word in forbidden:
        assert word not in raw, word
    assert "salt" in receipts.read_text(), "the secrets belong in the private receipt file, and only there"


def test_signing_leaves_no_key_or_original_bytes_next_to_the_output(ids, tmp_path, photo, signer):
    out = tmp_path / "out"
    assert main(sign_args(ids, "-o", str(out) + os.sep, str(photo))) == 0
    assert [p.name for p in out.iterdir()] == ["photo.png"], "no temporary files are left behind"
    from c2pa_support import key_der

    assert key_der(signer.identity) not in (out / "photo.png").read_bytes()
