"""Fault injection between the encoder and the verifier: the review's experiment (Section 5.4), kept as tests.

The encoder's output is replaced by a corrupted version before verification, leaving everything after that point
untouched, so these tests exercise the production verifier.  Every fault here was accepted by 1.2.4:

* F1: a JPEG quantisation or Huffman table that nothing uses, and 0xFF fill bytes, were not accounted for by the
  structural walk (stage 1).
* F2: for lossy formats the pixel comparison (stage 2) was a threshold on the mean error, so a black rectangle over
  2 % of the picture, or 1 % of its pixels replaced, passed.  It now also bounds the worst 8 x 8 block.
"""

from __future__ import annotations

import io
import struct

import numpy as np
import pytest
from PIL import Image

from imgstrip import core, fixtures, inspect_bytes, jpeg
from imgstrip.core import Options, VerificationError, strip_image


def photo_png(w: int = 96, h: int = 64) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(fixtures.photo(w, h)).save(buf, "PNG")
    return buf.getvalue()


def photo_jpeg(w: int = 96, h: int = 64, **kw) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(fixtures.photo(w, h)).save(buf, "JPEG", quality=90, **kw)
    return buf.getvalue()


def segment(marker: int, payload: bytes) -> bytes:
    return bytes((0xFF, marker)) + struct.pack(">H", len(payload) + 2) + payload


def before_scan(extra: bytes):
    return lambda d: d.replace(b"\xff\xda", extra + b"\xff\xda", 1)


UNUSED_DQT = segment(0xDB, bytes((3,)) + bytes(range(1, 65)))
UNUSED_DHT = segment(0xC4, b"\x13" + bytes((0, 1)) + bytes(14) + b"\x00")


def fill_inside_scan(d: bytes) -> bytes:
    """An extra 0xFF before a stuffed data byte (FF 00) inside the entropy-coded data: libjpeg skips it."""
    start = d.index(b"\xff\xda")
    i = d.index(b"\xff\x00", start)
    return d[:i] + b"\xff" + d[i:]


JPEG_FAULTS = {
    "unused quantisation table": (before_scan(UNUSED_DQT), "Unused quantisation table"),
    "unused Huffman table": (before_scan(UNUSED_DHT), "Unused Huffman table"),
    "Huffman table redefined before use": (
        lambda d: d[:2] + segment(0xC4, b"\x00\x00\x01" + bytes(14) + b"\x00") + d[2:],
        "Unused Huffman table",
    ),
    "fill bytes before the scan": (lambda d: d.replace(b"\xff\xda", b"\xff\xff\xff\xda", 1), "Fill bytes"),
    "fill bytes before the end": (lambda d: d[:-2] + b"\xff" * 4 + d[-2:], "Fill bytes"),
    "fill bytes inside the scan": (fill_inside_scan, "Fill bytes"),
}


def inject_after_encode(monkeypatch, fault):
    real = core._Job._encode

    def encode(self, frames, out, loop, src):
        data, expected, exact = real(self, frames, out, loop, src)
        return fault(data), expected, exact

    monkeypatch.setattr(core._Job, "_encode", encode)


def noisy_photo_png(w=160, h=120) -> bytes:
    """Enough texture that the entropy-coded data holds stuffed FF 00 bytes."""
    arr = np.clip(fixtures.photo(w, h).astype(int) + np.random.default_rng(5).integers(-40, 41, (h, w, 3)), 0, 255)
    buf = io.BytesIO()
    Image.fromarray(arr.astype(np.uint8)).save(buf, "PNG")
    return buf.getvalue()


@pytest.mark.parametrize("fault", list(JPEG_FAULTS))
def test_default_mode_refuses_unaccounted_jpeg_structure(monkeypatch, fault):
    inject, label = JPEG_FAULTS[fault]
    source = noisy_photo_png()
    clean = strip_image(source, Options(format="JPEG")).data
    corrupted = inject(clean)
    assert corrupted != clean
    assert np.array_equal(np.asarray(Image.open(io.BytesIO(corrupted))), np.asarray(Image.open(io.BytesIO(clean)))), (
        "the fault must leave the picture alone, so that only the structural walk can notice it"
    )
    inject_after_encode(monkeypatch, inject)
    with pytest.raises(VerificationError, match=label):
        strip_image(source, Options(format="JPEG"))


@pytest.mark.parametrize(
    "mode", [{"jpeg_lossless": True}, {"scrub_dct": "canonical"}, {"scrub_dct": "full", "dct_min_psnr": 0}]
)
@pytest.mark.parametrize("fault", ["unused quantisation table", "unused Huffman table", "fill bytes before the scan"])
def test_the_jpeg_modes_refuse_it_too(monkeypatch, in_process, mode, fault):
    inject, label = JPEG_FAULTS[fault]
    real = jpeg.assemble  # every JPEG mode builds its file around the scans here (bit-exact mode in the decode stage)
    monkeypatch.setattr(jpeg, "assemble", lambda *a, **k: inject(real(*a, **k)))
    with pytest.raises(VerificationError, match=label):
        strip_image(photo_jpeg(), Options(**mode))


@pytest.mark.parametrize("fault", list(JPEG_FAULTS))
def test_an_input_carrying_them_is_reported_and_cleaned(fault):
    inject, label = JPEG_FAULTS[fault]
    source = inject(
        photo_jpeg(160, 120)
        if fault != "fill bytes inside the scan"
        else strip_image(noisy_photo_png(), Options(format="JPEG")).data
    )
    assert any(label in f.label for f in inspect_bytes(source).findings)
    for options in (Options(), Options(scrub_dct="canonical")):
        result = strip_image(source, options)
        assert any(label in f.label for f in result.removed)
        assert inspect_bytes(result.data).clean


@pytest.mark.parametrize(
    "kw", [{}, {"optimize": True}, {"progressive": True}, {"subsampling": 0}, {"quality": 40, "progressive": True}]
)
@pytest.mark.parametrize("mode", ["RGB", "L", "CMYK"])
def test_what_libjpeg_writes_uses_every_table_it_defines(kw, mode):
    """No false alarm: libjpeg writes only the tables its scans use, with no fill bytes."""
    buf = io.BytesIO()
    Image.fromarray(fixtures.photo(64, 48)).convert(mode).save(buf, "JPEG", **kw)
    report = inspect_bytes(buf.getvalue())
    assert report.clean, report.findings


def test_tables_used_by_a_later_component_count_as_used():
    """Progressive files redefine tables between scans; a table latched by a component scanned later is used."""
    data = photo_jpeg(120, 90, progressive=True)
    assert data.count(b"\xff\xc4") > 3
    assert inspect_bytes(data).clean
    for options in (Options(), Options(jpeg_lossless=True), Options(scrub_dct="canonical")):
        assert inspect_bytes(strip_image(data, options).data).clean


def test_fill_inside_the_scan_is_read_the_way_libjpeg_reads_it():
    """Found while fixing F1: imgstrip's coefficient reader read FF FF 00 as two data bytes where libjpeg reads one
    (and the scan walker ended the scan there).  Now both follow libjpeg, and the fill counts as slack."""
    from imgstrip import dct

    clean = strip_image(noisy_photo_png(), Options(format="JPEG")).data
    filled = fill_inside_scan(clean)
    assert dct.visible_equal(dct.read(filled), dct.read(clean))
    assert dct.read(filled).slack == 1 and dct.read(clean).slack == 0
    sos = filled.index(b"\xff\xda")
    assert jpeg._scan_end(filled, sos + 2 + int.from_bytes(filled[sos + 2 : sos + 4], "big")) == len(filled) - 2
    with pytest.raises(core.StripError, match="no decoder uses"):
        strip_image(filled, Options(jpeg_lossless=True))


# -- F2: lossy formats -------------------------------------------------------------------------------------------


def _rectangle(share):
    def fault(arr):
        h, w = arr.shape[:2]
        sh, sw = max(1, round(h * share**0.5)), max(1, round(w * share**0.5))
        out = arr.copy()
        out[(h - sh) // 2 : (h - sh) // 2 + sh, (w - sw) // 2 : (w - sw) // 2 + sw, :3] = 0
        return out

    return fault


def _random_pixels(arr):
    rng = np.random.default_rng(1)
    out = arr.copy()
    flat = out.reshape(-1, out.shape[-1])
    pick = rng.choice(len(flat), len(flat) // 100, replace=False)
    flat[pick, :3] = rng.integers(0, 256, (len(pick), 3))
    return out


PIXEL_FAULTS = {
    "1% of the area blacked out": _rectangle(0.01),
    "2% of the area blacked out": _rectangle(0.02),
    "1% of the pixels random": _random_pixels,
}


def _lossy_sources():
    from imgstrip.core import heif_available
    from PIL import features

    core._register_plugins()  # Pillow writes HEIF only once pillow-heif is registered
    picture = fixtures.photo(301, 201)
    yield "JPEG", photo_jpeg(301, 201)
    for fmt in ("WEBP", "AVIF", "HEIF"):
        if (fmt == "AVIF" and not features.check("avif")) or (fmt == "HEIF" and not heif_available()):
            continue
        buf = io.BytesIO()
        Image.fromarray(picture).save(buf, fmt, quality=90)
        yield fmt, buf.getvalue()


LOSSY = list(_lossy_sources())


def inject_pixel_fault(monkeypatch, fault):
    """Change the pixels the encoder was given and encode them again with the same settings: the container stays
    valid and the verifier still compares with the original pixels (the review's pixel faults)."""
    real = core._Job._encode

    def encode(self, frames, out, loop, src):
        data, expected, exact = real(self, frames, out, loop, src)
        bad = core.Frame(image=Image.fromarray(fault(np.asarray(expected[0].image))), duration=expected[0].duration)
        return real(self, [bad], out, loop, src)[0], expected, exact

    monkeypatch.setattr(core._Job, "_encode", encode)


@pytest.mark.parametrize("fault", list(PIXEL_FAULTS))
@pytest.mark.parametrize("fmt, source", LOSSY, ids=[f for f, _ in LOSSY])
def test_a_local_change_to_a_lossy_output_is_refused(monkeypatch, fmt, source, fault):
    assert strip_image(source, Options()).output_format == fmt  # the control is accepted
    inject_pixel_fault(monkeypatch, PIXEL_FAULTS[fault])
    with pytest.raises(VerificationError, match="8x8 block"):
        strip_image(source, Options())


@pytest.mark.parametrize("sigma", [0, 10, 20, 30, 40, 50, 60])
@pytest.mark.parametrize("quality", [95, 75])
def test_noisy_photographs_are_still_accepted(sigma, quality):
    """No new false refusals: the review's side test (Gaussian noise up to sigma 60, saved at q95 or q75)."""
    rng = np.random.default_rng(sigma)
    arr = np.clip(fixtures.photo(320, 240).astype(float) + rng.normal(0, sigma, (240, 320, 3)), 0, 255)
    buf = io.BytesIO()
    Image.fromarray(arr.astype(np.uint8)).save(buf, "JPEG", quality=quality)
    for options in (Options(), Options(format="WEBP"), Options(quality=50)):
        strip_image(buf.getvalue(), options)


def test_the_block_bound_never_tightens_as_quality_falls():
    for fmt, subsampled in core._BLOCK_MEASURED:
        previous = None
        for q in range(100, 0, -1):
            bound = core.block_tolerance(fmt, q, subsampled)
            if previous is not None:
                assert bound.luma >= previous.luma and bound.chroma >= previous.chroma, (fmt, subsampled, q)
            previous = bound


def test_the_block_error_names_the_block_that_moved():
    want = np.full((32, 48, 3), 128, np.uint8)
    got = want.copy()
    got[16:24, 40:48] = 0
    blocks = core.BlockError()
    blocks.add(got[:16], want[:16], False, 0)
    blocks.add(got[16:], want[16:], False, 16)
    assert blocks.luma_at == (40, 16) and blocks.luma == pytest.approx(128, abs=0.5)
    assert blocks.chroma == pytest.approx(0, abs=0.01)
