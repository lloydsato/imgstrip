"""Differential testing: readers that share nothing with imgstrip must see what imgstrip verified.

imgstrip's own checks (the container inspector and a Pillow re-decode) could share a blind spot with the
encoder that wrote the file.  These tests ask independent readers instead: a strict PNG reader written for
the purpose (``zlib`` and ``numpy`` only), ImageMagick, and FFmpeg.  They also test the checkers themselves
(a harness that cannot fail proves nothing) and feed imgstrip files written by *other* encoders, whose
pixels are known, so the corpus is not only things imgstrip made.
"""

from __future__ import annotations

import io
import random
import shutil
import struct
import subprocess
import zlib

import numpy as np
import pytest
from PIL import Image

from imgstrip import fixtures, oracles
from imgstrip.core import OUTPUT_FORMATS, Options, StripError, heif_available, strip_image
from test_fuzz import mutate

HAS_MAGICK = shutil.which("convert") is not None
HAS_FFMPEG = shutil.which("ffmpeg") is not None
needs_magick = pytest.mark.skipif(not HAS_MAGICK, reason="ImageMagick not installed")
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="FFmpeg not installed")

NAMES = list(fixtures.all_fixtures())
FORMATS = [f for f in OUTPUT_FORMATS if f != "HEIF" or heif_available()]


def png_of(arr: np.ndarray, **save) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG", **save)
    return buf.getvalue()


def chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png_parts(data: bytes) -> list[tuple[bytes, bytes]]:
    return oracles.png_chunks(data)


def assemble(parts: list[tuple[bytes, bytes]]) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunk(k, b) for k, b in parts)


def raw_png(width: int, height: int, ctype: int, depth: int, rows: bytes, extra=()) -> bytes:
    head = struct.pack(">IIBBBBB", width, height, depth, ctype, 0, 0, 0)
    return assemble([(b"IHDR", head), *extra, (b"IDAT", zlib.compress(rows)), (b"IEND", b"")])


@pytest.fixture(scope="module")
def tiny() -> bytes:
    """A small clean RGB PNG, as imgstrip writes it."""
    return strip_image(png_of(fixtures.photo(24, 16, 1)), Options()).data


# --------------------------------------------------------------------------
# The checkers must be able to fail
# --------------------------------------------------------------------------


def test_compare_notices_a_single_wrong_pixel_and_a_missing_frame():
    a = np.zeros((4, 5, 4), dtype=np.uint16)
    a[..., 3] = 65535
    b = a.copy()
    b[2, 3, 1] += 1
    assert oracles.compare([a], [a.copy()]) is None
    assert "1 pixel(s) differ, first at x=3 y=2" in oracles.compare([a], [b])
    assert "2 frame(s) against 1" in oracles.compare([a, a], [a])
    assert "size 5x4 against 4x4" in oracles.compare([a], [a[:, :4]])


def test_compare_tolerance_is_an_average_not_a_loophole():
    a = np.full((8, 8, 4), 20 * 257, dtype=np.uint16)
    b = a.copy()
    b[..., :3] += 2 * 257
    assert oracles.compare([a], [b], tolerance=3.0) is None
    b[..., :3] += 10 * 257
    assert "mean difference" in oracles.compare([a], [b], tolerance=3.0)


def test_pixels_nobody_can_see_do_not_count():
    a = np.zeros((4, 4, 4), dtype=np.uint16)
    b = a.copy()
    b[..., :3] = 4000  # colour under fully transparent pixels
    assert oracles.compare([a], [b]) is None


def test_the_strict_reader_accepts_what_imgstrip_writes(tiny):
    ref = oracles.reference_png(tiny)
    assert ref.chunks == ["IHDR", "IDAT", "IEND"] and (ref.width, ref.height) == (24, 16)


def corruptions(tiny: bytes):
    parts = png_parts(tiny)
    head, idat = parts[0][1], b"".join(b for k, b in parts if k == b"IDAT")
    bad_crc = bytearray(tiny)
    bad_crc[40] ^= 0xFF
    rows = zlib.decompress(idat)
    yield "bad checksum", bytes(bad_crc)
    yield "after IEND", tiny + b"extra"
    yield "no IEND", tiny[:-12]
    yield "deflate stream", assemble([parts[0], (b"IDAT", idat + b"junk"), parts[-1]])
    yield "pixel data is", assemble([parts[0], (b"IDAT", zlib.compress(rows[:-5])), parts[-1]])
    yield "pixel data is", assemble([parts[0], (b"IDAT", zlib.compress(rows + b"x")), parts[-1]])
    yield "unknown critical chunk", assemble([parts[0], (b"XYZW", b"hi"), parts[1], parts[2]])
    yield (
        "not consecutive",
        assemble([parts[0], (b"IDAT", idat[:10]), (b"prVt", b"x"), (b"IDAT", idat[10:]), parts[-1]]),
    )
    yield "does not exist", assemble([parts[0], (b"IDAT", zlib.compress(b"\x05" + rows[1:])), parts[-1]])
    yield "IHDR must come first", assemble([(b"prVt", b"x"), *parts])
    yield "more than one PLTE", assemble([parts[0], (b"PLTE", b"\0" * 3), (b"PLTE", b"\0" * 3), *parts[1:]])
    yield "PLTE after IDAT", assemble([parts[0], parts[1], (b"PLTE", b"\0" * 3), parts[2]])
    yield "cannot have bit depth", assemble([(b"IHDR", head[:8] + bytes([4, 2, 0, 0, 0])), *parts[1:]])
    yield "IEND is not empty", assemble([*parts[:-1], (b"IEND", b"x")])
    yield "palette image without PLTE", assemble([(b"IHDR", head[:8] + bytes([8, 3, 0, 0, 0])), *parts[1:]])


def test_the_strict_reader_rejects_what_a_careful_reader_rejects(tiny):
    seen = set()
    for expected, data in corruptions(tiny):
        with pytest.raises(oracles.OracleError, match=expected):
            oracles.reference_png(data)
        seen.add(expected)
    assert len(seen) >= 12


def test_palette_index_beyond_the_palette_is_rejected():
    rows = b"\x00" + bytes([0, 1, 2, 5])  # entry 5 of a 3-entry palette
    data = raw_png(4, 1, 3, 8, rows, extra=[(b"PLTE", bytes(range(9)))])
    with pytest.raises(oracles.OracleError, match="palette entry that does not exist"):
        oracles.reference_png(data)


def test_a_private_chunk_decodes_fine_but_the_cross_check_still_flags_it(tiny):
    """A strict reader tolerates ancillary chunks; the differential check must not."""
    parts = png_parts(tiny)
    stuffed = assemble([parts[0], (b"prVt", b"CANARY"), *parts[1:]])
    assert oracles.reference_png(stuffed).chunks[1] == "prVt"
    found = oracles.differential(stuffed, "PNG", True).problems
    assert any("chunks beyond the picture: prVt" in p for p in found)


def test_a_reader_that_sees_other_pixels_is_a_finding(tiny, monkeypatch):
    def shifted(_data):
        frame = np.zeros((16, 24, 4), dtype=np.uint16)
        frame[..., 3] = 65535
        return [frame]

    monkeypatch.setattr(oracles, "our_view", shifted)
    found = oracles.differential(tiny, "PNG", True)
    assert any("strict PNG reader sees different pixels" in p for p in found.problems)
    if HAS_MAGICK:
        assert any("ImageMagick sees different pixels" in p for p in found.problems)


def test_a_file_a_reader_rejects_is_a_finding(tiny):
    damaged = tiny + b"x"  # data after IEND: Pillow and FFmpeg shrug, the strict reader does not
    found = oracles.differential(damaged, "PNG", True).problems
    assert any("strict PNG reader rejects the output" in p and "after IEND" in p for p in found)


def test_readers_that_cannot_read_a_format_give_no_opinion():
    assert oracles.externals("HEIF", animated=True) == []
    assert all(r.name != "FFmpeg" for r in oracles.externals("WEBP", animated=True))


# --------------------------------------------------------------------------
# The strict PNG reader against encoders it shares nothing with
# --------------------------------------------------------------------------


def pam(arr: np.ndarray, maxval: int) -> bytes:
    """A netpbm P7 file, which ImageMagick reads for any depth and alpha."""
    h, w, c = arr.shape
    kind = {1: "GRAYSCALE", 2: "GRAYSCALE_ALPHA", 3: "RGB", 4: "RGB_ALPHA"}[c]
    head = f"P7\nWIDTH {w}\nHEIGHT {h}\nDEPTH {c}\nMAXVAL {maxval}\nTUPLTYPE {kind}\nENDHDR\n".encode()
    return head + (arr.astype(">u2") if maxval > 255 else arr.astype(np.uint8)).tobytes()


def magick(src: bytes, *args: str, ext_in: str = "pam", out: str = "png") -> bytes:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / f"in.{ext_in}").write_bytes(src)
        subprocess.run(
            ["convert", str(Path(tmp) / f"in.{ext_in}"), *args, f"{out}:{Path(tmp) / 'out'}"],
            check=True,
            capture_output=True,
        )
        return (Path(tmp) / "out").read_bytes()


RNG = np.random.default_rng(2024)


@needs_magick
@pytest.mark.parametrize(
    "channels, depth, ctype",
    [
        (1, 8, 0),
        (1, 16, 0),
        (2, 8, 4),
        (2, 16, 4),
        (3, 8, 2),
        (3, 16, 2),
        (4, 8, 6),
        (4, 16, 6),
        (1, 1, 0),
        (1, 2, 0),
        (1, 4, 0),
    ],
)
def test_the_strict_reader_matches_imagemagick_on_every_colour_type_and_depth(channels, depth, ctype):
    top = (1 << depth) - 1
    levels = RNG.integers(0, top + 1, (17, 23, channels))
    maxval = 65535 if depth == 16 else 255
    arr = levels * (maxval // top) if depth < 8 else levels
    png = magick(
        pam(arr, maxval),
        "-depth",
        str(depth),
        "-define",
        f"png:color-type={ctype}",
        "-define",
        f"png:bit-depth={depth}",
    )
    ref = oracles.reference_png(png)
    assert (ref.bit_depth, ref.color_type) == (depth, ctype)
    (theirs,) = oracles.ImageMagick().decode(png, "PNG")
    assert oracles.compare(ref.frames, [theirs]) is None


@needs_magick
@pytest.mark.parametrize("colours, depth", [(2, 1), (4, 2), (16, 4), (200, 8)])
def test_the_strict_reader_matches_imagemagick_on_palette_images(colours, depth):
    palette = RNG.integers(0, 256, (colours, 3))
    idx = RNG.integers(0, colours, (15, 21))
    png = magick(pam(palette[idx], 255), "-define", "png:color-type=3", "-define", f"png:bit-depth={depth}")
    ref = oracles.reference_png(png)
    assert ref.color_type == 3
    (theirs,) = oracles.ImageMagick().decode(png, "PNG")
    assert oracles.compare(ref.frames, [theirs]) is None


@needs_magick
def test_the_strict_reader_matches_imagemagick_on_every_filter():
    arr = fixtures.photo(31, 19, 6)
    for filt in range(5):
        png = magick(pam(arr, 255), "-define", f"png:compression-filter={filt}", "-define", "png:compression-level=9")
        ref = oracles.reference_png(png)
        assert oracles.compare(ref.frames, [oracles.expand(arr, 8)]) is None, f"filter {filt}"


@needs_magick
def test_the_strict_reader_matches_imagemagick_on_transparent_colour_keys():
    arr = RNG.integers(0, 3, (12, 14, 3)).astype(np.uint8) * 100
    png = magick(pam(arr, 255), "-transparent", "rgb(100,100,100)", "-define", "png:color-type=2")
    assert b"tRNS" in png
    ref = oracles.reference_png(png)
    (theirs,) = oracles.ImageMagick().decode(png, "PNG")
    assert oracles.compare(ref.frames, [theirs]) is None


def test_interlaced_png_is_declared_unsupported_not_misread():
    head = struct.pack(">IIBBBBB", 2, 2, 8, 0, 0, 0, 1)
    data = assemble([(b"IHDR", head), (b"IDAT", zlib.compress(b"\x00\x01\x00\x02")), (b"IEND", b"")])
    with pytest.raises(oracles.Unsupported):
        oracles.reference_png(data)


# --------------------------------------------------------------------------
# Every fixture, every output format
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_independent_readers_see_what_imgstrip_verified(fx, name):
    runs = [("default", Options()), ("scrub-lsb", Options(scrub_lsb=True)), ("icc=sanitize", Options(icc="sanitize"))]
    runs += [(f"to {fmt.lower()}", Options(format=fmt)) for fmt in FORMATS]
    if name.endswith(".jpg") and "mpo" not in name:
        runs.append(("jpeg-lossless", Options(jpeg_lossless=True)))
    opinions = 0
    for label, options in runs:
        try:
            result = strip_image(fx[name], options)
        except StripError:
            continue
        got = oracles.differential(
            result.data, result.output_format, result.verified_exact, allow_icc=options.icc == "sanitize"
        )
        assert not got.problems, f"{name} {label}: {got.problems}"
        opinions += len(got.readers)
    # some outputs have no independent reader (an animated AVIF, say), but a fixture must get opinions overall
    assert opinions or not (HAS_MAGICK or HAS_FFMPEG), f"{name}: no independent reader gave an opinion on any output"


def test_the_cross_check_really_runs_every_reader_on_png(fx):
    result = strip_image(fx["png_everything.png"], Options())
    got = oracles.differential(result.data, "PNG", True)
    assert "reference-png" in got.readers
    assert ("ImageMagick" in got.readers) == HAS_MAGICK and ("FFmpeg" in got.readers) == HAS_FFMPEG


# --------------------------------------------------------------------------
# Inputs written by other encoders, with known pixels
# --------------------------------------------------------------------------


def scene(width=48, height=36, *, alpha=False, seed=3) -> np.ndarray:
    rgb = fixtures.photo(width, height, seed)
    if not alpha:
        return rgb
    a = np.tile(np.linspace(0, 255, width, dtype=np.uint8), (height, 1))
    a[:3] = 255
    a[3:6] = 0
    return np.dstack([rgb, a])


def check_foreign(foreign: bytes, expected: list[np.ndarray] | None, *, lossy: bool = False, options=None):
    """imgstrip cleans a file some other encoder wrote: the result must show the same picture to every reader."""
    result = strip_image(foreign, options or Options())
    tolerance = oracles.LOSSY_TOLERANCE if lossy else 0.0
    if expected is not None:
        assert oracles.compare(oracles.our_view(result.data), expected, tolerance) is None
    got = oracles.differential(result.data, result.output_format, result.verified_exact)
    assert not got.problems, got.problems
    return result


@needs_magick
@pytest.mark.parametrize(
    "label, make, args, ext",
    [
        ("png rgb", lambda: scene(), [], "png"),
        ("png rgba", lambda: scene(alpha=True), [], "png"),
        ("png interlaced", lambda: scene(), ["-interlace", "PNG"], "png"),
        ("png interlaced rgba", lambda: scene(alpha=True), ["-interlace", "PNG"], "png"),
        ("tiff lzw", lambda: scene(), ["-compress", "LZW"], "tif"),
        ("tiff zip", lambda: scene(alpha=True), ["-compress", "Zip"], "tif"),
        ("tiff packbits", lambda: scene(), ["-compress", "RLE"], "tif"),
        ("tiff tiled", lambda: scene(), ["-define", "tiff:tile-geometry=16x16"], "tif"),
        ("tiff strips of 7 rows", lambda: scene(), ["-define", "tiff:rows-per-strip=7"], "tif"),
        ("tiff big-endian", lambda: scene(), ["-define", "tiff:endian=msb"], "tif"),
        ("bmp 24-bit", lambda: scene(), [], "bmp"),
        ("bmp rle", lambda: scene(), ["-colors", "200", "-compress", "RLE"], "bmp"),
        ("webp lossless", lambda: scene(alpha=True), ["-define", "webp:lossless=true"], "webp"),
        ("gif", lambda: scene(), ["-colors", "64"], "gif"),
    ],
)
def test_images_written_by_imagemagick(label, make, args, ext):
    arr = make()
    foreign = magick(pam(arr, 255), *args, out=ext)
    (theirs,) = oracles.ImageMagick().decode(
        foreign, {"tif": "TIFF", "png": "PNG", "bmp": "BMP", "webp": "WEBP", "gif": "GIF"}[ext]
    )
    expected = [theirs]
    if label.startswith(("png", "tiff", "webp")) and "rle" not in label:
        assert oracles.compare([oracles.expand(arr, 8)], expected) is None, "the foreign file holds the pixels we wrote"
    result = check_foreign(foreign, expected)
    assert result.pixels_exact, label


@needs_magick
@pytest.mark.parametrize("ext, args", [("png", []), ("tif", ["-compress", "Zip"]), ("tif", ["-compress", "LZW"])])
def test_sixteen_bit_images_written_by_imagemagick_keep_every_bit(ext, args):
    arr = RNG.integers(0, 65536, (21, 17, 4)).astype(np.uint16)
    foreign = magick(pam(arr, 65535), "-depth", "16", *args, out=ext)
    result = check_foreign(foreign, [oracles.expand(arr, 16)])
    assert result.pixels_exact


@needs_magick
def test_an_optimised_animated_gif_keeps_every_composited_frame():
    """-layers optimize writes sub-rectangle frames with disposal methods: decoders must compose them alike."""
    import tempfile
    from pathlib import Path

    base = scene(40, 30)
    frames = []
    for i in range(4):
        f = base.copy()
        f[5 + 4 * i : 12 + 4 * i, 8:20] = (200, 30 * i, 90)
        frames.append(f)
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for i, f in enumerate(frames):
            p = Path(tmp) / f"f{i}.pam"
            p.write_bytes(pam(f, 255))
            paths.append(str(p))
        subprocess.run(
            ["convert", "-delay", "7", *paths, "-colors", "128", "-layers", "optimize", f"gif:{Path(tmp) / 'o.gif'}"],
            check=True,
            capture_output=True,
        )
        foreign = (Path(tmp) / "o.gif").read_bytes()
        flat = subprocess.run(
            ["convert", f"gif:{Path(tmp) / 'o.gif'}", "-coalesce", "-depth", "16", "-endian", "MSB", "rgba:-"],
            check=True,
            capture_output=True,
        ).stdout
    n = len(flat) // (40 * 30 * 8)
    assert n == 4
    expected = oracles._to16(oracles._split(flat, [(40, 30)] * n, 8))
    result = check_foreign(foreign, expected)
    assert result.frames == 4


@needs_magick
@pytest.mark.parametrize("args", [[], ["-sampling-factor", "1x1"], ["-interlace", "JPEG"], ["-colorspace", "Gray"]])
def test_jpegs_written_by_imagemagick(args):
    foreign = magick(pam(scene(64, 48), 255), "-quality", "92", *args, out="jpg")
    (theirs,) = oracles.ImageMagick().decode(foreign, "JPEG")
    check_foreign(foreign, [theirs], lossy=True)


@needs_magick
def test_a_jpeg_stays_the_same_picture_when_kept_bit_exact():
    foreign = magick(pam(scene(64, 48), 255), "-quality", "90", out="jpg")
    (theirs,) = oracles.ImageMagick().decode(foreign, "JPEG")
    result = check_foreign(foreign, [theirs], lossy=True, options=Options(jpeg_lossless=True))
    assert result.pixels_exact


def ffmpeg_write(arr: np.ndarray, *args: str, out: str) -> bytes:
    import tempfile
    from pathlib import Path

    h, w, c = arr.shape
    pix = {3: "rgb24", 4: "rgba"}[c]
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / f"o.{out}"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "rawvideo",
                "-pix_fmt",
                pix,
                "-s",
                f"{w}x{h}",
                "-i",
                "-",
                "-frames:v",
                "1",
                *args,
                str(target),
            ],
            input=arr.tobytes(),
            check=True,
            capture_output=True,
        )
        return target.read_bytes()


@needs_ffmpeg
@pytest.mark.parametrize(
    "label, alpha, args, out",
    [
        ("png", False, [], "png"),
        ("png rgba", True, [], "png"),
        ("png paeth", False, ["-pred", "paeth"], "png"),
        ("png sub", False, ["-pred", "sub"], "png"),
        ("png avg", False, ["-pred", "avg"], "png"),
        ("tiff lzw", False, ["-compression_algo", "lzw"], "tiff"),
        ("tiff deflate", False, ["-compression_algo", "deflate"], "tiff"),
        ("tiff packbits", False, ["-compression_algo", "packbits"], "tiff"),
        ("bmp", False, [], "bmp"),
        ("webp lossless", True, ["-lossless", "1"], "webp"),
    ],
)
def test_images_written_by_ffmpeg(label, alpha, args, out):
    arr = scene(alpha=alpha)
    foreign = ffmpeg_write(arr, *args, out=out)
    result = check_foreign(foreign, [oracles.expand(arr, 8)])
    assert result.pixels_exact, label


# --------------------------------------------------------------------------
# Malformed inputs
# --------------------------------------------------------------------------


def test_whatever_imgstrip_accepts_from_a_corrupted_file_every_reader_agrees_on(fx):
    accepted = 0
    for name in NAMES:
        rng = random.Random(f"differential-{name}")
        for _ in range(12):
            corrupt = mutate(fx[name], rng)
            try:
                result = strip_image(corrupt, Options(max_pixels=50_000_000))
            except StripError:
                continue
            accepted += 1
            got = oracles.differential(result.data, result.output_format, result.verified_exact)
            assert not got.problems, f"{name}: {got.problems}"
    assert accepted > 60, f"only {accepted} corrupted files were accepted: the check would be mostly idle"
