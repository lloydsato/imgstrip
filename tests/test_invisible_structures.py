"""Regression tests for the legal-but-invisible structures that `check` used to report as clean (N1-N6) and for the
two false refusals found by the independent evaluation (B1, B2).

N1  suggested palette in a truecolour or RGBA PNG        N4  JPEG restart interval that no marker uses
N2  tRNS colour key with high bits / unused / all-opaque  N5  JPEG component identifiers other than 1..n
N3  empty or unusually cut IDAT chunks                    N6  GIF still image with a delay, a loop count, GIF87a
B1  valid 1-bit BMP with a white-first palette refused    B2  PNG with a very long tEXt keyword refused under the sandbox
"""

import io
import struct
import zlib

import numpy as np
import pytest
from PIL import Image

from imgstrip import Options, inspect_bytes, strip_image
from imgstrip.inspect import canonical_form_problems

RNG = np.random.default_rng(7)
PIXELS = RNG.integers(0, 256, (48, 64, 3), dtype=np.uint8)
_Y, _X = np.mgrid[0:48, 0:64]
SMOOTH = np.stack([_X * 3 % 256, _Y * 5 % 256, (_X + _Y) * 2 % 256], axis=-1).astype(np.uint8)  # lossy-friendly


def _save(img, fmt, **kw):
    buf = io.BytesIO()
    (img if isinstance(img, Image.Image) else Image.fromarray(img)).save(buf, fmt, **kw)
    return buf.getvalue()


def _chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def _png_insert(data, kind, body):
    at = data.find(b"IDAT") - 4
    return data[:at] + _chunk(kind, body) + data[at:]


def _jseg(marker, payload):
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def _gif_after_header(data):
    flags = data[10]
    return 13 + (3 * 2 ** ((flags & 7) + 1) if flags & 0x80 else 0)


def _planted():
    png = _save(PIXELS, "PNG")
    jpg = _save(SMOOTH, "JPEG", quality=90)
    gif = _save(Image.fromarray(PIXELS).quantize(16), "GIF")
    p = _gif_after_header(gif)
    return {
        "N1 PLTE in truecolour": (_png_insert(png, b"PLTE", bytes(range(256)) * 3), "Suggested palette"),
        "N2 colour key with high bits": (_png_insert(png, b"tRNS", b"\x12\x34\x56\x78\x9a\xbc"), "bits above"),
        "N3 empty IDAT": (png[:-12] + _chunk(b"IDAT", b"") + png[-12:], "Empty IDAT"),
        "N4 unused DRI": (jpg[:2] + _jseg(0xDD, struct.pack(">H", 65000)) + jpg[2:], "Restart interval"),
        "N6 still GIF with loop": (
            gif[:p] + b"\x21\xff\x0bNETSCAPE2.0\x03\x01" + struct.pack("<H", 5) + b"\x00" + gif[p:],
            "Loop count",
        ),
    }


@pytest.mark.parametrize("name", list(_planted()))
def test_check_reports_planted_structure(name):
    data, label = _planted()[name]
    labels = [f.label + " " + f.detail for f in inspect_bytes(data).findings]
    assert any(label in x for x in labels), labels


@pytest.mark.parametrize("name", list(_planted()))
def test_cleaning_removes_them_and_output_is_clean(name):
    data, _ = _planted()[name]
    out = strip_image(data, Options(sandbox=False)).data
    assert not inspect_bytes(out).findings
    assert canonical_form_problems(out, inspect_bytes(out).format) == []


def test_clean_inputs_have_no_new_findings():
    for fmt, kw in (("PNG", {}), ("JPEG", {"quality": 90}), ("GIF", {})):
        img = Image.fromarray(PIXELS).quantize(16) if fmt == "GIF" else PIXELS
        assert not [f for f in inspect_bytes(_save(img, fmt, **kw)).findings if f.category != "color"]


def test_png_idat_chunking_is_canonical_only_for_outputs():
    png = _save(PIXELS, "PNG")
    idat, pos = b"", 8
    while pos < len(png):
        length, kind = struct.unpack_from(">I4s", png, pos)
        if kind == b"IDAT":
            idat += png[pos + 8 : pos + 8 + length]
        pos += 12 + length
    pieces = [idat[k : k + 97] for k in range(0, len(idat), 97)]
    head = png[: png.find(b"IDAT") - 4]
    cut = head + b"".join(_chunk(b"IDAT", p) for p in pieces) + _chunk(b"IEND", b"")
    assert not inspect_bytes(cut).findings  # other encoders cut the stream anywhere: normal in an input
    assert canonical_form_problems(cut, "PNG")  # but not in an output


def test_jpeg_component_ids_and_gif_signature_are_canonical_only_for_outputs():
    jpg = bytearray(_save(SMOOTH, "JPEG", quality=90))
    pos = 2
    while pos < len(jpg):
        marker, length = jpg[pos + 1], struct.unpack(">H", jpg[pos + 2 : pos + 4])[0]
        if marker == 0xC0:
            for k in range(jpg[pos + 9]):
                jpg[pos + 10 + 3 * k] += 0x40
        if marker == 0xDA:
            for k in range(jpg[pos + 4]):
                jpg[pos + 5 + 2 * k] += 0x40
            break
        pos += 2 + length
    assert not inspect_bytes(bytes(jpg)).findings
    assert canonical_form_problems(bytes(jpg), "JPEG")
    gif = _save(Image.fromarray(PIXELS).quantize(16), "GIF")
    assert canonical_form_problems(b"GIF87a" + gif[6:], "GIF")
    assert canonical_form_problems(strip_image(gif, Options(sandbox=False)).data, "GIF") == []


def _bmp_with_palette(palette):
    index = (RNG.random((40, 50)) > 0.5).astype(np.uint8)
    img = Image.fromarray(index, "P")
    img.putpalette(palette)
    return _save(img, "BMP"), index


@pytest.mark.parametrize("palette", [[255, 255, 255, 0, 0, 0], [0, 0, 0, 255, 255, 255]])
def test_b1_two_colour_bmp_is_cleaned_and_keeps_its_pixels(palette):
    data, _ = _bmp_with_palette(palette)
    result = strip_image(data, Options(sandbox=False))
    before = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
    after = np.asarray(Image.open(io.BytesIO(result.data)).convert("RGB"))
    assert result.output_format == "BMP"
    assert after.shape == before.shape
    if palette[0] == 255:  # Pillow reads the white-first file correctly; the output must show the same picture
        assert np.array_equal(before, after)


def test_b2_text_keyword_cannot_make_a_finding_label_longer_than_the_sandbox_accepts():
    png = _save(PIXELS, "PNG")
    key = b"XML:com.adobe.xmp<?xpacket begin='x' " + b"A" * 3000
    for kind, body in ((b"zTXt", key + b"\x00\x00" + b"not a zlib stream"), (b"iTXt", key + b"\x00\x01\x00\x00\x00bad")):
        data = _png_insert(png, kind, body)
        labels = [f.label for f in inspect_bytes(data).findings]
        assert labels and all(len(x) <= 400 for x in labels), [len(x) for x in labels]
