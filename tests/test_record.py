"""The signed processing record: what it holds, that any edit is detected, and how keys are handled."""

from __future__ import annotations

import base64
import json

import pytest

from imgstrip import record
from imgstrip.core import Options


def make_body(**kw):
    args = dict(
        input_name="IMG_0001.jpg",
        input_data_sha256="a" * 64,
        input_bytes=1234,
        input_format="JPEG",
        output_name="IMG_0001.jpg",
        output_data=b"cleaned bytes",
        output_format="JPEG",
        options=Options(scrub_dct="canonical"),
        result={"removed": [{"category": "metadata", "label": "EXIF", "detail": "gps"}]},
        case="CASE-7",
        operator="A. Examiner",
        when="2026-01-02T03:04:05Z",
    )
    args.update(kw)
    return record.build(**args)


@pytest.fixture(scope="module")
def keys():
    private, public, key_id = record.generate_key()
    return private, public, key_id


@pytest.fixture()
def signer(keys):
    return record.load_signer(keys[0])


# --------------------------------------------------------------------------
# What a record holds
# --------------------------------------------------------------------------


def test_body_holds_hashes_versions_options_and_the_people(signer):
    body = make_body()
    assert body["format"] == "imgstrip-record" and body["version"] == 1
    assert body["created"] == "2026-01-02T03:04:05Z"
    assert body["case"] == "CASE-7" and body["operator"] == "A. Examiner"
    assert body["input"] == {"name": "IMG_0001.jpg", "sha256": "a" * 64, "bytes": 1234, "format": "JPEG"}
    assert body["output"]["sha256"] == record.sha256(b"cleaned bytes") and body["output"]["bytes"] == 13
    assert body["tool"]["imgstrip"] and body["tool"]["pillow"] and body["tool"]["python"]
    assert body["options"]["scrub_dct"] == "canonical" and body["options"]["icc"] == "convert"


def test_the_body_never_holds_floats_so_every_reader_rebuilds_the_same_bytes():
    body = make_body(options=Options(dct_min_psnr=25.5))

    def floats(value):
        if isinstance(value, float):
            return [value]
        if isinstance(value, dict):
            return [f for v in value.values() for f in floats(v)]
        if isinstance(value, list):
            return [f for v in value for f in floats(v)]
        return []

    assert floats(body) == []
    assert body["options"]["dct_min_psnr"] == "25.5"


def test_the_time_is_utc_to_the_second():
    body = make_body(when=None)
    assert body["created"].endswith("Z") and len(body["created"]) == 20


def test_no_record_holds_a_host_name_or_a_user_name():
    text = json.dumps(make_body())
    import getpass
    import socket

    for private in (socket.gethostname(), getpass.getuser()):
        if len(private) > 3:
            assert private not in text


def test_result_facts_keeps_what_was_removed_and_the_checks_that_passed():
    payload = {
        "size": [10, 20],
        "frames": 1,
        "pixels_exact": True,
        "verified_by": ["structure", "Pillow"],
        "removed": [{"category": "metadata", "label": "EXIF", "detail": "14 bytes", "extra": "dropped"}],
        "actions": ["re-encoded"],
        "warnings": ["w"],
        "kept": {"format": "JPEG"},
        "steganography": {"state": "not_scanned", "summary": "long text"},
        "dct": {"headline": "canonical", "summary": "x"},
        "credentials": {"signed": True, "signer": "someone"},
    }
    facts = record.result_facts(payload)
    assert facts["removed"] == [{"category": "metadata", "label": "EXIF", "detail": "14 bytes"}]
    assert facts["verified_by"] == ["structure", "Pillow"] and facts["steganography"] == "not_scanned"
    assert facts["dct"] == "canonical" and facts["content_credentials"] is True
    assert "someone" not in json.dumps(facts)


@pytest.mark.parametrize("text", ["", "   ", None])
def test_empty_case_and_operator_mean_none(text):
    assert record.clean_text(text, "case reference") is None


def test_case_text_is_tidied_and_bounded():
    assert record.clean_text("  CASE \n  12\t B ", "case reference") == "CASE 12 B"
    with pytest.raises(record.RecordError, match="longer than"):
        record.clean_text("x" * 201, "case reference")
    with pytest.raises(record.RecordError, match="control"):
        record.clean_text("a\x07b", "operator name")


def test_file_names_are_safe_and_carry_the_hash():
    name = record.file_name("../../etc/pass wd.jpg", "0123456789abcdef" * 4)
    assert "/" not in name and "\\" not in name and name.endswith(".0123456789ab.imgstrip-record.json")
    assert name == "_.._etc_pass_wd.jpg.0123456789ab.imgstrip-record.json"
    assert not record.file_name("...", "ab" * 32).startswith(".")
    assert record.file_name("-rf.jpg", "ab" * 32).startswith("_-rf")


# --------------------------------------------------------------------------
# Signing and checking
# --------------------------------------------------------------------------


def test_a_signed_record_verifies_and_is_trusted_with_the_right_key(keys, signer):
    document = record.seal(make_body(), signer)
    assert record.verify(document).status == record.SIGNED and record.verify(document).ok
    verdict = record.verify(document, record.load_public(keys[1]))
    assert verdict.status == record.TRUSTED and verdict.ok and verdict.key_id == keys[2]
    assert verdict.body["case"] == "CASE-7"


def test_the_signature_block_names_the_key_and_the_algorithm(keys, signer):
    sig = json.loads(record.seal(make_body(), signer))["signature"]
    assert sig["algorithm"] == "ed25519" and sig["key_id"] == keys[2]
    assert record.key_id_of(base64.b64decode(sig["public_key"])) == keys[2]


@pytest.mark.parametrize(
    "path, value",
    [
        (("record", "case"), "CASE-8"),
        (("record", "operator"), "someone else"),
        (("record", "created"), "2026-01-02T03:04:06Z"),
        (("record", "input", "sha256"), "b" * 64),
        (("record", "output", "sha256"), "c" * 64),
        (("record", "options", "icc"), "drop"),
        (("record", "result", "removed"), []),
        (("record", "tool", "imgstrip"), "9.9.9"),
    ],
)
def test_editing_any_field_breaks_the_signature(signer, path, value):
    doc = json.loads(record.seal(make_body(), signer))
    node = doc
    for key in path[:-1]:
        node = node[key]
    assert node[path[-1]] != value
    node[path[-1]] = value
    verdict = record.verify(json.dumps(doc).encode())
    assert verdict.status == record.INVALID and "changed after it was signed" in verdict.problems[0]


def test_adding_or_removing_a_field_also_breaks_it(signer):
    doc = json.loads(record.seal(make_body(), signer))
    added = json.loads(json.dumps(doc))
    added["record"]["extra"] = "x"
    removed = json.loads(json.dumps(doc))
    del removed["record"]["operator"]
    assert record.verify(json.dumps(added).encode()).status == record.INVALID
    assert record.verify(json.dumps(removed).encode()).status == record.INVALID


def test_the_way_the_file_is_laid_out_does_not_matter_only_the_content(signer):
    doc = json.loads(record.seal(make_body(), signer))
    squeezed = json.dumps(doc, separators=(",", ":"), sort_keys=False).encode()
    assert record.verify(squeezed).status == record.SIGNED


def test_swapping_in_a_different_key_and_signature_is_caught_by_a_trusted_key(keys, signer):
    """Someone edits the record and re-signs it with their own key: the signature is consistent, the key is not yours."""
    forged = record.seal(make_body(case="FORGED"), record.load_signer(record.generate_key()[0]))
    assert record.verify(forged).status == record.SIGNED  # intact on its own, which is why a trusted key matters
    verdict = record.verify(forged, record.load_public(keys[1]))
    assert verdict.status == record.INVALID and "different key" in verdict.problems[0]


def test_a_signature_block_that_lies_about_its_key_is_refused(signer):
    doc = json.loads(record.seal(make_body(), signer))
    doc["signature"]["key_id"] = "0" * 64
    assert record.verify(json.dumps(doc).encode()).status == record.INVALID


def test_an_unsigned_record_is_labelled_unsigned_and_not_ok():
    verdict = record.verify(record.seal(make_body(), None))
    assert verdict.status == record.UNSIGNED and not verdict.ok and "not signed" in verdict.problems[0]


def test_removing_the_signature_from_a_signed_record_does_not_make_it_pass(signer):
    doc = json.loads(record.seal(make_body(), signer))
    doc["signature"] = None
    verdict = record.verify(json.dumps(doc).encode())
    assert verdict.status == record.UNSIGNED and not verdict.ok


@pytest.mark.parametrize(
    "junk",
    [b"", b"not json", b"[]", b"{}", b'{"record": 1, "signature": null}', b'{"record": {"format": "other"}, "signature": null}',
     b"\xff\xfe", b"[" * 100000, b"x" * (record.MAX_RECORD_BYTES + 1)],
)  # fmt: skip
def test_things_that_are_not_records_are_refused_without_a_crash(junk):
    verdict = record.verify(junk)
    assert verdict.status == record.INVALID and not verdict.ok


def test_a_newer_record_version_is_refused_not_guessed_at(signer):
    doc = json.loads(record.seal(make_body(), signer))
    doc["record"]["version"] = 2
    assert "version" in record.verify(json.dumps(doc).encode()).problems[0]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update(algorithm="rsa"),
        lambda s: s.update(value="!!notbase64!!"),
        lambda s: s.update(public_key=base64.b64encode(b"short").decode()),
        lambda s: s.pop("value"),
        lambda s: s.update(value=base64.b64encode(b"\0" * 64).decode()),
    ],
)
def test_a_damaged_signature_block_is_invalid(signer, mutate):
    doc = json.loads(record.seal(make_body(), signer))
    mutate(doc["signature"])
    assert record.verify(json.dumps(doc).encode()).status == record.INVALID


def test_the_files_can_be_compared_with_the_record(signer):
    body = make_body(output_data=b"the cleaned file")
    body["input"]["sha256"] = record.sha256(b"the original")
    assert record.check_files(body, original=b"the original", cleaned=b"the cleaned file") == []
    assert len(record.check_files(body, original=b"another original")) == 1
    assert "cleaned file" in record.check_files(body, cleaned=b"tampered")[0]
    assert record.check_files(body) == []


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------


def test_a_protected_key_needs_its_passphrase():
    private, public, key_id = record.generate_key(b"correct horse")
    assert b"ENCRYPTED" in private
    with pytest.raises(record.PassphraseRequired):
        record.load_signer(private)
    with pytest.raises(record.PassphraseRequired, match="wrong"):
        record.load_signer(private, b"battery staple")
    assert record.load_signer(private, b"correct horse").key_id == key_id


def test_an_unprotected_key_loads_without_one(keys):
    assert b"ENCRYPTED" not in keys[0]
    assert record.load_signer(keys[0]).key_id == keys[2]
    assert record.load_signer(keys[0], b"unneeded").key_id == keys[2]


def test_the_public_key_is_not_a_private_key_and_vice_versa(keys):
    with pytest.raises(record.RecordError, match="private key"):
        record.load_signer(keys[1])
    with pytest.raises(record.RecordError, match="PRIVATE key"):
        record.load_public(keys[0])
    with pytest.raises(record.RecordError):
        record.load_public(b"nothing")
    with pytest.raises(record.RecordError):
        record.load_signer(b"-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n")


def test_only_ed25519_keys_are_accepted():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    with pytest.raises(record.RecordError, match="Ed25519"):
        record.load_signer(private)
    with pytest.raises(record.RecordError, match="Ed25519"):
        record.load_public(public)


def test_the_signer_never_shows_its_key_when_printed(signer):
    assert "private" not in repr(signer).lower() and "Ed25519PrivateKey" not in repr(signer)


def test_the_short_key_id_reads_in_groups_of_four(keys):
    short = record.short_id(keys[2])
    assert len(short.replace(" ", "")) == 16 and short.count(" ") == 3


def test_without_the_cryptography_library_signing_says_how_to_get_it(monkeypatch):
    import builtins

    real = builtins.__import__

    def blocked(name, *a, **kw):
        if name.startswith("cryptography"):
            raise ImportError(name)
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert record.available() is False
    with pytest.raises(record.RecordError, match=r"imgstrip\[record\]"):
        record.generate_key()
    # an unsigned record needs no library at all
    assert record.verify(record.seal(make_body(), None)).status == record.UNSIGNED
