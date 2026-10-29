"""Content Credentials: signing, the fail-closed checks, what is recorded, and what never is."""

from __future__ import annotations

import datetime as dt
import hashlib
import inspect as pyinspect
import io
import re
import struct
import threading
import zlib

import numpy as np
import pytest
from PIL import Image

from c2pa_support import (
    Pki,
    assert_one_credential_and_nothing_else,
    edit_intent,
    foreign_credential,
    imgstrip_definition,
    photo_bytes,
    raw_sign,
    sensor_photo,
    smooth,
)
from imgstrip import codecs, fixtures, jumbf
from imgstrip.core import Options, canonical_frames, heif_available, strip_image
from imgstrip.inspect import inspect_bytes, is_c2pa_finding

# A moment inside every test certificate's validity window (they are issued "now"), with seconds
# that must never reach a manifest. Pinning a calendar date instead would fail once the clock passed it.
FIXED_NOW = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).replace(
    hour=13, minute=37, second=59, microsecond=0
)


def run(creds, source, options, identity, config=None):
    result = strip_image(source, options)
    evidence = creds.gather_evidence(source, result, options)
    signed = creds.sign(result.data, evidence, identity, config)
    return result, evidence, signed


def store_of(data: bytes, fmt: str):
    return jumbf.parse_store(jumbf.extract_store(data, fmt))


def strings_in(value):
    """Every text and byte string anywhere inside decoded manifest content."""
    if isinstance(value, (str, bytes)):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from strings_in(k)
            yield from strings_in(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from strings_in(v)
    elif isinstance(value, jumbf.Tagged):
        yield from strings_in(value.value)


def exif_segment() -> bytes:
    """A well-formed APP1 EXIF segment holding one tag (what 'metadata slipped in' looks like)."""
    exif = Image.Exif()
    exif[0x010F] = "SOMETHING"
    payload = exif.tobytes()
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


def same_pixels(a: bytes, b: bytes) -> bool:
    fa, fb = canonical_frames(a), canonical_frames(b)
    return len(fa) == len(fb) and all(
        x.shape == y.shape and x.dtype == y.dtype and np.array_equal(x, y) for x, y in zip(fa, fb, strict=True)
    )


# --------------------------------------------------------------------------
# The inspector and the canonical decode that the checks rest on
# --------------------------------------------------------------------------


def test_every_container_reports_the_credential_as_one_c2pa_item(signed_samples):
    assert {"JPEG", "PNG", "WEBP", "GIF"} <= set(signed_samples)
    for fmt, (clean, signed, _ev) in signed_samples.items():
        assert not any(is_c2pa_finding(f) for f in inspect_bytes(clean).findings), fmt
        assert_one_credential_and_nothing_else(signed)


def test_canonical_frames_tell_identical_from_re_encoded():
    png = photo_bytes("PNG")
    assert same_pixels(png, strip_image(png, Options()).data)
    jpg = photo_bytes("JPEG", quality=90)
    assert not same_pixels(jpg, strip_image(jpg, Options()).data)
    assert same_pixels(jpg, strip_image(jpg, Options(jpeg_lossless=True)).data)


def test_canonical_frames_ignore_hidden_colour_under_transparency_and_palette_choice(fx):
    src = fx["png_hidden_alpha.png"]
    assert same_pixels(src, strip_image(src, Options()).data)
    rgb = np.zeros((16, 16, 3), np.uint8)
    rgb[:, 8:] = (200, 30, 30)
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, "PNG")
    out = strip_image(buf.getvalue(), Options())  # the engine rewrites it as a palette image
    assert same_pixels(buf.getvalue(), out.data)


def test_canonical_frames_keep_16_bit_precision():
    arr = np.random.default_rng(3).integers(0, 65535, (24, 32, 3)).astype(np.uint16)
    src = codecs.encode_png(arr, 16, 2)
    (frame,) = canonical_frames(src)
    assert frame.dtype == np.uint16 and np.array_equal(frame, arr)
    low = arr.copy()
    low[0, 0, 0] ^= 1  # a single low-bit difference must be visible to the comparison
    assert not same_pixels(src, codecs.encode_png(low, 16, 2))


def test_canonical_frames_follow_the_engines_frame_selection(fx):
    assert len(canonical_frames(fx["gif_animated.gif"])) == strip_image(fx["gif_animated.gif"], Options()).frames
    assert len(canonical_frames(fx["mpo_depth.jpg"])) == 1  # secondary images are not part of the picture


# --------------------------------------------------------------------------
# Signing, every format
# --------------------------------------------------------------------------


def test_each_format_gets_one_valid_credential_and_unchanged_pixels(creds, signed_samples):
    for fmt, (clean, signed, evidence) in signed_samples.items():
        assert same_pixels(clean, signed), fmt
        cred = creds.read_credential(signed)
        assert cred.present and cred.imgstrip and cred.pixels_only and cred.format == fmt, fmt
        assert cred.state == "Valid" and cred.label.startswith("Unverified"), fmt
        assert cred.signer == "Test Newsroom" and cred.signer_name == "Jane Reporter", fmt
        assert cred.sanitization["sanitized"] is True and cred.sanitization["pixel_integrity"] == evidence.integrity, (
            fmt
        )
        assert cred.problems == [], (fmt, cred.problems)


def test_trust_anchors_change_only_the_reading_not_the_file(creds, signer, signed_samples):
    _clean, signed, _ = signed_samples["JPEG"]
    assert creds.read_credential(signed).state == "Valid"
    assert creds.read_credential(signed, signer.root_pem).state == "Trusted"
    assert creds.read_credential(signed, Pki("Other").root_pem).state == "Valid"


def test_the_hard_binding_is_correct_without_the_sdk(signed_samples):
    """sha256 over the file with the credential's own bytes cut out equals the signed hash."""
    for fmt in ("JPEG", "PNG", "WEBP", "GIF"):
        _clean, signed, _ = signed_samples[fmt]
        binding = store_of(signed, fmt).manifests[0].assertions["c2pa.hash.data"]
        assert binding["alg"] == "sha256" and binding["exclusions"]
        kept, pos = bytearray(), 0
        for ex in sorted(binding["exclusions"], key=lambda e: e["start"]):
            kept += signed[pos : ex["start"]]
            pos = ex["start"] + ex["length"]
        kept += signed[pos:]
        assert hashlib.sha256(bytes(kept)).digest() == binding["hash"], fmt


def test_the_signature_is_correct_without_the_sdk(creds, signer, signed_samples):
    for fmt, (_c, signed, _e) in signed_samples.items():
        m = store_of(signed, fmt).manifests[0]
        assert creds.check_cose_signature(m, signer.identity.chain) == signer.identity.leaf, fmt


def test_formats_that_cannot_carry_a_credential_say_so_and_suggest_a_fix(creds, signer):
    for fmt in ("BMP", "TIFF"):
        source = photo_bytes("PNG")
        result = strip_image(source, Options(format=fmt))
        ev = creds.gather_evidence(source, result, Options(format=fmt))
        with pytest.raises(creds.CredentialError, match=fmt + r" files cannot carry Content Credentials.*png"):
            creds.sign(result.data, ev, signer.identity)
    assert creds.unsignable_reason("JPEG") is None
    assert "cannot carry" in creds.unsignable_reason(None)


def test_animations_keep_their_frames_and_are_signed(creds, signer, fx):
    for name in ("gif_animated.gif", "webp_animated.webp", "apng_default.png"):
        result, evidence, signed = run(creds, fx[name], Options(), signer.identity)
        assert evidence.frames >= 2 and same_pixels(result.data, signed.data), name
        assert_one_credential_and_nothing_else(signed.data)


def test_a_lossy_pipeline_still_signs_exactly_what_it_verified(creds, signer):
    """Signing never re-encodes: the signed file decodes to the clean file's pixels, bit for bit."""
    jpg = photo_bytes("JPEG", quality=85)
    result, _ev, signed = run(creds, jpg, Options(), signer.identity)
    assert same_pixels(result.data, signed.data)
    assert signed.data.startswith(b"\xff\xd8")  # still a plain JPEG


def test_a_large_image_signs_in_reasonable_time(creds, signer):
    import time

    big = fixtures.photo(1600, 1200)
    buf = io.BytesIO()
    Image.fromarray(big).save(buf, "PNG")
    t = time.time()
    run(creds, buf.getvalue(), Options(), signer.identity)
    assert time.time() - t < 30


def test_concurrent_signing_is_safe(creds, signer):
    results, errors = [], []
    source = photo_bytes("PNG")

    def work():
        try:
            _r, _e, s = run(creds, source, Options(), signer.identity)
            results.append(creds.read_credential(s.data).imgstrip)
        except Exception as exc:
            errors.append(repr(exc))

    threads = [threading.Thread(target=work) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and results == [True] * 6


# --------------------------------------------------------------------------
# What the credential says about the pixels
# --------------------------------------------------------------------------


def test_lossless_formats_are_identical(creds, signer):
    _r, ev, signed = run(creds, photo_bytes("PNG"), Options(), signer.identity)
    assert ev.integrity == "identical" and ev.operations == [] and ev.psnr_db is None
    assert signed.credential.sanitization["pixel_integrity"] == "identical"
    assert "identical to the file received" in signed.credential.sanitization["statement"]


def test_a_jpeg_is_equivalent_when_re_encoded_and_identical_when_kept_bit_exact(creds, signer):
    jpg = photo_bytes("JPEG", quality=92)
    _r, ev, signed = run(creds, jpg, Options(), signer.identity)
    assert ev.integrity == "equivalent" and "re-encoded" in ev.operations and ev.psnr_db and ev.psnr_db > 30
    assert "not bit-identical" in signed.credential.sanitization["statement"]
    _r, ev, signed = run(creds, jpg, Options(jpeg_lossless=True), signer.identity)
    assert ev.integrity == "identical" and signed.credential.sanitization["pixel_integrity"] == "identical"


def test_the_sensor_fingerprint_scrub_and_the_lsb_scrub_are_declared_as_altered(creds, signer):
    buf = io.BytesIO()
    Image.fromarray(sensor_photo()).save(buf, "PNG")
    source = buf.getvalue()
    _r, ev, signed = run(creds, source, Options(scrub_prnu=True), signer.identity)
    assert ev.integrity == "altered" and "sensor-fingerprint-disrupted" in ev.operations
    assert "deliberately altered" in signed.credential.sanitization["statement"]
    _r, ev, signed = run(creds, source, Options(scrub_lsb=True), signer.identity)
    assert ev.integrity == "altered" and "lsb-scrubbed" in ev.operations


def test_removing_hidden_data_is_not_an_alteration(creds, signer, fx):
    _r, ev, _s = run(creds, fx["png_hidden_alpha.png"], Options(), signer.identity)
    assert ev.integrity == "identical"


def test_orientation_baked_into_the_pixels_is_equivalent_but_not_when_kept_as_a_tag(creds, signer):
    exif = Image.Exif()
    exif[0x0112] = 6
    buf = io.BytesIO()
    Image.fromarray(smooth(80, 48)).save(buf, "JPEG", quality=95, exif=exif)
    jpg = buf.getvalue()
    _r, ev, _s = run(creds, jpg, Options(), signer.identity)
    assert ev.integrity == "equivalent" and "orientation-applied" in ev.operations
    result, ev, signed = run(creds, jpg, Options(jpeg_lossless=True), signer.identity)
    assert ev.integrity == "identical", "bit-exact JPEG mode keeps the pixels, and only a one-tag orientation block"
    assert_orientation_block_only(signed.data)


def assert_orientation_block_only(data: bytes) -> None:
    report = inspect_bytes(data)
    assert [f.label for f in report.findings if not is_c2pa_finding(f)] == ["EXIF"]


def test_colour_conversion_and_format_conversion_are_recorded(creds, signer, fx):
    _r, ev, _s = run(creds, fx["png_16bit_adobe.png"], Options(), signer.identity)
    assert ev.integrity == "equivalent" and "colour-converted" in ev.operations
    _r, ev, _s = run(creds, fx["gif_animated.gif"], Options(format="JPEG"), signer.identity)
    assert {"format-converted", "frames-dropped", "re-encoded"} <= set(ev.operations)
    assert ev.integrity == "equivalent"


def test_sixteen_bit_pictures_are_identical_at_full_precision(creds, signer):
    arr = (np.add.outer(np.arange(40) * 900, np.arange(50) * 200)[..., None] + np.array([0, 100, 200])).astype(
        np.uint16
    )
    src = codecs.encode_png(arr, 16, 2)
    _r, ev, _s = run(creds, src, Options(), signer.identity)
    assert ev.integrity == "identical"


def test_minimal_detail_says_less_than_full(creds, signer):
    jpg = photo_bytes("JPEG", quality=92)
    for detail in ("minimal", "full"):
        _r, ev, signed = run(creds, jpg, Options(), signer.identity, creds.Config(detail=detail))
        s = signed.credential.sanitization
        manifest = store_of(signed.data, "JPEG").manifests[0]
        actions = [a["action"] for a in manifest.assertions["c2pa.actions.v2"]["actions"]]
        if detail == "minimal":
            assert "operations" not in s and "psnr_db" not in s
            assert actions == ["c2pa.opened", "c2pa.edited.metadata", "c2pa.transcoded"]
        else:
            assert s["operations"] == ["re-encoded"] and s["psnr_db"] == ev.psnr_db
            assert actions == ["c2pa.opened", "c2pa.edited.metadata", "c2pa.transcoded"]


def test_altered_pixels_always_leave_an_honest_edited_action(creds, signer):
    buf = io.BytesIO()
    Image.fromarray(sensor_photo()).save(buf, "PNG")
    _r, _ev, signed = run(creds, buf.getvalue(), Options(scrub_prnu=True), signer.identity)
    m = store_of(signed.data, "PNG").manifests[0]
    actions = [a["action"] for a in m.assertions["c2pa.actions.v2"]["actions"]]
    assert actions[:2] == ["c2pa.opened", "c2pa.edited.metadata"] and "c2pa.edited" in actions
    assert "operations" not in signed.credential.sanitization, "minimal detail does not say how"


def test_the_statement_is_generated_from_the_fields_and_scopes_itself(creds):
    for level in creds.INTEGRITY:
        text = creds.statement_text(level)
        assert text.startswith("Metadata and hidden data were removed from this image to protect its source.")
        assert "vouches for that processing only, not for the origin or authenticity of the image" in text


# --------------------------------------------------------------------------
# Dates, configuration
# --------------------------------------------------------------------------


def test_date_precision_never_goes_below_the_minute(creds, signer):
    source = photo_bytes("PNG")
    day = FIXED_NOW.strftime("%Y-%m-%d")
    for precision, expected in (("day", day), ("minute", f"{day}T13:37Z")):
        _r, _e, signed = run(
            creds, source, Options(), signer.identity, creds.Config(date_precision=precision, now=FIXED_NOW)
        )
        assert signed.credential.sanitization["date"] == expected
        # A leaked second reads "...T13:37:59Z". (Not a bare "59Z": the signing certificate's own validity times are
        # embedded as "YYMMDDHHMMSSZ", and a test identity made at second :59 ends in exactly that.)
        assert b"13:37:59" not in signed.data and b":59Z" not in signed.data
    local = dt.timezone(dt.timedelta(hours=-8))
    evening = FIXED_NOW.replace(hour=22, minute=0, second=0, tzinfo=local)  # 06:00 UTC the next day
    _r, _e, signed = run(creds, source, Options(), signer.identity, creds.Config(now=evening))
    next_day = (FIXED_NOW + dt.timedelta(days=1)).strftime("%Y-%m-%d")
    assert signed.credential.sanitization["date"] == next_day, "dates are always UTC"


def test_bad_configuration_is_refused(creds, signer):
    source = photo_bytes("PNG")
    result = strip_image(source, Options())
    ev = creds.gather_evidence(source, result, Options())
    for kw in ({"date_precision": "second"}, {"detail": "everything"}):
        with pytest.raises(creds.CredentialError):
            creds.sign(result.data, ev, signer.identity, creds.Config(**kw))


def test_an_identity_that_expired_after_loading_cannot_sign(creds, pki):
    ident = pki.issue(days=2)
    source = photo_bytes("PNG")
    result = strip_image(source, Options())
    ev = creds.gather_evidence(source, result, Options())
    later = creds.Config(now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3))
    with pytest.raises(creds.CredentialError, match="expired"):
        creds.sign(result.data, ev, ident, later)


# --------------------------------------------------------------------------
# Nothing from the original goes in
# --------------------------------------------------------------------------

CANARY_FILES = [
    "jpeg_everything.jpg",
    "png_everything.png",
    "png_stego.png",
    "png_palette.png",
    "apng_default.png",
    "webp_lossy.webp",
    "webp_animated.webp",
    "gif_animated.gif",
    "avif_everything.avif",
    "heic_everything.heic",
    "cmyk.jpg",
    "mpo_depth.jpg",
]


def test_the_canary_fixtures_really_are_loaded_with_canaries(fx):
    """So the test below cannot pass vacuously (the steganography and palette fixtures hide theirs differently)."""
    for name in (
        "jpeg_everything.jpg",
        "png_everything.png",
        "webp_lossy.webp",
        "gif_animated.gif",
        "avif_everything.avif",
    ):
        assert fixtures.CANARY in fx[name], name


@pytest.mark.parametrize("name", CANARY_FILES)
def test_no_byte_of_the_original_reaches_the_credential(creds, signer, fx, name):
    if name.endswith(".heic") and not heif_available():
        pytest.skip("pillow-heif not installed")
    source = fx[name]
    result = strip_image(source, Options())
    if result.output_format not in creds.SIGNABLE:
        pytest.skip("output cannot carry a credential")
    ev = creds.gather_evidence(source, result, Options())
    signed = creds.sign(result.data, ev, signer.identity).data
    assert fixtures.CANARY not in signed
    for m in store_of(signed, result.output_format).manifests:
        for text in [*strings_in(m.assertions), *strings_in(m.claim)]:
            low = text.lower() if isinstance(text, bytes) else text.lower().encode()
            assert b"canary" not in low
    assert_one_credential_and_nothing_else(signed)


def test_the_manifest_holds_only_the_fixed_vocabulary(creds, signer, fx):
    result, _ev, signed = run(creds, fx["jpeg_everything.jpg"], Options(), signer.identity)
    m = store_of(signed.data, "JPEG").manifests[0]
    assert set(m.assertions) == {"org.imgstrip.sanitization", "c2pa.actions.v2", "c2pa.ingredient.v3", "c2pa.hash.data"}
    assert m.assertions["c2pa.ingredient.v3"] == {
        "relationship": "parentOf",
        "dc:title": "Image as received",
        "dc:format": "image/jpeg",
        "instanceID": m.assertions["c2pa.ingredient.v3"]["instanceID"],
    }
    assert m.claim["dc:title"] == "Sanitized image"
    assert set(m.claim["claim_generator_info"]) <= {"name", "version", "org.contentauth.c2pa_rs"}
    assert signed.credential.sanitization.keys() == {
        "version",
        "statement",
        "sanitized",
        "date",
        "pixel_integrity",
        "scope",
    }


def test_there_is_no_thumbnail_anywhere(creds, signed_samples):
    for fmt, (_c, signed, _e) in signed_samples.items():
        m = store_of(signed, fmt).manifests[0]
        assert not [k for k in m.assertions if "thumbnail" in k], fmt
        assert b"c2pa.thumbnail" not in jumbf.extract_store(signed, fmt), fmt


def test_ids_are_random_each_time_and_say_nothing(creds, signer):
    source = photo_bytes("PNG")
    ids = set()
    for _ in range(3):
        _r, _e, signed = run(creds, source, Options(), signer.identity)
        m = store_of(signed.data, "PNG").manifests[0]
        ids.add(m.claim["instanceID"])
        ids.add(m.assertions["c2pa.ingredient.v3"]["instanceID"])
        ids.add(m.label)
    assert len(ids) == 9


def test_the_original_credentials_are_removed_reported_and_not_carried(creds, signer, fx):
    foreign = Pki("Camera Corp").issue(org="Camera Corp", cn="Camera 7")
    source = photo_bytes("PNG")
    with_credential = foreign_credential(foreign, source, "PNG")
    assert b"Camera Corp" in with_credential and b"48.8584" in with_credential
    result, ev, signed = run(creds, with_credential, Options(), signer.identity)
    assert [f.label for f in result.removed if is_c2pa_finding(f)]
    assert b"Camera Corp" not in signed.data and b"48.8584" not in signed.data and b"camera shot" not in signed.data
    assert len(store_of(signed.data, "PNG").manifests) == 1, "no nested manifest, no ingredient history"
    assert ev.source_credentials == {
        "present": True,
        "readable": True,
        "valid": True,
        "signer": "Camera Corp",
        "declared_ai": False,
    }
    warnings = creds.source_warnings(ev)
    assert len(warnings) == 1 and "Camera Corp" in warnings[0] and "not for where the image came from" in warnings[0]
    assert "source_declared_ai_generated" not in signed.credential.sanitization


def test_a_declaration_of_ai_generation_in_the_original_is_carried_over(creds, signer):
    foreign = Pki("Gen Corp").issue(org="Gen Corp")
    source = foreign_credential(foreign, photo_bytes("PNG"), "PNG", ai=True)
    _r, ev, signed = run(creds, source, Options(), signer.identity)
    assert ev.source_credentials["declared_ai"] is True
    assert signed.credential.sanitization["source_declared_ai_generated"] is True
    assert any("AI-generated" in w for w in creds.source_warnings(ev))
    assert creds.read_credential(signed.data).imgstrip, "the extra field is part of the allowed vocabulary"


def test_an_original_without_credentials_reports_none(creds, signer):
    _r, ev, _s = run(creds, photo_bytes("PNG"), Options(), signer.identity)
    assert ev.source_credentials is None and creds.source_warnings(ev) == []


def test_a_corrupt_credential_in_the_original_does_not_stop_cleaning(creds, signer):
    source = foreign_credential(Pki("X").issue(), photo_bytes("PNG"), "PNG")
    pos = source.index(b"caBX") - 4
    (length,) = struct.unpack_from(">I", source, pos)
    body = bytearray(source[pos + 8 : pos + 8 + length])
    body[30:60] = b"\xff" * 30  # damage the manifest store, then give the chunk a valid checksum again
    chunk = struct.pack(">I", length) + b"caBX" + bytes(body)
    chunk += struct.pack(">I", zlib.crc32(b"caBX" + bytes(body)) & 0xFFFFFFFF)
    broken = source[:pos] + chunk + source[pos + 12 + length :]
    _r, ev, signed = run(creds, broken, Options(), signer.identity)
    assert ev.source_credentials["present"] is True and ev.source_credentials["valid"] is not True
    assert signed.credential.imgstrip


# --------------------------------------------------------------------------
# Fail closed: a hostile or buggy SDK result is refused
# --------------------------------------------------------------------------


def craft(creds, fmt, identity, clean, evidence, config, now, instance, commitment, mutate=None, **kw):
    assertion = creds.build_assertion(evidence, config, now, commitment)
    definition = imgstrip_definition(fmt, assertion)
    definition["ingredients"][0]["instance_id"] = instance
    if mutate:
        mutate(definition)
    return raw_sign(
        identity, clean, fmt, definition, intent=edit_intent(), actions=creds._actions(evidence, config), **kw
    )


def hostile(monkeypatch, creds, builder):
    original = creds._sign_bytes

    def fake(clean, fmt, identity, evidence, config, *, now, instance, commitment=None):
        return builder(original, clean, fmt, identity, evidence, config, now, instance, commitment)

    monkeypatch.setattr(creds, "_sign_bytes", fake)


def attempt(creds, signer_identity, source=None, options=None):
    source = source or photo_bytes("PNG")
    options = options or Options()
    result = strip_image(source, options)
    ev = creds.gather_evidence(source, result, options)
    return creds.sign(result.data, ev, signer_identity)


def test_the_harness_itself_accepts_a_faithful_credential(creds, signer, monkeypatch):
    hostile(
        monkeypatch,
        creds,
        lambda orig, clean, fmt, ident, ev, cfg, now, inst, com: craft(
            creds, fmt, ident, clean, ev, cfg, now, inst, com
        ),
    )
    assert attempt(creds, signer.identity).credential.imgstrip


def add_assertion(label, data):
    return lambda d: d["assertions"].append({"label": label, "data": data})


def set_key(path, value):
    def apply(d):
        node = d
        for k in path[:-1]:
            node = node[k]
        node[path[-1]] = value

    return apply


def sanitization_data(d):
    return next(a["data"] for a in d["assertions"] if a["label"] == "org.imgstrip.sanitization")


HOSTILE = {
    "an extra assertion that could carry a payload": (
        add_assertion("com.evil.payload", {"x": "hidden"}),
        "unexpected assertions",
    ),
    "GPS in a metadata assertion": (
        add_assertion(
            "c2pa.metadata", {"@context": {"exif": "http://ns.adobe.com/exif/1.0/"}, "exif:GPSLatitude": "1.0"}
        ),
        "unexpected assertions",
    ),
    "a hidden field in our own assertion": (
        lambda d: sanitization_data(d).update(note="secret"),
        "does not say what was requested",
    ),
    "a changed statement": (
        lambda d: sanitization_data(d).update(statement="This image is authentic."),
        "does not say what was requested",
    ),
    "a different pixel claim": (
        lambda d: sanitization_data(d).update(pixel_integrity="altered"),
        "does not say what was requested",
    ),
    "the source's name as the ingredient title": (
        set_key(["ingredients", 0, "title"], "whistleblower.jpg"),
        "ingredient holds unexpected data",
    ),
    "extra ingredient data": (set_key(["ingredients", 0, "description"], "from Jane's phone"), "ingredient"),
    "a second ingredient": (
        lambda d: d["ingredients"].append(dict(d["ingredients"][0], title="Other")),
        "unexpected assertions|ingredient|actions",
    ),
    "another claim generator": (
        set_key(["claim_generator_info", 0, "name"], "evil-tool"),
        "claim generator is not imgstrip",
    ),
    "another title": (set_key(["title"], "IMG_0042.jpg"), "unexpected values"),
}


@pytest.mark.parametrize("why", list(HOSTILE))
def test_a_credential_with_anything_extra_is_refused(creds, signer, monkeypatch, why):
    mutate, message = HOSTILE[why]
    hostile(
        monkeypatch,
        creds,
        lambda orig, clean, fmt, ident, ev, cfg, now, inst, com: craft(
            creds, fmt, ident, clean, ev, cfg, now, inst, com, mutate
        ),
    )
    with pytest.raises(creds.CredentialError, match=message):
        attempt(creds, signer.identity)


def test_a_thumbnail_is_refused(creds, signer, monkeypatch):
    jpg = photo_bytes("JPEG", quality=92)
    hostile(
        monkeypatch,
        creds,
        lambda orig, clean, fmt, ident, ev, cfg, now, inst, com: craft(
            creds, fmt, ident, clean, ev, cfg, now, inst, com, thumbnails=True
        ),
    )
    # a thumbnail is a binary assertion: the strict parser refuses it before any shape check
    with pytest.raises(creds.CredentialError, match="malformed: unexpected box among the assertions"):
        attempt(creds, signer.identity, jpg)


def test_the_source_file_as_an_ingredient_stream_is_refused(creds, signer, monkeypatch):
    """Feeding the SDK the original would nest its manifest and metadata; the shape check catches it."""
    source = foreign_credential(Pki("Cam").issue(), photo_bytes("PNG"), "PNG")
    hostile(
        monkeypatch,
        creds,
        lambda orig, clean, fmt, ident, ev, cfg, now, inst, com: craft(
            creds, fmt, ident, clean, ev, cfg, now, inst, com, ingredient_stream=source
        ),
    )
    with pytest.raises(creds.CredentialError):
        attempt(creds, signer.identity, source)


def test_a_signature_by_a_different_chain_is_refused(creds, signer, pki, monkeypatch):
    other = pki.issue(cn="Someone else")
    hostile(
        monkeypatch,
        creds,
        lambda orig, clean, fmt, ident, ev, cfg, now, inst, com: craft(
            creds, fmt, other, clean, ev, cfg, now, inst, com
        ),
    )
    with pytest.raises(creds.CredentialError, match="different certificate chain"):
        attempt(creds, signer.identity)


def test_a_credential_over_different_pixels_is_refused(creds, signer, monkeypatch):
    arr = smooth().copy()
    arr[0, 0] = (1, 2, 3)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG")
    different = strip_image(buf.getvalue(), Options()).data
    hostile(
        monkeypatch,
        creds,
        lambda orig, clean, fmt, ident, ev, cfg, now, inst, com: craft(
            creds, fmt, ident, different, ev, cfg, now, inst, com
        ),
    )
    with pytest.raises(creds.CredentialError, match="changed the pixels"):
        attempt(creds, signer.identity)


def test_a_credential_that_slips_in_exif_is_refused(creds, signer, monkeypatch):
    jpg = photo_bytes("JPEG", quality=92)

    def with_exif(orig, clean, fmt, ident, ev, cfg, now, inst, com):
        dirty = clean[:2] + exif_segment() + clean[2:]
        return craft(creds, fmt, ident, dirty, ev, cfg, now, inst, com)

    hostile(monkeypatch, creds, with_exif)
    with pytest.raises(creds.CredentialError, match="more than the credential"):
        attempt(creds, signer.identity, jpg)


def test_unsigned_wrong_format_or_doubly_signed_output_is_refused(creds, signer, monkeypatch):
    hostile(monkeypatch, creds, lambda orig, c, fmt, ident, ev, cfg, now, inst, com: c)
    with pytest.raises(creds.CredentialError, match="exactly one credential"):
        attempt(creds, signer.identity)
    jpeg = strip_image(photo_bytes("PNG"), Options(format="JPEG")).data
    hostile(monkeypatch, creds, lambda orig, c, fmt, ident, ev, cfg, now, inst, com: jpeg)
    with pytest.raises(creds.CredentialError):
        attempt(creds, signer.identity)

    def twice(orig, c, fmt, ident, ev, cfg, now, inst, com):
        first = craft(creds, fmt, ident, c, ev, cfg, now, inst, com)
        import c2pa

        return raw_sign(
            ident,
            first,
            fmt,
            {"claim_generator_info": [{"name": "x"}], "title": "t"},
            intent=(c2pa.C2paBuilderIntent.EDIT,),
        )

    hostile(monkeypatch, creds, twice)
    with pytest.raises(creds.CredentialError):
        attempt(creds, signer.identity)


def test_a_bad_signature_is_caught_without_the_sdk(creds, signer, signed_samples):
    _c, signed, _e = signed_samples["PNG"]
    m = store_of(signed, "PNG").manifests[0]
    sign1 = m.signature.value
    forged = jumbf.Manifest(
        m.label, m.assertions, m.claim, m.claim_bytes + b"\x00", jumbf.Tagged(18, sign1), m.signature_bytes
    )
    with pytest.raises(creds.CredentialError, match="does not verify"):
        creds.check_cose_signature(forged)
    wrong_sig = jumbf.Tagged(18, [sign1[0], sign1[1], None, bytes([sign1[3][0] ^ 1]) + sign1[3][1:]])
    with pytest.raises(creds.CredentialError, match="does not verify"):
        creds.check_cose_signature(jumbf.Manifest(m.label, m.assertions, m.claim, m.claim_bytes, wrong_sig, b""))
    attached = jumbf.Tagged(18, [sign1[0], sign1[1], b"claim", sign1[3]])
    with pytest.raises(creds.CredentialError, match="detached"):
        creds.check_cose_signature(jumbf.Manifest(m.label, m.assertions, m.claim, m.claim_bytes, attached, b""))
    extra_header = jumbf.Tagged(18, [sign1[0], {**sign1[1], "smuggled": b"data"}, None, sign1[3]])
    with pytest.raises(creds.CredentialError, match="unexpected fields"):
        creds.check_cose_signature(jumbf.Manifest(m.label, m.assertions, m.claim, m.claim_bytes, extra_header, b""))
    with pytest.raises(creds.CredentialError, match="different certificate chain"):
        creds.check_cose_signature(m, Pki("zzz").issue().chain)
    not_a_chain = jumbf.Tagged(18, [sign1[0], {33: 5}, None, sign1[3]])
    with pytest.raises(creds.CredentialError, match="no certificate chain"):
        creds.check_cose_signature(jumbf.Manifest(m.label, m.assertions, m.claim, m.claim_bytes, not_a_chain, b""))


# --------------------------------------------------------------------------
# Reading credentials: honest about everything that is not ours
# --------------------------------------------------------------------------


def test_a_file_without_a_credential_reports_none(creds):
    cred = creds.read_credential(photo_bytes("PNG"))
    assert not cred.present and cred.label == "No Content Credentials" and not cred.imgstrip


def test_a_tampered_file_is_invalid_and_says_why(creds, signed_samples):
    spots = {
        "JPEG": lambda d: len(d) - 20,
        "PNG": lambda d: d.index(b"IDAT") + 12,
        "WEBP": lambda d: d.rindex(b"VP8") + 14,
        "GIF": lambda d: len(d) - 6,
    }
    for fmt, spot in spots.items():
        _c, signed, _e = signed_samples[fmt]
        broken = bytearray(signed)
        broken[spot(signed)] ^= 0x01
        cred = creds.read_credential(bytes(broken))
        assert cred.state == "Invalid" and not cred.imgstrip and cred.label == "Invalid", fmt
        assert any("mismatch" in p for p in cred.problems), (fmt, cred.problems)


def test_a_byte_hidden_in_the_manifests_padding_is_noticed_even_though_the_signature_ignores_it(creds, signed_samples):
    """The SDK stays 'Valid' (padding is outside the signature); imgstrip's strict reader still refuses the file as ours."""
    for fmt, (_c, signed, _e) in signed_samples.items():
        # the manifest's reserved space is a long run of zero bytes (GIF splits it into 255-byte sub-blocks)
        run_at = re.compile(rb"\x00{100,}").search(signed, signed.index(b"jumb"))
        assert run_at, fmt
        position = run_at.start() + 50
        tampered = bytearray(signed)
        tampered[position] = 0x41
        cred = creds.read_credential(bytes(tampered))
        assert cred.present and not cred.imgstrip and not cred.pixels_only, fmt
        assert any("padding" in n for n in cred.notes), (fmt, cred.notes)


def test_changing_what_a_credential_says_breaks_it(creds, signed_samples):
    _c, signed, _e = signed_samples["PNG"]
    assert b"identical" in signed
    # rewrite the pixel claim in place, same length: the assertion hash no longer matches
    forged = signed.replace(b"identical", b"equivalen", 1)
    assert forged != signed
    cred = creds.read_credential(forged)
    assert cred.state == "Invalid" and not cred.imgstrip


def test_adding_metadata_to_a_signed_file_is_noticed(creds, signed_samples):
    _c, signed, _e = signed_samples["JPEG"]
    dirty = signed[:2] + exif_segment() + signed[2:]
    cred = creds.read_credential(dirty)
    assert cred.state == "Invalid" and not cred.pixels_only
    assert [f.label for f in cred.other_findings] == ["EXIF"]


def test_a_foreign_credential_is_read_but_never_taken_for_ours(creds):
    foreign = Pki("Camera Corp").issue(org="Camera Corp", cn="Camera 7")
    data = foreign_credential(foreign, photo_bytes("PNG"), "PNG")
    cred = creds.read_credential(data)
    assert cred.present and cred.state == "Valid" and cred.signer == "Camera Corp"
    assert not cred.imgstrip and not cred.pixels_only and cred.sanitization is None
    assert any("not an imgstrip credential" in n for n in cred.notes)
    ai = creds.read_credential(foreign_credential(foreign, photo_bytes("PNG"), "PNG", ai=True))
    assert ai.declared_ai and not creds.read_credential(data).declared_ai


def test_a_lookalike_that_passes_the_sdk_but_not_our_rules_is_not_ours(creds, signer):
    """Right label, valid signature, plausible fields: one extra assertion is enough to disqualify it."""
    clean = strip_image(photo_bytes("PNG"), Options()).data
    source = photo_bytes("PNG")
    result = strip_image(source, Options())
    ev = creds.gather_evidence(source, result, Options())
    now = dt.datetime.now(dt.timezone.utc)
    config = creds.Config()
    for mutate in (add_assertion("com.evil.payload", {"x": 1}), lambda d: sanitization_data(d).update(extra=1)):
        data = craft(
            creds,
            "PNG",
            signer.identity,
            clean,
            ev,
            config,
            now,
            "xmp:iid:" + "0" * 8 + "-0000-4000-8000-" + "0" * 12,
            None,
            mutate,
        )
        cred = creds.read_credential(data)
        assert cred.present and cred.state in ("Valid", "Trusted") and not cred.imgstrip and not cred.pixels_only


def test_static_guarantees_no_network_features_are_ever_used():
    from imgstrip import credentials as c

    source = pyinspect.getsource(c)
    assert "tsa_url" not in source.replace("tsa_url=None", "") and "ta_url" not in source
    for needle in ("c2pa.Reader(", "c2pa.Builder("):
        for m in re.finditer(re.escape(needle), source):
            call = source[m.start() : m.start() + 160]
            assert "context=" in call or "ctx" in call, f"{needle} without our settings: {call!r}"
    settings = c.sdk_settings(b"anchors")
    assert settings["verify"]["remote_manifest_fetch"] is False and settings["verify"]["ocsp_fetch"] is False
    assert settings["core"]["allowed_network_hosts"] == [] and settings["builder"]["thumbnail"]["enabled"] is False
    assert settings["trust"]["user_anchors"] == "anchors"


def test_a_remote_manifest_link_is_never_followed(creds):
    """Positive control first: with the SDK's default settings a hostile file does make it phone home."""
    import http.server

    import c2pa

    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(404)
            self.end_headers()

        do_HEAD = do_GET

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        xmp = (
            '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about="" '
            f'xmlns:dcterms="http://purl.org/dc/terms/" dcterms:provenance="http://127.0.0.1:{port}/m.c2pa"/>'
            "</rdf:RDF></x:xmpmeta><?xpacket end='w'?>"
        ).encode()
        buf = io.BytesIO()
        Image.fromarray(np.zeros((16, 16, 3), np.uint8)).save(buf, "JPEG", xmp=xmp)
        data = buf.getvalue()
        with pytest.raises(c2pa.C2paError):
            c2pa.Reader("image/jpeg", io.BytesIO(data), context=c2pa.Context()).json()
        if not hits:
            pytest.skip("this SDK build no longer fetches by default; nothing to prove")
        hits.clear()
        with creds._context() as ctx:
            with pytest.raises(c2pa.C2paError, match="(?i)remote"):
                c2pa.Reader("image/jpeg", io.BytesIO(data), context=ctx).json()
        assert hits == [], "imgstrip's settings must stop the SDK from contacting anyone"
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------
# Auditing later: the commitment and the receipt
# --------------------------------------------------------------------------


def commit_run(creds, signer, source=None, options=None):
    source = source or photo_bytes("PNG")
    options = options or Options()
    result = strip_image(source, options)
    ev = creds.gather_evidence(source, result, options)
    signed = creds.sign(result.data, ev, signer.identity, creds.Config(commit_source=True))
    return source, ev, signed


def test_nothing_is_committed_unless_asked(creds, signer):
    _r, _e, signed = run(creds, photo_bytes("PNG"), Options(), signer.identity)
    assert signed.receipt is None and "source_commitment" not in signed.credential.sanitization


def test_the_commitment_is_keyed_so_it_does_not_link_the_published_image_to_the_original(creds, signer):
    source, ev, signed = commit_run(creds, signer)
    again = commit_run(creds, signer, source)[2]
    c1 = signed.credential.sanitization["source_commitment"]["value"]
    c2 = again.credential.sanitization["source_commitment"]["value"]
    assert c1 != c2 and signed.receipt["salt"] != again.receipt["salt"]
    for secret in (ev.source_pixels_sha256, ev.source_sha256):
        assert secret not in signed.data.hex() and bytes.fromhex(secret) not in signed.data
        assert base64_of(secret) not in signed.data
    unkeyed = hashlib.sha256(bytes.fromhex(ev.source_pixels_sha256)).hexdigest()
    assert unkeyed not in signed.data.hex() and bytes.fromhex(unkeyed) not in signed.data


def base64_of(hexdigest: str) -> bytes:
    import base64

    return base64.b64encode(bytes.fromhex(hexdigest))


def test_a_receipt_opens_the_commitment_and_confirms_identical_pixels(creds, signer):
    source, _ev, signed = commit_run(creds, signer, options=Options())
    doc = creds.receipts_document([signed.receipt])
    assert set(signed.receipt) == {
        "signed_sha256",
        "source_sha256",
        "source_pixels_sha256",
        "salt",
        "commitment",
        "pixel_integrity",
        "created",
    }
    assert signed.receipt["signed_sha256"] == hashlib.sha256(signed.data).hexdigest()
    assert signed.receipt["source_sha256"] == hashlib.sha256(source).hexdigest()
    assert "PRIVATE" in doc["warning"]
    audit = creds.audit(signed.data, source, doc)
    assert (
        audit.ok
        and audit.commitment_ok
        and audit.measured == "identical"
        and audit.claimed == "identical"
        and not audit.problems
    )


def test_an_audit_of_a_re_encoded_image_confirms_the_link_and_measures_the_difference(creds, signer):
    jpg = photo_bytes("JPEG", quality=92)
    source, _ev, signed = commit_run(creds, signer, jpg)
    audit = creds.audit(signed.data, source, [signed.receipt])  # a bare list of receipts also works
    assert audit.ok and audit.claimed == "equivalent" and audit.measured == "equivalent" and audit.psnr_db > 30


def test_an_audit_rejects_the_wrong_original(creds, signer):
    source, _ev, signed = commit_run(creds, signer)
    arr = smooth().copy()
    arr[::-1] = arr
    other = io.BytesIO()
    Image.fromarray(arr[:, ::-1].copy()).save(other, "PNG")
    audit = creds.audit(signed.data, other.getvalue(), [signed.receipt])
    assert not audit.ok and audit.commitment_ok is False and "not the original" in audit.problems[0]


def test_an_audit_needs_the_receipt_and_a_committed_credential(creds, signer):
    source, _ev, signed = commit_run(creds, signer)
    assert "no receipt matches" in creds.audit(signed.data, source, creds.receipts_document([])).problems[0]
    bad_salt = dict(signed.receipt, salt="not base64!!")
    assert "malformed" in creds.audit(signed.data, source, [bad_salt]).problems[0]
    assert "malformed" in creds.audit(signed.data, source, [dict(signed.receipt, salt=42)]).problems[0]
    wrong_salt = dict(signed.receipt, salt=base64_of("11" * 32).decode())
    assert creds.audit(signed.data, source, [wrong_salt]).commitment_ok is False
    _r, _e, plain = run(creds, source, Options(), signer.identity)
    assert "cannot be audited" in creds.audit(plain.data, source, [signed.receipt]).problems[0]
    assert not creds.audit(photo_bytes("PNG"), source, [signed.receipt]).ok


def test_an_audit_of_a_tampered_file_fails_even_with_the_right_receipt(creds, signer):
    source, _ev, signed = commit_run(creds, signer)
    broken = bytearray(signed.data)
    broken[broken.index(b"IDAT") + 12] ^= 1
    audit = creds.audit(
        bytes(broken), source, [dict(signed.receipt, signed_sha256=hashlib.sha256(bytes(broken)).hexdigest())]
    )
    assert not audit.ok and audit.credential.state == "Invalid"


def test_an_audit_catches_a_false_claim_of_identical_pixels(creds, signer):
    """Commitment opens, but the credential claimed 'identical' for a picture that differs."""
    original = photo_bytes("PNG")
    arr = smooth().copy()
    arr[10, 10] = (250, 0, 0)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG")
    published = strip_image(buf.getvalue(), Options()).data
    ev = creds.Evidence(
        integrity="identical",
        operations=[],
        psnr_db=None,
        source_pixels_sha256=creds._digest(canonical_frames(original)),
        source_sha256=hashlib.sha256(original).hexdigest(),
        frames=1,
    )
    signed = creds.sign(published, ev, signer.identity, creds.Config(commit_source=True))
    audit = creds.audit(signed.data, original, [signed.receipt])
    assert audit.commitment_ok and audit.measured == "equivalent" and audit.consistent is False and not audit.ok
    assert "identical, but the two files differ" in audit.problems[-1]


# --------------------------------------------------------------------------
# Credential.as_dict (what the web app and --json show)
# --------------------------------------------------------------------------


def test_credential_summaries_serialise_cleanly(creds, signed_samples):
    import json

    _c, signed, _e = signed_samples["PNG"]
    d = creds.read_credential(signed).as_dict()
    json.dumps(d)
    assert d["present"] and d["imgstrip"] and d["pixels_only"] and d["label"].startswith("Unverified")
    assert d["sanitization"]["scope"] == "processing" and d["problems"] == [] and d["other"] == []


# --------------------------------------------------------------------------
# Hostile input: corrupted credentials must never crash or hang the reader
# --------------------------------------------------------------------------


def _mutations(data: bytes, rng, count: int):
    """Byte flips, truncations, insertions and zeroed stretches, biased towards the credential's own bytes."""
    start = max(0, data.find(b"jumb") - 8)
    end = min(len(data), start + 4096)  # the boxes, claim and the start of the signature
    for i in range(count):
        out = bytearray(data)
        kind = i % 5
        pos = int(rng.integers(start, end)) if kind != 4 else int(rng.integers(0, len(data)))
        if kind == 0:
            out[pos] ^= int(rng.integers(1, 256))
        elif kind == 1:
            out = out[: pos + 1]
        elif kind == 2:
            out[pos:pos] = bytes(rng.integers(0, 256, int(rng.integers(1, 40))).astype(np.uint8))
        elif kind == 3:
            out[pos : pos + int(rng.integers(1, 64))] = b"\x00" * int(rng.integers(1, 64))
        else:
            out[pos] ^= 1 << int(rng.integers(0, 8))
        yield bytes(out)


def test_corrupted_credentials_are_reported_never_crash_the_reader(creds, signed_samples):
    rng = np.random.default_rng(20261015)
    seen_states = set()
    for fmt, (_clean, signed, _ev) in signed_samples.items():
        for bad in _mutations(signed, rng, 60):
            try:
                cred = creds.read_credential(bad)
            except creds.CredentialError:
                continue  # a normal, user-facing error
            seen_states.add(cred.state)
            assert not (cred.imgstrip and cred.state == "Invalid"), fmt
            if cred.imgstrip:
                assert cred.problems == [] and cred.sanitization["sanitized"] is True
            cred.as_dict()  # always serialisable
    assert "Invalid" in seen_states or None in seen_states, "the mutations must actually have broken some credentials"


def test_corrupted_credentials_never_survive_a_sign_time_check(creds, signed_samples):
    """A mutated file is never accepted as 'one valid imgstrip credential and nothing else' unless it is bit-identical."""
    rng = np.random.default_rng(7)
    for fmt, (clean, signed, _ev) in signed_samples.items():
        for bad in _mutations(signed, rng, 40):
            if bad == signed:
                continue
            try:
                cred = creds.read_credential(bad)
            except creds.CredentialError:
                continue
            clean_credential = cred.imgstrip and cred.pixels_only and cred.state in ("Valid", "Trusted")
            if clean_credential:
                # allowed only if the change fell somewhere that provably does not matter: the pixels must be the clean file's
                assert same_pixels(clean, bad), (fmt, "a changed file was accepted as a clean credential")
