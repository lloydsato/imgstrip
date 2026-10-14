"""--jpeg-lossless keeps the compressed image data bit-for-bit."""

import io

import numpy as np
import pytest
from conftest import assert_clean
from PIL import Image

from imgstrip import Options, StripError, fixtures, inspect_bytes, strip_image
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
