"""--jpeg-lossless keeps the compressed image data bit-for-bit."""

import io

import numpy as np
import pytest
from conftest import assert_clean
from PIL import Image

from imgstrip import Options, StripError, fixtures, inspect_bytes, strip_image
from imgstrip import jpeg
from imgstrip.jpeg import orientation_exif


def _scan_bytes(data: bytes) -> bytes:
    return data[data.index(b"\xff\xda") :]


@pytest.mark.parametrize("kwargs", [{}, {"progressive": True}, {"restart_marker_blocks": 2}, {"subsampling": 0}])
def test_entropy_data_identical(kwargs):
    buf = io.BytesIO()
    Image.fromarray(fixtures.photo(90, 70)).save(buf, "JPEG", quality=88, **kwargs)
    dirty = buf.getvalue()[:2] + b"\xff\xfe\x00\x10CANARY-COMMENT" + buf.getvalue()[2:] + b"CANARY-TRAILER"
    result = strip_image(dirty, Options(jpeg_lossless=True))
    assert _scan_bytes(result.data) == _scan_bytes(buf.getvalue())
    assert np.array_equal(
        np.asarray(Image.open(io.BytesIO(result.data))), np.asarray(Image.open(io.BytesIO(buf.getvalue())))
    )
    assert_clean(result.data, ".jpg")


def test_orientation_kept_as_single_tag(fx):
    result = strip_image(fx["jpeg_everything.jpg"], Options(jpeg_lossless=True, icc="drop"))
    report = inspect_bytes(result.data)
    assert report.info["exif"] == orientation_exif(6)[6:]
    assert [f.label for f in report.findings] == ["EXIF"]
    assert Image.open(io.BytesIO(result.data)).getexif() == {274: 6}
    assert fixtures.CANARY not in result.data


def test_icc_sanitized_in_lossless_mode(fx):
    result = strip_image(fx["jpeg_everything.jpg"], Options(jpeg_lossless=True))
    report = inspect_bytes(result.data)
    assert "CANARY" not in str(report.findings)
    profile = report.info["icc"]
    assert b"CANARY" not in profile and b"APPL" not in profile and b"appl" not in profile
    assert any("rebuilt anonymous profile" in a for a in result.actions)


def test_cmyk_adobe_marker_preserved(fx):
    result = strip_image(fx["cmyk.jpg"], Options(jpeg_lossless=True))
    a = np.asarray(Image.open(io.BytesIO(fx["cmyk.jpg"])))
    b = np.asarray(Image.open(io.BytesIO(result.data)))
    assert np.array_equal(a, b)


def test_mpo_lossless_drops_secondary(fx):
    result = strip_image(fx["mpo_depth.jpg"], Options(jpeg_lossless=True))
    assert result.data.count(b"\xff\xd8") == 1
    assert b"MPF" not in result.data


def test_lossless_requires_jpeg_input(fx):
    with pytest.raises(StripError):
        strip_image(fx["png_everything.png"], Options(jpeg_lossless=True, format="jpeg"))


def test_an_appended_image_with_its_own_thumbnail_counts_once():
    # A secondary image (as in a camera's MPF file) whose EXIF block carries a thumbnail JPEG of its own.
    secondary = fixtures.jpeg_everything()
    assert secondary.count(b"\xff\xd8\xff") == 2
    _data, dropped = jpeg.rebuild(fixtures.jpeg_everything() + secondary)
    assert dropped == 1
    _data, dropped = jpeg.rebuild(fixtures.jpeg_everything() + secondary + secondary)
    assert dropped == 2


def _jpeg_with_segment(marker: int, payload: bytes) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(fixtures.photo(64, 48)).save(buf, "JPEG", quality=85)
    data = buf.getvalue()
    sos = data.index(b"\xff\xda")
    segment = bytes((0xFF, marker)) + (len(payload) + 2).to_bytes(2, "big") + payload
    return data[:sos] + segment + data[sos:]


@pytest.mark.parametrize("marker", [0xDC, 0xDE, 0xDF])
def test_lossless_drops_segments_no_decoder_uses(marker):
    # DNL/DHP/EXP are ignored or rejected by decoders, so they can only carry hidden data.
    dirty = _jpeg_with_segment(marker, b"CANARY-HIDDEN-" + b"A" * 200)
    assert b"CANARY-HIDDEN" in dirty
    rebuilt, _ = jpeg.rebuild(dirty)
    assert b"CANARY-HIDDEN" not in rebuilt
    assert marker not in {m for m in _markers(rebuilt)}


def test_lossless_drops_dac_from_huffman_frame():
    dirty = _jpeg_with_segment(0xCC, b"\x00\x11\x11\x02CANARY!!")
    rebuilt, _ = jpeg.rebuild(dirty)
    assert 0xCC not in _markers(rebuilt) and b"CANARY" not in rebuilt


def test_lossless_dac_malformed_payload_is_refused():
    with pytest.raises(jpeg.JpegError):
        jpeg.rebuild(_jpeg_with_segment(0xCC, b"\x00\x11\x22"))


def test_inspector_reports_dnl_segment():
    report = inspect_bytes(_jpeg_with_segment(0xDC, b"CANARY-HIDDEN"))
    assert any("0xFFDC" in f.label for f in report.findings)


def _markers(data: bytes) -> list[int]:
    out, pos = [], 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        out.append(marker)
        if marker == 0xDA:
            break
        pos += 2 + int.from_bytes(data[pos + 2 : pos + 4], "big")
    return out
