"""Does the verifier cover every byte it vouches for?

imgstrip rebuilds every file from pixels and then checks the result.  "Checks" is only as good as what the
checker looks at, so this file attacks the checker from two sides:

* **The byte-flip audit.**  Flip bits of a clean output, one at a time.  If the picture still decodes the same
  and nothing complains, that byte is one the verifier does not account for.  Such bytes are accepted only
  where ``regions.allowed`` says so (padding inside compressed streams, tables no coefficient uses, ...); a
  byte anywhere else is a hole.  The audit repairs PNG chunk checksums after each flip, because otherwise the
  checksum would "account" for every byte and hide the holes.
* **Adversarial injections.**  Plant a hidden channel at a specific structural slot (row padding, a palette's
  reserved byte, a nested box that swallows its siblings, ...) and require that verification refuses the file.

The audit itself is tested too: switching a check off must make it report the hole.
"""

from __future__ import annotations

import functools
import io
import struct
import warnings
import zlib

import numpy as np
import pytest
from PIL import Image

import regions
from imgstrip import fixtures, inspect, oracles
from imgstrip.core import Options, VerificationError, _hidden_colour, _Job, heif_available, open_image, strip_image
from imgstrip.inspect import inspect_bytes

BITS = (0, 3, 4, 7)


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


def png_of(arr: np.ndarray, palette: list[int] | None = None) -> bytes:
    im = Image.fromarray(arr)
    if palette:
        im = im.convert("P")
        im.putpalette(palette)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def rgba(width: int, height: int) -> np.ndarray:
    """A photo whose top rows are fully transparent *over visible colour*, and next rows half transparent."""
    alpha = np.full((height, width), 255, np.uint8)
    alpha[:3] = 0
    alpha[3:5] = 128
    return np.dstack([fixtures.photo(width, height, 2), alpha])


def four_colours(width: int, height: int) -> bytes:
    idx = np.random.default_rng(7).integers(0, 4, (height, width)).astype(np.uint8)
    return png_of(idx, [0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0, 255])


def photo_png(width: int, height: int) -> bytes:
    return png_of(fixtures.photo(width, height, 2))


FX = fixtures.all_fixtures()

CASES = {
    "png rgb": lambda: (photo_png(15, 9), Options()),
    "png rgba": lambda: (png_of(rgba(16, 12)), Options()),
    "png 2-bit palette": lambda: (four_colours(15, 9), Options()),
    "png 1-bit grey": lambda: (
        png_of((np.random.default_rng(2).integers(0, 2, (9, 13)) * 255).astype(np.uint8)),
        Options(),
    ),
    "png 16-bit": lambda: (FX["png_16bit_adobe.png"], Options()),
    "apng": lambda: (FX["gif_animated.gif"], Options(format="PNG")),
    "gif photo": lambda: (photo_png(24, 16), Options(format="GIF")),
    "gif palette": lambda: (four_colours(15, 9), Options(format="GIF")),
    "gif animated": lambda: (FX["gif_animated.gif"], Options()),
    "jpeg": lambda: (photo_png(16, 12), Options(format="JPEG")),
    "webp lossless": lambda: (png_of(rgba(16, 12)), Options(format="WEBP")),
    "webp lossy": lambda: (photo_png(16, 12), Options(format="WEBP", quality=80)),
    "tiff rgb": lambda: (photo_png(15, 9), Options(format="TIFF")),
    "tiff rgba": lambda: (png_of(rgba(16, 12)), Options(format="TIFF")),
    "tiff palette": lambda: (four_colours(15, 9), Options(format="TIFF")),
    "tiff 16-bit": lambda: (FX["png_16bit_adobe.png"], Options(format="TIFF")),
    "tiff pages": lambda: (FX["tiff_multipage.tif"], Options()),
    "bmp rgb": lambda: (photo_png(15, 9), Options(format="BMP")),
    "bmp rgba": lambda: (png_of(rgba(16, 12)), Options(format="BMP")),
    "bmp palette": lambda: (four_colours(15, 9), Options(format="BMP")),
    "bmp grey": lambda: (png_of(fixtures.photo(13, 9, 2)[..., 0]), Options(format="BMP")),
    "avif": lambda: (photo_png(16, 12), Options(format="AVIF")),
}
if heif_available():
    CASES["heif"] = lambda: (photo_png(16, 12), Options(format="HEIF"))


@functools.cache
def clean_output(name: str) -> tuple[bytes, str]:
    source, options = CASES[name]()
    result = strip_image(source, options)
    return result.data, result.output_format


# --------------------------------------------------------------------------
# What "noticed" means: whatever imgstrip's own verification would notice
# --------------------------------------------------------------------------


def hidden_in_pixels(data: bytes) -> bool:
    """The decode-based checks of imgstrip's output verification (unused palette data, colour under transparency)."""
    try:
        im = open_image(data)
        im.load()
        arr = np.asarray(im) if im.mode in ("LA", "RGBA") else None
        hidden = _hidden_colour(arr) if arr is not None else 0
        _Job._verify_nothing_hidden_in_pixels(0, im, hidden, arr is not None, False)
    except VerificationError:
        return True
    except Exception:  # noqa: BLE001 - undecodable data is "noticed" elsewhere
        return False
    return False


def noticed(mutated: bytes, base_view) -> bool:
    report = inspect_bytes(mutated)
    if report.error or report.findings:
        return True
    try:
        view = oracles.our_view(mutated)
    except Exception:  # noqa: BLE001 - a reader that cannot decode the file has noticed
        return True
    return oracles.compare(view, base_view) is not None or hidden_in_pixels(mutated)


def repair_png(buf: bytearray, offset: int) -> bytearray | None:
    """Make the chunk checksum valid again after a flip (what an attacker, or a buggy encoder, would write)."""
    pos = 8
    while pos < len(buf):
        (n,) = struct.unpack_from(">I", buf, pos)
        if offset < pos + 4:
            return buf  # the length field: nothing to repair
        if offset < pos + 8 + n + 4:
            if offset >= pos + 8 + n:
                return None  # the checksum itself
            struct.pack_into(">I", buf, pos + 8 + n, zlib.crc32(bytes(buf[pos + 4 : pos + 8 + n])) & 0xFFFFFFFF)
            return buf
        pos += 12 + n
    return buf


def unaccounted(data: bytes, fmt: str, bits=BITS, offsets=None) -> set[int]:
    """Offsets where flipping a bit changes nothing the picture shows and nothing in the verifier notices."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        base = oracles.our_view(data)
        found = set()
        for i in range(len(data)) if offsets is None else offsets:
            for bit in bits:
                mutated = bytearray(data)
                mutated[i] ^= 1 << bit
                if fmt == "PNG":
                    mutated = repair_png(mutated, i)
                    if mutated is None:
                        continue
                if not noticed(bytes(mutated), base):
                    found.add(i)
                    break
    return found


def stray(name: str) -> dict[str, list[int]]:
    data, fmt = clean_output(name)
    spans = regions.WALKERS[fmt](data)
    where = {o: regions.span_of(spans, o) for o in range(len(data))}
    # Only bytes outside the allow-list can be a hole, so only those are worth flipping.
    candidates = [o for o, (region, rel) in where.items() if regions.allowed(fmt, region, rel) is None]
    holes: dict[str, list[int]] = {}
    for offset in sorted(unaccounted(data, fmt, offsets=candidates)):
        holes.setdefault(where[offset][0], []).append(offset)
    return holes


# --------------------------------------------------------------------------
# The byte-flip audit
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(CASES))
def test_every_byte_is_accounted_for_or_is_documented_slack(name):
    assert stray(name) == {}


def test_the_audit_does_see_slack_where_it_is_known_to_exist():
    """A harness that reports nothing proves nothing: it must find the slack we know about."""
    data, fmt = clean_output("jpeg")
    spans = regions.WALKERS[fmt](data)
    hit = {regions.span_of(spans, o)[0] for o in unaccounted(data, fmt)}
    assert {"DQT", "scan data"} & hit
    data, fmt = clean_output("tiff rgb")
    hit = {regions.span_of(regions.WALKERS[fmt](data), o)[0] for o in unaccounted(data, fmt)}
    assert any(h.endswith("tag and type") for h in hit)


def test_the_audit_reports_a_hole_when_a_check_is_switched_off(monkeypatch):
    """Mutation test of the audit: disable the BMP row-padding check and the padding bytes must show up."""
    assert stray("bmp rgb") == {}
    monkeypatch.setattr(inspect, "_bmp_row_padding", lambda *a, **k: None)
    assert "pixel rows" in stray("bmp rgb")
    monkeypatch.undo()
    assert stray("bmp rgb") == {}


def test_the_audit_reports_hidden_transparent_colour_when_that_check_is_switched_off(monkeypatch):
    monkeypatch.setattr(_Job, "_verify_nothing_hidden_in_pixels", staticmethod(lambda *a, **k: None))
    assert "pixel rows" in stray("bmp rgba")


def test_the_audit_reports_unused_palette_data_when_that_check_is_switched_off(monkeypatch):
    assert stray("gif photo") == {}
    monkeypatch.setattr(inspect, "_gif_palette", lambda *a, **k: None)
    monkeypatch.setattr(_Job, "_verify_nothing_hidden_in_pixels", staticmethod(lambda *a, **k: None))
    assert "local colour table" in stray("gif photo")


# --------------------------------------------------------------------------
# Adversarial injections
# --------------------------------------------------------------------------


def verifier_findings(data: bytes) -> str:
    """Everything imgstrip's verification says about a file, as text ('' when it accepts it)."""
    report = inspect_bytes(data)
    text = [report.error] if report.error else []
    text += [str(f) for f in report.findings]
    if hidden_in_pixels(data):
        text.append("hidden data in the pixels")
    return "; ".join(text)


def png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    return oracles.png_chunks(data)


def png_join(parts) -> bytes:
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    return b"\x89PNG\r\n\x1a\n" + b"".join(chunk(k, b) for k, b in parts)


def inject_png_before_idat(kind: bytes, body: bytes):
    def go(d):
        parts = png_chunks(d)
        first = next(i for i, (k, _) in enumerate(parts) if k == b"IDAT")
        return png_join([*parts[:first], (kind, body), *parts[first:]])

    return go


def png_idat(change):
    def go(d):
        parts = png_chunks(d)
        stream = b"".join(b for k, b in parts if k == b"IDAT")
        out = [(k, change(stream)) if k == b"IDAT" else (k, b) for k, b in parts]
        seen, keep = False, []
        for k, b in out:  # keep only the first IDAT, which now holds the whole changed stream
            if k == b"IDAT":
                if seen:
                    continue
                seen = True
            keep.append((k, b))
        return png_join(keep)

    return go


def png_split_idat(d):
    parts = png_chunks(d)
    i = next(i for i, (k, _) in enumerate(parts) if k == b"IDAT")
    body = parts[i][1]
    return png_join([*parts[:i], (b"IDAT", body[:5]), (b"prVt", b"x"), (b"IDAT", body[5:]), *parts[i + 1 :]])


def png_extra_palette_entry(d):
    parts = png_chunks(d)
    return png_join([(k, b + b"\x07\x08\x09" if k == b"PLTE" else b) for k, b in parts])


def bmp_set(offset_fn, value: bytes):
    def go(d):
        out = bytearray(d)
        at = offset_fn(d)
        out[at : at + len(value)] = value
        return bytes(out)

    return go


def bmp_geometry(d):
    pix, dib, w, h, bpp = (
        struct.unpack_from("<I", d, 10)[0],
        struct.unpack_from("<I", d, 14)[0],
        *struct.unpack_from("<ii", d, 18),
        struct.unpack_from("<H", d, 28)[0],
    )
    row = (w * bpp + 7) // 8
    return pix, dib, w, abs(h), bpp, row, ((w * bpp + 31) // 32) * 4


def bmp_padding(d):
    out = bytearray(d)
    pix, _dib, _w, _h, _bpp, row, stride = bmp_geometry(d)
    assert stride > row, "the case needs padded rows"
    out[pix + row] = 0x41
    return bytes(out)


def bmp_gap(d):
    out = bytearray(d)
    pix = struct.unpack_from("<I", d, 10)[0]
    struct.pack_into("<I", out, 10, pix + 4)
    out[pix:pix] = b"\x11\x22\x33\x44"
    struct.pack_into("<I", out, 2, len(out))
    return bytes(out)


def bmp_trailer(d):
    out = bytearray(d) + b"CANARY"
    struct.pack_into("<I", out, 2, len(out))
    return bytes(out)


def bmp_palette_reserved(d):
    out = bytearray(d)
    pix, dib, *_ = bmp_geometry(d)
    out[14 + dib + 3] = 0x5A
    return bytes(out)


def bmp_index_out_of_range(d):
    out = bytearray(d)
    pix, *_ = bmp_geometry(d)
    out[pix] = 0xFF  # entry 255 (or entries 15 and 15) of a palette that holds four colours
    return bytes(out)


def bmp_transparent_colour(d):
    out = bytearray(d)
    pix, _dib, w, h, _bpp, row, stride = bmp_geometry(d)
    top_row = pix + stride * (h - 1)  # bottom-up: the last stored row is the top of the picture
    out[top_row : top_row + 3] = b"\x7f\x7e\x7d"
    return bytes(out)


def bmp_endpoints(d):
    out = bytearray(d)
    out[14 + 60 + 7] = 0x33
    return bytes(out)


def gif_insert(after_region: str, block: bytes):
    def go(d):
        spans = regions.gif(d)
        end = next(e for _s, e, n in spans if n == after_region)
        return d[:end] + block + d[end:]

    return go


def gif_screen(offset: int, value: bytes):
    def go(d):
        return d[:offset] + value + d[offset + len(value) :]

    return go


def gif_frame_flags(d):
    out = bytearray(d)
    start = next(s for s, _e, n in regions.gif(d) if n == "frame header")
    out[start + 9] |= 0x18
    return bytes(out)


def gif_unused_entry(d):
    out = bytearray(d)
    spans = regions.gif(d)
    start, end = next((s, e) for s, e, n in spans if n in ("local colour table", "global colour table"))
    out[end - 3 : end] = b"\x31\x32\x33"  # the last entry of a 256-entry table the picture does not use
    return bytes(out)


def tiff_inline_padding(d):
    spans = regions.tiff(d)
    start = next(s for s, e, n in spans if n.endswith(" padding") and e > s)
    return d[:start] + b"\x5a" + d[start + 1 :]


def tiff_colour_map_low_byte(d):
    e = "<" if d[:2] == b"II" else ">"
    spans = regions.tiff(d)
    start = next(s for s, _e, n in spans if n == "ColorMap value")
    out = bytearray(d)
    out[start + (0 if e == "<" else 1)] ^= 0x55  # the low byte of the first red entry
    return bytes(out)


def append(extra: bytes):
    return lambda d: d + extra


def jpeg_insert(marker: int, payload: bytes):
    def go(d):
        return d[:2] + bytes((0xFF, marker)) + struct.pack(">H", len(payload) + 2) + payload + d[2:]

    return go


def jpeg_stray(d):
    end = next(e for _s, e, n in regions.jpeg(d) if n == "JFIF")
    return d[:end] + b"\x00\x00CANARY" + d[end:]


def jpeg_scan_params(d):
    out = bytearray(d)
    start = next(s for s, _e, n in regions.jpeg(d) if n == "SOS")
    (length,) = struct.unpack_from(">H", d, start + 2)
    out[start + 2 + length - 3 : start + 2 + length] = b"\x01\x3f\x00"  # Ss = 1: start of spectral selection
    return bytes(out)


def jpeg_jfif_version(d):
    out = bytearray(d)
    start = next(s for s, _e, n in regions.jpeg(d) if n == "JFIF")
    out[start + 9] = 7
    return bytes(out)


def jpeg_jfif_thumbnail(d):
    out = bytearray(d)
    start = next(s for s, _e, n in regions.jpeg(d) if n == "JFIF")
    out[start + 16 : start + 18] = b"\x02\x02"
    return bytes(out)


def riff_chunk(kind: bytes, body: bytes):
    def go(d):
        chunk = kind + struct.pack("<I", len(body)) + body + (b"\x00" if len(body) & 1 else b"")
        out = bytearray(d + chunk)
        struct.pack_into("<I", out, 4, len(out) - 8)
        return bytes(out)

    return go


def nested_box_to_the_end(d):
    out = bytearray(d)
    pixi = out.find(b"pixi")
    struct.pack_into(">I", out, pixi - 4, 0)
    return bytes(out)


INJECTIONS = [
    # PNG
    ("png rgb", "a private chunk before the pixels", inject_png_before_idat(b"prVt", b"CANARY"), "Unknown chunk"),
    ("png rgb", "a text chunk", inject_png_before_idat(b"tEXt", b"k\x00CANARY"), "Text"),
    ("png rgb", "bytes after IEND", append(b"CANARY"), "Data after"),
    ("png rgb", "bytes after the deflate stream", png_idat(lambda s: s + b"CANARY"), "Data after the compressed"),
    (
        "png rgb",
        "more pixel bytes than the picture needs",
        png_idat(lambda s: zlib.compress(zlib.decompress(s) + b"CANARY")),
        "Extra data inside",
    ),
    ("png rgb", "an IDAT split around another chunk", png_split_idat, "Non-contiguous"),
    ("png 2-bit palette", "an unused palette entry", png_extra_palette_entry, "hidden data in the pixels"),
    # BMP
    ("bmp rgb", "data in the row padding", bmp_padding, "padding at the end of BMP pixel rows"),
    ("bmp rgb", "a reserved file-header field", bmp_set(lambda d: 6, b"\x41\x41"), "reserved header fields"),
    ("bmp rgb", "bytes between the headers and the pixels", bmp_gap, "Unreferenced bytes"),
    ("bmp rgb", "bytes after the pixels", bmp_trailer, "Data after"),
    ("bmp rgb", "a print resolution", bmp_set(lambda d: 38, struct.pack("<ii", 11811, 11811)), "Print resolution"),
    (
        "bmp rgb",
        "an 'important colours' count",
        bmp_set(lambda d: 50, struct.pack("<I", 77)),
        "unused BMP header field",
    ),
    ("bmp rgb", "a wrong image-size field", bmp_set(lambda d: 34, struct.pack("<I", 12345)), "image size field"),
    ("bmp palette", "a palette reserved byte", bmp_palette_reserved, "reserved bytes in the BMP palette"),
    ("bmp palette", "a pixel beyond the palette", bmp_index_out_of_range, "palette entry that does not exist"),
    ("bmp rgba", "colour under a transparent pixel", bmp_transparent_colour, "hidden data in the pixels"),
    ("bmp rgba", "colour-space endpoints that nothing uses", bmp_endpoints, "colour endpoints"),
    # GIF
    ("gif photo", "a comment", gif_insert("screen", b"\x21\xfe\x06CANARY\x00"), "Comment"),
    ("gif photo", "bytes after the trailer", append(b"CANARY"), "Data after"),
    ("gif photo", "a frame outside the canvas", gif_screen(6, struct.pack("<HH", 4, 4)), "outside the canvas"),
    ("gif photo", "reserved bits in a frame header", gif_frame_flags, "reserved bits"),
    ("gif photo", "a background index with no global table", gif_screen(11, b"\x09"), "background index"),
    ("gif photo", "colour in an unused table entry", gif_unused_entry, "hidden data in the pixels"),
    # TIFF
    ("tiff rgb", "data in the padding of a tag field", tiff_inline_padding, "padding inside TIFF tag fields"),
    ("tiff rgb", "bytes after the file", append(b"CANARY"), "Data after"),
    ("tiff palette", "low bytes of the colour map", tiff_colour_map_low_byte, "more than 8 bits"),
    # JPEG
    ("jpeg", "a comment", jpeg_insert(0xFE, b"CANARY"), "Comment"),
    ("jpeg", "an APP1 segment", jpeg_insert(0xE1, b"CANARY"), "APP1"),
    ("jpeg", "bytes after the end of the image", append(b"CANARY"), "Data after"),
    ("jpeg", "stray bytes between segments", jpeg_stray, "Stray bytes"),
    ("jpeg", "non-standard scan parameters", jpeg_scan_params, "Non-standard scan parameters"),
    ("jpeg", "an impossible JFIF version", jpeg_jfif_version, "Non-standard JFIF version"),
    ("jpeg", "a JFIF thumbnail", jpeg_jfif_thumbnail, "JFIF thumbnail"),
    # WebP
    ("webp lossless", "an unknown chunk", riff_chunk(b"ABCD", b"CANARY!"), "chunk"),
    ("webp lossless", "an EXIF chunk", riff_chunk(b"EXIF", b"CANARY!!"), "EXIF"),
    # AVIF
    ("avif", "a box that claims to run to the end", nested_box_to_the_end, "extend to the end"),
    ("avif", "bytes after the last box", append(b"\x00\x00\x00\x10free" + b"CANARY!!"), ""),
]


@pytest.mark.parametrize("case, what, inject, expected", INJECTIONS, ids=[f"{c}: {w}" for c, w, *_ in INJECTIONS])
def test_verification_refuses_a_planted_hidden_channel(case, what, inject, expected):
    clean, _fmt = clean_output(case)
    assert verifier_findings(clean) == "", "the control must be accepted"
    planted = inject(clean)
    assert planted != clean
    found = verifier_findings(planted)
    assert found, f"{what}: nothing noticed"
    assert expected.lower() in found.lower(), found


# --------------------------------------------------------------------------
# Regressions the audit found
# --------------------------------------------------------------------------


def test_the_lzw_walk_names_exactly_the_palette_entries_a_frame_uses():
    """The GIF palette check reads the used indices from the LZW codes; Pillow's decode is the truth."""
    rng = np.random.default_rng(11)
    for trial in range(60):
        k = int(rng.integers(2, 257))
        w, h = (220, 190) if trial % 10 == 0 else (int(rng.integers(1, 90)), int(rng.integers(1, 90)))
        idx = rng.integers(0, k, (h, w)).astype(np.uint8)  # the long ones fill and clear the 12-bit code table
        if trial % 3 == 0:
            idx = np.sort(idx, axis=1)  # long runs, which exercise the code-not-yet-in-the-table case
        im = Image.fromarray(idx, "P")
        im.putpalette(rng.integers(0, 256, 768).astype(np.uint8).tolist())
        buf = io.BytesIO()
        im.save(buf, "GIF", optimize=False)
        d = buf.getvalue()
        pos = 13 + (3 << ((d[10] & 7) + 1) if d[10] & 0x80 else 0)
        while d[pos] == 0x21:
            pos += 2
            while d[pos]:
                pos += d[pos] + 1
            pos += 1
        flags = d[pos + 9]
        pos += 10 + (3 << ((flags & 7) + 1) if flags & 0x80 else 0)
        data, _ = inspect._gif_subblocks(d, pos + 1)
        want = 0
        for i in np.unique(np.asarray(Image.open(io.BytesIO(d)))).tolist():
            want |= 1 << i
        assert inspect._gif_used_indices(d[pos], data) == want, (trial, k, w, h)


def bmp_v5_with_profile(profile: bytes) -> bytes:
    w, h = 4, 2
    header = struct.pack("<IiiHHIIiiII", 124, w, h, 1, 32, 3, w * h * 4, 0, 0, 0, 0)
    header += struct.pack("<IIII", 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000) + b"DEBM" + bytes(48)
    pixels = bytes(w * h * 4)
    header += struct.pack("<IIII", 8, 124 + len(pixels), len(profile), 0)  # intent, offset, size, reserved
    start = 14 + len(header)
    return b"BM" + struct.pack("<IHHI", start + len(pixels) + len(profile), 0, 0, start) + header + pixels + profile


def test_an_embedded_bmp_v5_profile_is_read_from_the_right_header_fields():
    """The V5 profile fields sit after the endpoints and gamma; they used to be read from the endpoint area."""
    from PIL import ImageCms

    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    report = inspect_bytes(bmp_v5_with_profile(profile))
    assert report.error is None
    assert [f.label for f in report.findings] == ["ICC color profile"]
    assert f"{len(profile)} bytes" in report.findings[0].detail
    assert not any("Data after" in f.label for f in report.findings), (
        "the profile is part of the file, not trailing data"
    )


def test_a_profile_field_without_a_profile_is_hidden_data():
    data = bytearray(bmp_v5_with_profile(b""))
    struct.pack_into("<4s", data, 14 + 56, b"BGRs")  # an sRGB colour space: no profile
    struct.pack_into("<II", data, 14 + 112, 500, 40)
    assert any("Profile fields set without a profile" in f.label for f in inspect_bytes(bytes(data)).findings)


def test_only_the_last_top_level_box_may_claim_to_run_to_the_end():
    data, _ = clean_output("avif")
    assert inspect_bytes(data).error is None
    assert "extend to the end" in (inspect_bytes(nested_box_to_the_end(data)).error or "")


def test_a_smaller_header_that_runs_into_the_pixels_is_rejected():
    data, _ = clean_output("bmp rgb")
    out = bytearray(data)
    struct.pack_into("<I", out, 14, 56)  # a longer header than the one written: it now overlaps the pixel data
    assert "run into the pixel data" in (inspect_bytes(bytes(out)).error or "")


# --------------------------------------------------------------------------
# The engine fails closed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["PNG", "TIFF", "BMP", "WEBP"])  # GIF stores no colour for a transparent pixel
def test_the_engine_refuses_to_write_hidden_colour_if_its_own_scrub_regresses(monkeypatch, fmt):
    """Break the step that clears colour under transparent pixels: verification must stop the write."""
    monkeypatch.setattr(_Job, "_scrub_transparent", lambda self, frames: None)
    with pytest.raises(VerificationError, match="hidden under|transparent"):
        strip_image(FX["png_hidden_alpha.png"], Options(format=fmt))


def test_the_engine_refuses_to_write_unused_palette_data_if_the_palette_is_not_canonical(monkeypatch):
    from imgstrip import codecs

    real = codecs._frame_palette

    def leaky(pixels, transparent, reserve):
        index, palette, t_index = real(pixels, transparent, reserve)
        table = bytearray(palette)
        table[-3:] = b"\x31\x32\x33"  # a colour in the last entry, which three colours leave unused
        return index, bytes(table), t_index

    three = np.random.default_rng(7).integers(0, 3, (9, 15)).astype(np.uint8)
    source = png_of(three, [0, 0, 0, 255, 0, 0, 0, 255, 0])
    assert strip_image(source, Options(format="GIF")).verified_exact
    monkeypatch.setattr(codecs, "_frame_palette", leaky)
    with pytest.raises(VerificationError, match="Unused palette entries"):
        strip_image(source, Options(format="GIF"))


def test_a_quantised_gif_keeps_no_colour_nothing_refers_to():
    """The quantiser used to leave entries nothing referred to, in tables always written at 256 entries."""
    photo = png_of(fixtures.photo(48, 40, 3))
    out = strip_image(photo, Options(format="GIF"))
    assert not out.verified_exact, "the case needs a quantised GIF"
    assert inspect_bytes(out.data).findings == []
    size_bits = (out.data[10] & 7) + 1
    assert out.data[10] & 0x80 == 0 or size_bits <= 8


def test_a_deflate_strip_is_checked_whatever_its_size():
    """A whole page in one strip is normal for imgstrip's own writer: size must not make it look unterminated."""
    big = zlib.compress(bytes(70 << 20), 1)  # 70 MiB of zeros, far past what a bounded read would see
    report = inspect.Report("TIFF")
    inspect._tiff_deflate_strip(big, report)
    assert report.findings == []
    for strip, label in ((big + b"CANARY", "Data after"), (big[:-20], "Unterminated"), (b"not deflate", "Corrupt")):
        report = inspect.Report("TIFF")
        inspect._tiff_deflate_strip(strip, report)
        assert [f.label for f in report.findings] and label in report.findings[0].label, (label, report.findings)
