"""The command line's processing record: --record, --new-record-key and --verify-record, and what they guarantee."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys

import pytest
from PIL import Image

from imgstrip import cli, record
from imgstrip.cli import main


def jpeg_with_exif(color=(200, 20, 20), size=(48, 32)) -> bytes:
    exif = Image.Exif()
    exif[0x010F] = "Camera Maker"
    exif[0x0132] = "2020:01:01 10:00:00"
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (24, 24), (1, 2, 3, 200)).save(buf, "PNG")
    return buf.getvalue()


def run(argv, capsys=None):
    # `-o X` with one input and no such folder means "write the file X", so folders are made first.
    argv = [str(a) for a in argv]
    for i, a in enumerate(argv[:-1]):
        if a == "-o" and argv[i + 1] != "-" and not os.path.splitext(argv[i + 1])[1]:
            os.makedirs(argv[i + 1], exist_ok=True)
    try:
        return main(argv)
    except SystemExit as exc:
        return exc.code


sha = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731


@pytest.fixture
def photo(tmp_path):
    p = tmp_path / "in" / "IMG_0001.jpg"
    p.parent.mkdir()
    p.write_bytes(jpeg_with_exif())
    return p


@pytest.fixture
def key(tmp_path):
    k = tmp_path / "keys" / "rec.pem"
    assert run(["--new-record-key", k]) == 0
    return k


def only_record(folder):
    files = sorted(folder.iterdir())
    assert len(files) == 1 and files[0].name.endswith(record.SUFFIX), files
    return files[0], json.loads(files[0].read_text())


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------


def test_new_record_key_writes_a_private_and_a_public_key_and_says_how_to_use_them(tmp_path, capsys):
    k = tmp_path / "a" / "b" / "rec.pem"
    assert run(["--new-record-key", k]) == 0
    out = capsys.readouterr()
    assert "--record-key" in out.out and "--record-pubkey" in out.out and "not protected" in out.err
    assert b"PRIVATE KEY" in k.read_bytes() and b"PUBLIC KEY" in (tmp_path / "a/b/rec.pem.pub").read_bytes()
    assert b"PRIVATE" not in (tmp_path / "a/b/rec.pem.pub").read_bytes()
    if os.name == "posix":
        assert stat.S_IMODE(k.stat().st_mode) == 0o600


def test_new_record_key_protects_the_key_when_a_passphrase_is_given_and_never_overwrites(tmp_path, monkeypatch):
    pw = tmp_path / "pw"
    pw.write_text("a long passphrase\n")
    k = tmp_path / "rec.pem"
    assert run(["--new-record-key", k, "--record-passphrase-file", pw]) == 0
    assert b"ENCRYPTED" in k.read_bytes()
    before = k.read_bytes()
    assert run(["--new-record-key", k]) == 2 and k.read_bytes() == before
    k2 = tmp_path / "rec2.pem"
    monkeypatch.setenv("IMGSTRIP_RECORD_PASSPHRASE", "from the environment")
    assert run(["--new-record-key", k2]) == 0 and b"ENCRYPTED" in k2.read_bytes()


def test_a_passphrase_on_the_command_line_is_refused(tmp_path, capsys):
    assert run(["--new-record-key", tmp_path / "k.pem", "--record-passphrase", "secret"]) == 2
    assert "never accepted on the command line" in capsys.readouterr().err
    assert not (tmp_path / "k.pem").exists()


def test_the_new_key_command_takes_no_files_and_no_other_mode(tmp_path, photo, capsys):
    assert run(["--new-record-key", tmp_path / "k.pem", photo]) == 2
    assert "takes no input files" in capsys.readouterr().err
    assert run(["--new-record-key", tmp_path / "k.pem", "--record", tmp_path / "r"]) == 2


# --------------------------------------------------------------------------
# Writing a record
# --------------------------------------------------------------------------


def test_a_record_holds_the_hashes_of_both_files_and_what_was_done(tmp_path, photo, key):
    out, rec = tmp_path / "out", tmp_path / "rec"
    original = photo.read_bytes()
    assert run([photo, "-o", out, "--record", rec, "--record-key", key, "--case", "C-9", "--operator", "J. Doe"]) == 0
    cleaned = (out / photo.name).read_bytes()
    path, doc = only_record(rec)
    body = doc["record"]
    assert body["input"] == {"name": "IMG_0001.jpg", "sha256": sha(original), "bytes": len(original), "format": "JPEG"}
    assert body["output"]["sha256"] == sha(cleaned) and body["output"]["bytes"] == len(cleaned)
    assert body["case"] == "C-9" and body["operator"] == "J. Doe"
    assert body["result"]["verified_by"] and any(r["label"] == "EXIF" for r in body["result"]["removed"])
    assert body["options"]["format"] is None and body["options"]["max_pixels"] > 0
    assert path.name == f"IMG_0001.jpg.{sha(cleaned)[:12]}{record.SUFFIX}"
    assert record.verify(path.read_bytes()).status == record.SIGNED


def test_the_record_never_goes_into_the_image_and_the_image_is_byte_identical_without_it(tmp_path, photo, key):
    with_rec, without = tmp_path / "a", tmp_path / "b"
    assert run([photo, "-o", with_rec, "--record", tmp_path / "rec", "--record-key", key]) == 0
    assert run([photo, "-o", without]) == 0
    assert (with_rec / photo.name).read_bytes() == (without / photo.name).read_bytes()
    assert b"IMG_0001" not in (with_rec / photo.name).read_bytes()


def test_the_record_file_is_private_to_its_owner(tmp_path, photo, key):
    assert run([photo, "-o", tmp_path / "o", "--record", tmp_path / "rec"]) == 0
    path, _ = only_record(tmp_path / "rec")
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp_path / "rec").stat().st_mode) == 0o700


def test_without_a_key_the_record_is_written_unsigned_and_says_so(tmp_path, photo, capsys):
    assert run([photo, "-o", tmp_path / "o", "--record", tmp_path / "rec"]) == 0
    err = capsys.readouterr()
    assert "UNSIGNED" in err.err and "UNSIGNED" in err.out
    path, doc = only_record(tmp_path / "rec")
    assert doc["signature"] is None and record.verify(path.read_bytes()).status == record.UNSIGNED


def test_json_output_names_the_record_and_its_key(tmp_path, photo, key, capsys):
    assert run([photo, "-o", tmp_path / "o", "--record", tmp_path / "rec", "--record-key", key, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    info = data["files"][0]["record"]
    assert info["signed"] is True and info["path"].endswith(record.SUFFIX) and len(info["key_id"]) == 64
    assert data["record"]["signed"] is True
    assert not any(k.startswith("_") for k in data["files"][0])


def test_a_dry_run_writes_no_record_and_makes_no_folder(tmp_path, photo, key):
    assert run([photo, "-n", "--record", tmp_path / "rec", "--record-key", key]) == 0
    assert not (tmp_path / "rec").exists()


def test_a_stdin_to_stdout_run_still_records_it(tmp_path, key):
    original = jpeg_with_exif()
    proc = subprocess.run(
        [sys.executable, "-m", "imgstrip", "-", "-o", "-", "--record", str(tmp_path / "rec"), "--record-key", str(key)],
        input=original,
        capture_output=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    path, doc = only_record(tmp_path / "rec")
    assert doc["record"]["input"]["name"] == "<stdin>" and doc["record"]["output"]["name"] == "<stdout>"
    assert doc["record"]["input"]["sha256"] == sha(original) and doc["record"]["output"]["sha256"] == sha(proc.stdout)
    assert proc.stdout.startswith(b"\xff\xd8")


def test_in_place_keeps_the_hash_of_the_original_that_is_about_to_be_replaced(tmp_path, photo, key):
    original = photo.read_bytes()
    assert run([photo, "-i", "--record", tmp_path / "rec", "--record-key", key]) == 0
    _, doc = only_record(tmp_path / "rec")
    assert doc["record"]["input"]["sha256"] == sha(original)
    assert doc["record"]["output"]["sha256"] == sha(photo.read_bytes()) != sha(original)


def test_several_files_get_one_record_each_also_in_parallel(tmp_path, key):
    src = tmp_path / "src"
    src.mkdir()
    names = []
    for i in range(5):
        (src / f"p{i}.jpg").write_bytes(jpeg_with_exif((10 * i, 40, 90)))
        names.append(f"p{i}.jpg")
    (src / "logo.png").write_bytes(png())
    assert run([src, "-r", "-o", tmp_path / "o", "--record", tmp_path / "rec", "--record-key", key, "-j", "2"]) == 0
    docs = [json.loads(p.read_text())["record"] for p in (tmp_path / "rec").iterdir()]
    assert sorted(d["input"]["name"] for d in docs) == sorted([*names, "logo.png"])
    for d in docs:
        assert sha((tmp_path / "o" / d["output"]["name"]).read_bytes()) == d["output"]["sha256"]


def test_files_that_fail_get_no_record(tmp_path, key):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"this is not an image")
    assert run([bad, "-o", tmp_path / "o", "--record", tmp_path / "rec", "--record-key", key]) == 1
    assert not list((tmp_path / "rec").iterdir())


def test_a_record_that_already_exists_is_not_replaced_unless_forced(tmp_path, photo, key):
    args = [photo, "-o", tmp_path / "o", "--record", tmp_path / "rec", "--record-key", key]
    assert run(args) == 0
    path, _ = only_record(tmp_path / "rec")
    path.write_text("keep me")
    assert run([*args, "--force"]) == 0 and path.read_text() != "keep me"
    path.write_text("keep me")
    capture = run([photo, "-o", tmp_path / "o2", "--record", tmp_path / "rec", "--record-key", key])
    assert capture == 1 and path.read_text() == "keep me"


def test_a_record_that_cannot_be_written_fails_the_file_and_says_so(tmp_path, photo, capsys, monkeypatch):
    real = cli.write_new

    def write_new(dest, data, mode=None):
        if str(dest).endswith(record.SUFFIX):
            raise OSError(28, "No space left on device")
        return real(dest, data, mode)

    monkeypatch.setattr(cli, "write_new", write_new)
    assert run([photo, "-o", tmp_path / "o", "--record", tmp_path / "rec"]) == 1
    err = capsys.readouterr().err
    assert "cleaned, but its record could not be written" in err and "No space" in err
    assert (tmp_path / "o" / photo.name).exists()  # the image was written, and the message says the record is missing


# --------------------------------------------------------------------------
# Keeping the record apart from the image
# --------------------------------------------------------------------------


def test_the_record_folder_may_not_be_the_folder_the_images_go_to(tmp_path, photo, capsys):
    out = tmp_path / "out"
    out.mkdir()
    assert run([photo, "-o", out, "--record", out]) == 2  # caught before anything is cleaned
    assert "keep it apart from the images" in capsys.readouterr().err
    assert not list(out.iterdir())


def test_the_check_also_covers_a_folder_that_does_not_exist_yet(tmp_path, photo):
    out = tmp_path / "new" / "out"
    assert run([photo, "-o", out / "clean.jpg", "--record", out]) == 1  # caught per file, before the image is written
    assert not (out / "clean.jpg").exists()


def test_in_place_records_must_not_sit_next_to_the_images(photo):
    assert run([photo, "-i", "--record", photo.parent]) == 1
    assert b"Camera Maker" in photo.read_bytes()  # nothing was replaced


def test_a_default_run_may_not_record_into_the_folder_of_the_copies_it_writes_next_to_the_originals(photo):
    assert run([photo, "--record", photo.parent]) == 1
    assert not [p for p in photo.parent.iterdir() if p != photo]


def test_a_sibling_a_sub_folder_and_a_parent_are_all_fine(tmp_path, photo):
    assert run([photo, "-o", tmp_path / "o", "--record", tmp_path / "rec"]) == 0
    assert run([photo, "-o", tmp_path / "o2", "--record", tmp_path / "o2" / "records"]) == 0
    assert run([photo, "-o", tmp_path / "x" / "y", "--record", tmp_path, "--force"]) == 0
    assert run([photo, "--record", photo.parent / "records", "--force"]) == 0


# --------------------------------------------------------------------------
# Options that make no sense together
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra, message",
    [
        (["--case", "X"], "only works with --record"),
        (["--operator", "X"], "only works with --record"),
        (["--record-key", "k"], "only works with --record"),
        (["--record-pubkey", "k"], "only works with --verify-record"),
        (["--record-original", "k"], "only works with --verify-record"),
        (["--record", "r", "--check"], "cannot be combined with --check"),
        (["--verify-record", "--check"], "cannot be combined"),
        (["--record", "r", "--record-passphrase-file", "p"], "needs --record-key"),
    ],
)
def test_misused_record_options_are_usage_errors(tmp_path, photo, capsys, extra, message):
    assert run([photo, *extra]) == 2
    assert message in capsys.readouterr().err


def test_a_case_with_control_characters_or_too_long_fails_the_file_not_the_run(tmp_path, photo, capsys):
    assert run([photo, "-o", tmp_path / "o", "--record", tmp_path / "rec", "--case", "x" * 300]) == 1
    assert "longer than" in capsys.readouterr().err


def test_a_missing_key_file_or_a_wrong_kind_of_key_is_a_clear_usage_error(tmp_path, photo, capsys):
    assert run([photo, "--record", tmp_path / "r", "--record-key", tmp_path / "nope.pem"]) == 2
    assert "cannot read record key" in capsys.readouterr().err
    run(["--new-record-key", tmp_path / "k.pem"])
    assert run([photo, "--record", tmp_path / "r", "--record-key", tmp_path / "k.pem.pub"]) == 2
    assert "private key" in capsys.readouterr().err


def test_a_protected_key_asks_for_its_passphrase_from_the_environment_or_a_file(tmp_path, photo, monkeypatch):
    pw = tmp_path / "pw"
    pw.write_text("open sesame\n")
    k = tmp_path / "k.pem"
    assert run(["--new-record-key", k, "--record-passphrase-file", pw]) == 0
    base = [photo, "-o", tmp_path / "o", "--record", tmp_path / "r", "--record-key", k]
    monkeypatch.delenv("IMGSTRIP_RECORD_PASSPHRASE", raising=False)
    assert run(base) == 2  # nothing to answer with, and no terminal
    monkeypatch.setenv("IMGSTRIP_RECORD_PASSPHRASE", "wrong")
    assert run(base) == 2
    monkeypatch.setenv("IMGSTRIP_RECORD_PASSPHRASE", "open sesame")
    assert run(base) == 0


def test_a_passphrase_set_in_the_environment_does_not_break_an_unprotected_key(tmp_path, photo, key, monkeypatch):
    monkeypatch.setenv("IMGSTRIP_RECORD_PASSPHRASE", "left over from another key")
    assert run([photo, "-o", tmp_path / "o", "--record", tmp_path / "r", "--record-key", key]) == 0


def test_without_the_cryptography_library_a_key_cannot_be_used_but_unsigned_records_still_work(tmp_path, photo, key, monkeypatch, capsys):
    monkeypatch.setattr(record, "available", lambda: False)
    assert run([photo, "-o", tmp_path / "o", "--record", tmp_path / "r", "--record-key", key]) == 2
    assert "imgstrip[record]" in capsys.readouterr().err
    assert run([photo, "-o", tmp_path / "o", "--record", tmp_path / "r"]) == 0


# --------------------------------------------------------------------------
# Checking records
# --------------------------------------------------------------------------


@pytest.fixture
def made(tmp_path, photo, key):
    assert run([photo, "-o", tmp_path / "out", "--record", tmp_path / "rec", "--record-key", key, "--case", "K-1"]) == 0
    path, doc = only_record(tmp_path / "rec")
    return path, doc


def test_verify_accepts_an_intact_record_with_the_right_public_key_and_matching_files(tmp_path, photo, key, made, capsys):
    path, _ = made
    pub = str(key) + ".pub"
    capsys.readouterr()
    code = run(["--verify-record", path, "--record-pubkey", pub, "--record-original", photo, "--record-cleaned", tmp_path / "out" / photo.name])
    out = capsys.readouterr().out
    assert code == 0 and "signed with the key you trust" in out and "match the record" in out and "K-1" in out


def test_verify_without_a_public_key_says_the_key_was_not_checked_and_is_not_a_pass(made, capsys):
    capsys.readouterr()
    assert run(["--verify-record", made[0]]) == 3  # 2.0.6: "ok", exit 0, whatever key had signed it
    out = capsys.readouterr().out
    assert "key not checked" in out and "UNCHECKED" in out and "--record-pubkey" in out


def test_verify_catches_an_edited_record(tmp_path, made, key, capsys):
    path, _ = made
    edited = tmp_path / "edited.json"
    edited.write_text(path.read_text().replace("K-1", "K-2"))
    capsys.readouterr()
    assert run(["--verify-record", edited, "--record-pubkey", str(key) + ".pub"]) == 3
    out = capsys.readouterr().out
    assert "PROBLEM" in out and "changed after it was signed" in out


def test_verify_catches_a_record_re_signed_with_someone_elses_key(tmp_path, made, key, capsys):
    path, doc = made
    other = tmp_path / "other.pem"
    run(["--new-record-key", other])
    forged = tmp_path / "forged"
    assert run([tmp_path / "in" / "IMG_0001.jpg", "-o", tmp_path / "o2", "--record", forged, "--record-key", other, "--case", "K-1"]) == 0
    capsys.readouterr()
    (forged_path,) = list(forged.iterdir())
    assert run(["--verify-record", forged_path, "--record-pubkey", str(key) + ".pub"]) == 3
    assert "different key" in capsys.readouterr().out


def test_verify_catches_a_cleaned_file_or_original_that_is_not_the_one_recorded(tmp_path, photo, made, capsys):
    path, _ = made
    other = tmp_path / "other.jpg"
    other.write_bytes(jpeg_with_exif((1, 2, 3)))
    capsys.readouterr()
    assert run(["--verify-record", path, "--record-cleaned", other]) == 3
    assert "cleaned file you gave does not match" in capsys.readouterr().out
    assert run(["--verify-record", path, "--record-original", other]) == 3


def test_verify_reports_an_unsigned_record_as_a_finding_not_a_pass(tmp_path, photo, capsys):
    run([photo, "-o", tmp_path / "o", "--record", tmp_path / "r"])
    (p,) = list((tmp_path / "r").iterdir())
    capsys.readouterr()
    assert run(["--verify-record", p]) == 3
    assert "UNSIGNED" in capsys.readouterr().out


def test_verify_handles_missing_and_foreign_files(tmp_path, made, capsys):
    capsys.readouterr()
    junk = tmp_path / "junk.json"
    junk.write_text("{}")
    assert run(["--verify-record", tmp_path / "missing.json"]) == 1
    assert run(["--verify-record", junk]) == 3
    assert run(["--verify-record", made[0], junk]) == 3  # one bad record makes the run a finding
    assert run(["--verify-record", made[0], tmp_path / "missing.json"]) == 1  # an unreadable one makes it an error


def test_verify_json_is_machine_readable(made, key, capsys):
    capsys.readouterr()
    assert run(["--verify-record", made[0], "--record-pubkey", str(key) + ".pub", "--json"]) == 0
    (entry,) = json.loads(capsys.readouterr().out)["records"]
    assert entry["status"] == "trusted" and entry["ok"] is True and entry["case"] == "K-1" and entry["problems"] == []


def test_comparing_files_needs_exactly_one_record(made, tmp_path, capsys):
    assert run(["--verify-record", made[0], made[0], "--record-cleaned", made[0]]) == 2
    assert "exactly one record" in capsys.readouterr().err


def test_a_public_key_that_is_really_a_private_key_is_refused(made, key, capsys):
    capsys.readouterr()
    assert run(["--verify-record", made[0], "--record-pubkey", key]) == 2
    assert "PRIVATE key" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Nothing changes when records are off
# --------------------------------------------------------------------------


def test_json_output_has_no_record_fields_when_records_are_off(tmp_path, photo, capsys):
    assert run([photo, "-o", tmp_path / "o", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "record" not in data and "record" not in data["files"][0]
    assert not any(k.startswith("_") for k in data["files"][0])


def test_with_a_content_credential_the_record_describes_the_signed_file(tmp_path, photo, key, signer):
    """The record is made after the credential was added, so its hash is of the bytes that were really written."""
    (tmp_path / "chain.pem").write_bytes(signer.chain_pem)
    (tmp_path / "ckey.pem").write_bytes(signer.key_pem)
    os.chmod(tmp_path / "ckey.pem", 0o600)
    args = [photo, "-o", tmp_path / "o", "--c2pa", "--c2pa-cert", tmp_path / "chain.pem", "--c2pa-key", tmp_path / "ckey.pem"]
    assert run([*args, "--record", tmp_path / "rec", "--record-key", key]) == 0
    _, doc = only_record(tmp_path / "rec")
    written = (tmp_path / "o" / photo.name).read_bytes()
    assert doc["record"]["output"]["sha256"] == sha(written) and doc["record"]["output"]["bytes"] == len(written)
    assert doc["record"]["result"]["content_credentials"] is True
