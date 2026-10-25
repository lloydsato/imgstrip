"""The container inspector must find everything (inputs) and fail closed (outputs)."""

import io
import struct
import zlib

import numpy as np
import pytest
from PIL import Image

from imgstrip import fixtures, inspect_bytes
from imgstrip.codecs import _pillow_lzw
from imgstrip.inspect import HIDDEN, METADATA, sniff_format


def labels(data):
    return {f.label for f in inspect_bytes(data).findings}


def test_jpeg_everything_found(fx):
    found = labels(fx["jpeg_everything.jpg"])
    for expected in (
        "EXIF",
        "GPS location",
        "Embedded EXIF thumbnail",
        "XMP",
        "Photoshop resources / IPTC",
        "JUMBF / C2PA content credentials",
        "Comment",
        "ICC color profile",
        "Data after end of JPEG image",
    ):
        assert expected in found, expected


def test_gps_coordinates_decoded(fx):
    gps = [f for f in inspect_bytes(fx["jpeg_everything.jpg"]).findings if f.label == "GPS location"][0]
    lat, lon = (float(x) for x in gps.detail.split(","))
    assert abs(lat - 37.42204) < 1e-4 and abs(lon + 122.08399) < 1e-4


def test_png_everything_found(fx):
    found = labels(fx["png_everything.png"])
    for expected in (
        "Text 'Author'",
        "Text 'Comment'",
        "XMP",
        "EXIF",
        "GPS location",
        "ICC color profile",
        "Modification time",
        "Physical pixel size / DPI",
        "Unknown chunk 'prVt'",
        "Data after IEND",
    ):
        assert expected in found, expected
    trailer = [f for f in inspect_bytes(fx["png_everything.png"]).findings if f.label == "Data after IEND"][0]
    assert "aCropalypse" in trailer.detail


def test_mpo_index_found(fx):
    found = labels(fx["mpo_depth.jpg"])
    assert "Multi-Picture index (MPF)" in found
    assert any(lbl.startswith("Data after end of JPEG") for lbl in found)


def test_gif_webp_tiff_bmp_avif_found(fx):
    assert {"Comment", "XMP", "Data after GIF trailer"} <= labels(fx["gif_animated.gif"])
    assert {"EXIF", "XMP", "ICC color profile", "Data after RIFF container"} <= labels(fx["webp_lossy.webp"])
    tiff = labels(fx["tiff_multipage.tif"])
    assert {"Image description", "Camera make", "Software", "Artist", "XMP", "Copyright"} <= tiff
    assert "Reduced-resolution image (thumbnail/preview) (page 3)" in tiff
    assert "Data after pixel data" in labels(fx["bmp_trailer.bmp"])
    assert {"EXIF", "XMP"} <= labels(fx["avif_everything.avif"])


def test_heic_found(fx):
    if "heic_everything.heic" not in fx:
        pytest.skip("pillow-heif not installed")
    assert {"EXIF", "XMP", "GPS location"} <= labels(fx["heic_everything.heic"])


def test_extra_data_inside_png_zlib_stream():
    buf = io.BytesIO()
    Image.new("L", (4, 4), 7).save(buf, "PNG")
    data = buf.getvalue()
    start = data.index(b"IDAT") - 4
    (length,) = struct.unpack_from(">I", data, start)
    raw = zlib.decompress(data[start + 8 : start + 8 + length])
    evil = zlib.compress(raw + b"\x00HIDDEN ROWS" * 10)
    chunk = struct.pack(">I", len(evil)) + b"IDAT" + evil + struct.pack(">I", zlib.crc32(b"IDAT" + evil) & 0xFFFFFFFF)
    tampered = data[:start] + chunk + data[start + 12 + length :]
    assert "Extra data inside the compressed pixel stream" in labels(tampered)


def test_bad_crc_reported():
    buf = io.BytesIO()
    Image.new("L", (4, 4)).save(buf, "PNG")
    data = bytearray(buf.getvalue())
    data[30] ^= 0xFF  # inside IHDR/IDAT payload
    report = inspect_bytes(bytes(data))
    assert report.error or any(f.label == "Chunk with bad checksum" for f in report.findings)


@pytest.mark.parametrize("name", list(fixtures.all_fixtures()))
def test_truncation_never_crashes(fx, name):
    data = fx[name]
    for cut in (1, 7, len(data) // 3, len(data) // 2, len(data) - 1):
        report = inspect_bytes(data[:cut])  # must return, never raise
        assert isinstance(report.findings, list)


def test_unknown_format():
    report = inspect_bytes(b"\x00" * 64)
    assert report.format is None and report.error


def test_sniffing():
    assert sniff_format(b"\xff\xd8\xff\xe0") == "JPEG"
    assert sniff_format(b"\x89PNG\r\n\x1a\n") == "PNG"
    assert sniff_format(b"RIFF\x00\x00\x00\x00WEBP") == "WEBP"
    assert sniff_format(b"GIF89a") == "GIF"
    assert sniff_format(b"II*\x00") == "TIFF"
    assert sniff_format(b"BM" + b"\x00" * 20) == "BMP"
    assert sniff_format(b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00mif1") == "AVIF"
    assert sniff_format(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1") == "HEIF"


def test_categories(fx):
    cats = {f.category for f in inspect_bytes(fx["jpeg_everything.jpg"]).findings}
    assert METADATA in cats and HIDDEN in cats


def _gif_with_global_table(frame_indices, transparent=None):
    """An 8x8 GIF whose frames are each filled with one index of a shared four-colour global table."""
    pal = bytes([0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0, 255])
    out = bytearray(b"GIF89a" + struct.pack("<HHBBB", 8, 8, 0x81, 0, 0) + pal)
    for n, k in enumerate(frame_indices):
        _p, min_code, data = _pillow_lzw(np.full((8, 8), k, np.uint8), pal)
        t = transparent.get(n) if transparent else None
        out += b"\x21\xf9\x04" + struct.pack("<BHB", 1 if t is not None else 0, 10, t or 0) + b"\x00"
        out += b"\x2c" + struct.pack("<HHHHB", 0, 0, 8, 8, 0) + bytes([min_code]) + data
    return bytes(out + b"\x3b")


def test_gif_global_table_is_judged_across_all_frames():
    # Each frame uses one colour, together all four: nothing is hidden.
    assert not inspect_bytes(_gif_with_global_table([0, 1, 2, 3])).findings
    # Transparent in one frame but drawn in another: the colour is visible.
    assert not inspect_bytes(_gif_with_global_table([0, 1, 2, 3, 1], {1: 1})).findings
    # An entry no frame draws still counts as hidden data.
    assert labels(_gif_with_global_table([0, 1, 2])) == {"Unused palette entries with colour data"}
    assert labels(_gif_with_global_table([0, 1, 2, 3], {3: 3})) == {"Colour data under a transparent palette entry"}
