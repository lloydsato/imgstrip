"""The strict manifest-store parser: every byte accounted for, or an error.

Most of these tests build stores by hand, so they need nothing but the standard library; the
container tests at the end use real credentials from the C2PA SDK.
"""

from __future__ import annotations

import struct

import pytest

from imgstrip import jumbf
from imgstrip.jumbf import JumbfError, Tagged

# --------------------------------------------------------------------------
# A tiny encoder, so the tests do not depend on the parser's own helpers
# --------------------------------------------------------------------------


def cbor(v) -> bytes:
    def head(major: int, n: int) -> bytes:
        if n < 24:
            return bytes([major << 5 | n])
        for info, width in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 1 << (8 * width):
                return bytes([major << 5 | info]) + n.to_bytes(width, "big")
        raise ValueError(n)

    if v is None:
        return b"\xf6"
    if v is True:
        return b"\xf5"
    if v is False:
        return b"\xf4"
    if isinstance(v, int):
        return head(0, v) if v >= 0 else head(1, -1 - v)
    if isinstance(v, bytes):
        return head(2, len(v)) + v
    if isinstance(v, str):
        raw = v.encode()
        return head(3, len(raw)) + raw
    if isinstance(v, list):
        return head(4, len(v)) + b"".join(cbor(x) for x in v)
    if isinstance(v, dict):
        return head(5, len(v)) + b"".join(cbor(k) + cbor(x) for k, x in v.items())
    if isinstance(v, Tagged):
        return head(6, v.tag) + cbor(v.value)
    raise TypeError(v)


def box(typ: bytes, body: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(body), typ) + body


def jumd(uuid: bytes, label: str | None, toggles: int | None = None, extra: bytes = b"") -> bytes:
    t = toggles if toggles is not None else (0x03 if label is not None else 0x00)
    body = uuid + bytes([t]) + (label.encode() + b"\x00" if label is not None else b"") + extra
    return box(b"jumd", body)


def jumb(uuid: bytes, label: str | None, *children: bytes, **kw) -> bytes:
    return box(b"jumb", jumd(uuid, label, **kw) + b"".join(children))


def salted(label: str) -> bytes:
    """A description box the way the SDK writes it: label plus a private 'c2sh' salt box."""
    return jumd(jumbf.UUID_CBOR, label, toggles=0x13, extra=box(b"c2sh", bytes(range(16))))


def assertion(label: str, value) -> bytes:
    return box(b"jumb", salted(label) + box(b"cbor", cbor(value)))


def store(
    assertions=None,
    claim=None,
    signature=None,
    *,
    manifest_extra: bytes = b"",
    store_extra: bytes = b"",
    claims: int = 1,
) -> bytes:
    assertions = {"org.test.a": {"x": 1}} if assertions is None else assertions
    claim = {"alg": "sha256", "dc:title": "t"} if claim is None else claim
    signature = (
        Tagged(18, [b"\xa1\x01\x26", {"pad": bytes(16)}, None, b"\x01" * 64]) if signature is None else signature
    )
    assertion_box = jumb(jumbf.UUID_ASSERTIONS, "c2pa.assertions", *(assertion(k, v) for k, v in assertions.items()))
    claim_box = jumb(jumbf.UUID_CLAIM, "c2pa.claim.v2", box(b"cbor", cbor(claim)))
    sig_box = jumb(jumbf.UUID_SIGNATURE, "c2pa.signature", box(b"cbor", cbor(signature)))
    children = [assertion_box, *([claim_box] * claims), sig_box]
    if manifest_extra:
        children.append(manifest_extra)
    manifest = jumb(jumbf.UUID_MANIFEST, "urn:c2pa:1234", *children)
    return jumb(jumbf.UUID_STORE, "c2pa", manifest) + store_extra


# --------------------------------------------------------------------------
# CBOR
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        0,
        23,
        24,
        255,
        256,
        65535,
        65536,
        2**32,
        2**63,
        -1,
        -24,
        -25,
        -1000,
        b"",
        b"\x00" * 300,
        "",
        "héllo",
        [],
        [1, [2, [3]]],
        {},
        {"a": 1, 2: b"x", -3: None},
        True,
        False,
        None,
        Tagged(18, [b"a"]),
    ],
)
def test_cbor_round_trip(value):
    assert jumbf.cbor_decode(cbor(value)) == value


def test_cbor_floats_and_undefined():
    assert jumbf.cbor_decode(bytes.fromhex("fb3ff8000000000000")) == 1.5  # double
    assert jumbf.cbor_decode(bytes.fromhex("fa3fc00000")) == 1.5  # single
    assert jumbf.cbor_decode(bytes.fromhex("f93e00")) == 1.5  # half
    assert jumbf.cbor_decode(b"\xf7") is jumbf.UNDEFINED


@pytest.mark.parametrize(
    "raw, why",
    [
        (b"\x9f\x01\xff", "indefinite array"),
        (b"\xbf\x01\x02\xff", "indefinite map"),
        (b"\x5f\x41a\xff", "indefinite byte string"),
        (b"\x01\x02", "trailing bytes"),
        (b"\x18", "truncated argument"),
        (b"\x43ab", "truncated byte string"),
        (b"\x62\xc3\x28", "text that is not UTF-8"),
        (b"\xa2\x01\x01\x01\x02", "duplicate map key"),
        (b"\x9b" + b"\xff" * 8, "absurd array length"),
        (b"\xf8\x20", "unsupported simple value"),
        (b"\x81" * 40 + b"\x00", "nested too deeply"),
        (b"\xa1\x80\x01", "array as a map key"),
        (b"", "empty"),
    ],
)
def test_cbor_rejects(raw, why):
    with pytest.raises(JumbfError):
        jumbf.cbor_decode(raw)


# --------------------------------------------------------------------------
# JUMBF boxes
# --------------------------------------------------------------------------


def test_a_well_formed_store_parses_and_exposes_what_the_signature_covers():
    s = jumbf.parse_store(store(claim={"alg": "sha256", "dc:title": "t"}))
    (m,) = s.manifests
    assert m.label == "urn:c2pa:1234"
    assert m.assertions == {"org.test.a": {"x": 1}}
    assert m.claim == {"alg": "sha256", "dc:title": "t"} and jumbf.cbor_decode(m.claim_bytes) == m.claim
    assert m.signature.tag == 18


def test_json_assertions_are_supported():
    body = b'{"a": [1, 2]}'
    a = box(b"jumb", jumd(jumbf.UUID_JSON, "org.test.json", 0x03) + box(b"json", body))
    claim = jumb(jumbf.UUID_CLAIM, "c2pa.claim.v2", box(b"cbor", cbor({})))
    sig = jumb(jumbf.UUID_SIGNATURE, "c2pa.signature", box(b"cbor", cbor(Tagged(18, [b"", {}, None, b"s"]))))
    m = jumb(jumbf.UUID_MANIFEST, "urn:c2pa:j", jumb(jumbf.UUID_ASSERTIONS, "c2pa.assertions", a), claim, sig)
    s = jumbf.parse_store(jumb(jumbf.UUID_STORE, "c2pa", m))
    assert s.manifests[0].assertions == {"org.test.json": {"a": [1, 2]}}


@pytest.mark.parametrize(
    "raw",
    [
        box(b"jumb", box(b"free", b"hidden payload")),  # superbox without a description box
        box(b"jumb", jumd(jumbf.UUID_STORE, "c2pa")[:-3]),  # truncated description
        box(b"jumb", jumd(jumbf.UUID_STORE, "c2pa") + b"\x00\x00\x00"),  # truncated child header
        struct.pack(">I4s", 99, b"jumb") + b"short",  # box overruns the buffer
        box(b"jumb", box(b"jumd", jumbf.UUID_STORE + b"\x03no-terminator")),
        box(b"jumb", box(b"jumd", jumbf.UUID_STORE[:10])),  # description too short
        box(b"jumb", box(b"jumd", jumbf.UUID_STORE + b"\x03c2pa\x00" + b"stray")),  # bytes after the label
        box(b"jumb", jumd(jumbf.UUID_STORE, "c2pa", toggles=0x13, extra=box(b"c2sh", b"short"))),  # salt too short
        box(
            b"jumb", jumd(jumbf.UUID_STORE, "c2pa", toggles=0x13, extra=box(b"evil", bytes(16)))
        ),  # private box of another kind
        box(b"jumb", jumd(jumbf.UUID_STORE, "c2pa", toggles=0x13, extra=box(b"c2sh", bytes(16)) + b"\x00")),
    ],
)
def test_malformed_boxes_are_rejected(raw):
    with pytest.raises(JumbfError):
        jumbf.parse_boxes(raw)


def test_boxes_nested_too_deeply_are_rejected():
    raw = jumb(jumbf.UUID_STORE, "c2pa")
    for _ in range(jumbf.MAX_DEPTH + 2):
        raw = jumb(jumbf.UUID_STORE, "c2pa", raw)
    with pytest.raises(JumbfError, match="deeply"):
        jumbf.parse_boxes(raw)


def test_oversized_store_is_rejected(monkeypatch):
    monkeypatch.setattr(jumbf, "MAX_STORE", 100)
    with pytest.raises(JumbfError, match="too large"):
        jumbf.parse_boxes(b"\x00" * 101)


# --------------------------------------------------------------------------
# The manifest store: shape and free space
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"claims": 2}, "more than one claim"),
        ({"store_extra": box(b"free", b"x" * 40)}, "not a C2PA manifest store"),
        ({"manifest_extra": box(b"free", b"tail data")}, "unexpected box"),
        ({"manifest_extra": jumb(jumbf.UUID_CBOR, "c2pa.evil", box(b"cbor", cbor(1)))}, "unexpected superbox"),
        ({"claim": [1, 2]}, "not a map"),
        ({"signature": Tagged(17, [b"", {}, None, b""])}, "COSE_Sign1"),
        ({"signature": Tagged(18, [b"", {}, None])}, "malformed COSE"),
        ({"signature": b"plain bytes"}, "COSE_Sign1"),
    ],
)
def test_store_shape_violations(kwargs, match):
    with pytest.raises(JumbfError, match=match):
        jumbf.parse_store(store(**kwargs))


def test_duplicate_assertion_labels_are_rejected():
    a = assertion("org.test.a", {"x": 1})
    asr = jumb(jumbf.UUID_ASSERTIONS, "c2pa.assertions", a, a)
    claim = jumb(jumbf.UUID_CLAIM, "c2pa.claim.v2", box(b"cbor", cbor({})))
    sig = jumb(jumbf.UUID_SIGNATURE, "c2pa.signature", box(b"cbor", cbor(Tagged(18, [b"", {}, None, b"s"]))))
    raw = jumb(jumbf.UUID_STORE, "c2pa", jumb(jumbf.UUID_MANIFEST, "urn:c2pa:d", asr, claim, sig))
    with pytest.raises(JumbfError, match="duplicate assertion"):
        jumbf.parse_store(raw)


def test_a_manifest_without_a_signature_or_claim_is_rejected():
    asr = jumb(jumbf.UUID_ASSERTIONS, "c2pa.assertions", assertion("org.test.a", 1))
    raw = jumb(jumbf.UUID_STORE, "c2pa", jumb(jumbf.UUID_MANIFEST, "urn:c2pa:x", asr))
    with pytest.raises(JumbfError, match="without a claim or a signature"):
        jumbf.parse_store(raw)
    with pytest.raises(JumbfError, match="no manifest"):
        jumbf.parse_store(jumb(jumbf.UUID_STORE, "c2pa"))


def test_an_assertion_that_is_not_cbor_or_json_is_rejected():
    bad = box(b"jumb", salted("org.test.bad") + box(b"free", b"hidden"))
    asr = jumb(jumbf.UUID_ASSERTIONS, "c2pa.assertions", bad)
    claim = jumb(jumbf.UUID_CLAIM, "c2pa.claim.v2", box(b"cbor", cbor({})))
    sig = jumb(jumbf.UUID_SIGNATURE, "c2pa.signature", box(b"cbor", cbor(Tagged(18, [b"", {}, None, b"s"]))))
    with pytest.raises(JumbfError, match="not a CBOR box"):
        jumbf.parse_store(jumb(jumbf.UUID_STORE, "c2pa", jumb(jumbf.UUID_MANIFEST, "urn:c2pa:b", asr, claim, sig)))


def test_trailing_bytes_inside_a_cbor_box_are_rejected():
    bad = box(b"jumb", salted("org.test.t") + box(b"cbor", cbor({"x": 1}) + b"\x00\x00hidden"))
    asr = jumb(jumbf.UUID_ASSERTIONS, "c2pa.assertions", bad)
    claim = jumb(jumbf.UUID_CLAIM, "c2pa.claim.v2", box(b"cbor", cbor({})))
    sig = jumb(jumbf.UUID_SIGNATURE, "c2pa.signature", box(b"cbor", cbor(Tagged(18, [b"", {}, None, b"s"]))))
    with pytest.raises(JumbfError, match="unexplained"):
        jumbf.parse_store(jumb(jumbf.UUID_STORE, "c2pa", jumb(jumbf.UUID_MANIFEST, "urn:c2pa:b", asr, claim, sig)))


@pytest.mark.parametrize("where", ["claim", "signature", "assertion"])
def test_padding_must_be_zero_bytes(where):
    zero, dirty = bytes(24), bytes(23) + b"\x01"
    kw = {
        "claim": lambda pad: {"claim": {"alg": "sha256", "pad": pad}},
        "signature": lambda pad: {"signature": Tagged(18, [b"", {"pad": pad}, None, b"s"])},
        "assertion": lambda pad: {"assertions": {"c2pa.hash.data": {"hash": b"h", "pad": pad}}},
    }[where]
    jumbf.parse_store(store(**kw(zero)))  # all-zero padding is the SDK's own reserve space
    with pytest.raises(JumbfError, match="padding"):
        jumbf.parse_store(store(**kw(dirty)))
    with pytest.raises(JumbfError, match="padding"):
        jumbf.parse_store(store(**kw("text, not bytes")))


# --------------------------------------------------------------------------
# JPEG: packets of the JUMBF box
# --------------------------------------------------------------------------


def app11(en: int, z: int, payload: bytes) -> bytes:
    body = b"JP" + struct.pack(">HI", en, z) + payload
    return b"\xff\xeb" + struct.pack(">H", len(body) + 2) + body


def jpeg_with(*segments: bytes) -> bytes:
    return b"\xff\xd8" + b"".join(segments) + b"\xff\xda\x00\x02\xff\xd9"


def test_jpeg_packets_are_reassembled_in_order():
    raw = store()
    cut = len(raw) // 2
    first = raw[:cut]
    second = raw[:8] + raw[cut:]  # later packets repeat the 8-byte box header
    data = jpeg_with(app11(1, 2, second), app11(1, 1, first))  # stored out of order on purpose
    assert jumbf.extract_jpeg(data) == raw


@pytest.mark.parametrize(
    "segments, match",
    [
        ((), "no manifest store"),
        ((app11(1, 1, b"a" * 20), app11(2, 1, b"b" * 20)), "more than one"),
        ((app11(1, 1, b"a" * 20), app11(1, 3, b"b" * 20)), "missing or out of order"),
    ],
)
def test_jpeg_packet_problems(segments, match):
    with pytest.raises(JumbfError, match=match):
        jumbf.extract_jpeg(jpeg_with(*segments))


# --------------------------------------------------------------------------
# Real containers
# --------------------------------------------------------------------------


def test_every_signable_container_yields_a_parsable_store(signed_samples):
    for fmt, (_clean, signed, _ev) in signed_samples.items():
        parsed = jumbf.parse_store(jumbf.extract_store(signed, fmt))
        assert len(parsed.manifests) == 1, fmt
        assert "org.imgstrip.sanitization" in parsed.manifests[0].assertions, fmt


def test_unsigned_files_have_no_store(signed_samples):
    for fmt, (clean, _signed, _ev) in signed_samples.items():
        with pytest.raises(JumbfError, match="no manifest store"):
            jumbf.extract_store(clean, fmt)


def test_formats_without_a_container_for_credentials_are_refused():
    with pytest.raises(JumbfError, match="cannot carry"):
        jumbf.extract_store(b"BM" + b"\x00" * 60, "BMP")


def test_two_stores_in_one_file_are_refused(signed_samples):
    _clean, png, _ = signed_samples["PNG"]
    pos = png.index(b"caBX") - 4
    (length,) = struct.unpack_from(">I", png, pos)
    chunk = png[pos : pos + 12 + length]
    doubled = png[:pos] + chunk + chunk + png[pos + len(chunk) :]
    with pytest.raises(JumbfError, match="more than one"):
        jumbf.extract_png(doubled)

    _clean, webp, _ = signed_samples["WEBP"]
    pos = webp.index(b"C2PA")
    (size,) = struct.unpack_from("<I", webp, pos + 4)
    chunk = webp[pos : pos + 8 + size + (size & 1)]
    doubled = bytearray(webp[:pos] + chunk + chunk + webp[pos + len(chunk) :])
    struct.pack_into("<I", doubled, 4, len(doubled) - 8)
    with pytest.raises(JumbfError, match="more than one"):
        jumbf.extract_webp(bytes(doubled))


def test_bmff_credential_box_layout_is_checked(signed_samples):
    fmt = "AVIF" if "AVIF" in signed_samples else "HEIF"
    _clean, data, _ = signed_samples[fmt]
    marker = data.index(b"manifest\x00")
    for offset, patch, match in (
        (marker - 4, b"\x00\x00\x00\x01", "version"),  # FullBox version/flags
        (marker, b"manifesT\x00", "layout"),  # purpose string
        (marker + 9, b"\x00\x00\x00\x00\x00\x00\x00\x07", "layout"),  # a Merkle offset we do not support
    ):
        bad = data[:offset] + patch + data[offset + len(patch) :]
        with pytest.raises(JumbfError, match=match):
            jumbf.extract_bmff(bad)


def test_a_truncated_store_is_an_error_not_a_crash(signed_samples):
    _clean, png, _ = signed_samples["PNG"]
    raw = jumbf.extract_store(png, "PNG")
    for cut in (5, 40, len(raw) // 2, len(raw) - 3):
        with pytest.raises(JumbfError):
            jumbf.parse_store(raw[:cut])
