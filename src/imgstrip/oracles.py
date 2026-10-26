"""Independent second opinions on what a file *shows*.

imgstrip's own checks (the container inspector and a Pillow re-decode) share assumptions with the encoder
that wrote the file: if they were wrong in the same way, nothing would notice.  This module asks readers
that share no code with them:

``reference_png``   a strict PNG reader written for this purpose with only ``zlib`` and ``numpy``.  It
                    rejects anything a careful reader would (bad CRC, chunk order, data after IEND, extra
                    or missing pixel data, unused deflate bytes) and decodes every colour type and bit
                    depth exactly, 16-bit included.
``magick``          ImageMagick (libpng, libjpeg, libtiff, libwebp, libheif and its own GIF/BMP readers).
``ffmpeg``          FFmpeg's own PNG, GIF, BMP, TIFF and MJPEG decoders.

Every oracle returns the same thing: a list of frames as 16-bit RGBA arrays (8-bit samples are scaled by
257, colour under fully transparent pixels is zeroed).  ``compare`` says whether two such views agree.
The external programs are optional; ``available()`` says which are installed and an oracle that cannot
read a format says so instead of guessing.  Used by ``imgstrip --self-test`` and the test suite.
"""

from __future__ import annotations

import functools
import re
import shutil
import struct
import subprocess
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class OracleError(Exception):
    """The oracle read the file and found it invalid (a verdict, not a missing tool)."""


class Unsupported(Exception):
    """The oracle cannot read this kind of file (no verdict either way)."""


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


def normalise(frame: np.ndarray) -> np.ndarray:
    """RGBA uint16 with the colour of fully transparent pixels zeroed (what a viewer shows)."""
    out = np.array(frame, dtype=np.uint16, copy=True)
    out[out[..., 3] == 0] = 0
    return out


def expand(arr: np.ndarray, bits: int) -> np.ndarray:
    """(H, W, C) samples with C = 1..4 (grey, grey+alpha, RGB, RGBA) to 16-bit RGBA."""
    arr = arr.astype(np.uint16)
    if bits <= 8:
        arr = arr * np.uint16(257)
    if arr.ndim == 2:
        arr = arr[..., None]
    h, w, c = arr.shape
    out = np.empty((h, w, 4), dtype=np.uint16)
    if c in (1, 2):
        out[..., :3] = arr[..., :1]
        out[..., 3] = arr[..., 1] if c == 2 else 65535
    else:
        out[..., :3] = arr[..., :3]
        out[..., 3] = arr[..., 3] if c == 4 else 65535
    return normalise(out)


# Mean absolute difference, in 8-bit levels, that two independent decoders may disagree by on a lossy file
# (IDCT rounding, chroma upsampling, YUV conversion).  Measured on every fixture, mode and format: 2.07 at most.
LOSSY_TOLERANCE = 3.0
# CMYK has no standard conversion to RGB; decoders round it differently.
CMYK_TOLERANCE = 1.0


def tolerance_for(data: bytes, fmt: str, exact: bool) -> float:
    """How closely independent decoders must agree about ``data``.

    Identical for losslessly stored pixels.  JPEG and AVIF/HEIF always get the lossy allowance: even
    the same JPEG coefficients decode one level apart in different decoders.
    """
    if not exact or fmt in ("JPEG", "AVIF", "HEIF"):
        return LOSSY_TOLERANCE
    from .core import _pillow_guard, open_image

    with _pillow_guard([]):
        return CMYK_TOLERANCE if open_image(data).mode == "CMYK" else 0.0


def compare(a: list[np.ndarray], b: list[np.ndarray], tolerance: float = 0.0) -> str | None:
    """None when the two views agree, else a short description of the difference.

    ``tolerance`` is the allowed mean absolute difference per sample in 8-bit levels (0 = identical);
    lossy formats are decoded slightly differently by different decoders.
    """
    if len(a) != len(b):
        return f"{len(a)} frame(s) against {len(b)}"
    for i, (x, y) in enumerate(zip(a, b, strict=True)):
        if x.shape != y.shape:
            return f"frame {i}: size {x.shape[1]}x{x.shape[0]} against {y.shape[1]}x{y.shape[0]}"
        x, y = normalise(x), normalise(y)
        if not tolerance:
            if not np.array_equal(x, y):
                bad = np.argwhere((x != y).any(axis=-1))
                row, col = bad[0]
                return f"frame {i}: {len(bad)} pixel(s) differ, first at x={col} y={row}: {x[row, col]} against {y[row, col]}"
            continue
        diff = np.abs(x.astype(np.int32) - y.astype(np.int32)) / 257.0
        weight = (x[..., 3:] > 0) | (y[..., 3:] > 0)  # pixels nobody can see do not count
        mean = float((diff * weight).sum() / max(1, int(weight.sum()) * 4))
        if mean > tolerance:
            return f"frame {i}: mean difference {mean:.2f} levels (allowed {tolerance})"
    return None


# --------------------------------------------------------------------------
# A strict PNG reader that shares nothing with Pillow or imgstrip.codecs
# --------------------------------------------------------------------------

_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
_KNOWN = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"acTL", b"fcTL", b"fdAT"}
_BEFORE_PLTE = {b"tRNS"}


@dataclass
class RefPng:
    chunks: list[str]  # every chunk type, in file order
    width: int
    height: int
    bit_depth: int
    color_type: int
    frames: list[np.ndarray]  # the default image only (an animation's other frames are not composed)


def png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """Every chunk of a PNG as (type, body), rejecting anything a strict reader would reject."""
    if not data.startswith(_PNG_SIGNATURE):
        raise OracleError("not a PNG signature")
    pos, out = 8, []
    while pos < len(data):
        if pos + 12 > len(data):
            raise OracleError("truncated chunk header")
        (length,) = struct.unpack_from(">I", data, pos)
        ctype = data[pos + 4 : pos + 8]
        if length > 0x7FFFFFFF or pos + 12 + length > len(data):
            raise OracleError(f"{ctype!r} runs past the end of the file")
        body = data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack_from(">I", data, pos + 8 + length)
        if zlib.crc32(ctype + body) != crc:
            raise OracleError(f"{ctype!r} has a bad checksum")
        if not all(65 <= c <= 90 or 97 <= c <= 122 for c in ctype):
            raise OracleError(f"invalid chunk type {ctype!r}")
        out.append((ctype, body))
        pos += 12 + length
        if ctype == b"IEND":
            if pos != len(data):
                raise OracleError(f"{len(data) - pos} byte(s) after IEND")
            if length:
                raise OracleError("IEND is not empty")
            return out
    raise OracleError("no IEND chunk")


def _check_order(chunks: list[tuple[bytes, bytes]]) -> None:
    types = [t for t, _ in chunks]
    if types[0] != b"IHDR" or types.count(b"IHDR") != 1:
        raise OracleError("IHDR must come first, once")
    if types[-1] != b"IEND":
        raise OracleError("IEND must come last")
    idat = [i for i, t in enumerate(types) if t == b"IDAT"]
    if not idat:
        raise OracleError("no IDAT")
    if idat != list(range(idat[0], idat[0] + len(idat))):
        raise OracleError("IDAT chunks are not consecutive")
    for t in set(types):
        if t not in _KNOWN and not (t[0] & 0x20):
            raise OracleError(f"unknown critical chunk {t!r}")
    if types.count(b"PLTE") > 1:
        raise OracleError("more than one PLTE")
    if b"PLTE" in types and types.index(b"PLTE") > idat[0]:
        raise OracleError("PLTE after IDAT")
    for t in _BEFORE_PLTE:
        if t in types and types.count(t) > 1:
            raise OracleError(f"more than one {t!r}")


def _unfilter(raw: bytes, height: int, row_bytes: int, bpp: int) -> np.ndarray:
    stride = row_bytes + 1
    if len(raw) != stride * height:
        raise OracleError(f"pixel data is {len(raw)} bytes, expected {stride * height}")
    buf = np.frombuffer(raw, dtype=np.uint8).reshape(height, stride)
    out = np.zeros((height, row_bytes), dtype=np.uint8)
    for y in range(height):
        kind, line = int(buf[y, 0]), buf[y, 1:].astype(np.int32)
        prev = out[y - 1].astype(np.int32) if y else np.zeros(row_bytes, dtype=np.int32)
        if kind == 0:
            row = line
        elif kind == 1:  # Sub
            row = line.copy()
            for i in range(bpp, row_bytes):
                row[i] = (row[i] + row[i - bpp]) & 255
        elif kind == 2:  # Up
            row = (line + prev) & 255
        elif kind == 3:  # Average
            row = line.copy()
            for i in range(row_bytes):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 255
        elif kind == 4:  # Paeth
            row = line.copy()
            for i in range(row_bytes):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                row[i] = (row[i] + pred) & 255
        else:
            raise OracleError(f"filter type {kind} does not exist")
        out[y] = row.astype(np.uint8)
    return out


def reference_png(data: bytes) -> RefPng:
    """Decode a PNG strictly and exactly (default image only; interlaced files are not supported)."""
    chunks = png_chunks(data)
    _check_order(chunks)
    head = chunks[0][1]
    if len(head) != 13:
        raise OracleError("IHDR is not 13 bytes")
    width, height, depth, ctype, comp, filt, interlace = struct.unpack(">IIBBBBB", head)
    if not width or not height:
        raise OracleError("zero-sized image")
    if ctype not in _CHANNELS or depth not in _DEPTHS[ctype]:
        raise OracleError(f"colour type {ctype} cannot have bit depth {depth}")
    if comp or filt:
        raise OracleError("unknown compression or filter method")
    if interlace:
        raise Unsupported("interlaced PNG")
    palette = next((b for t, b in chunks if t == b"PLTE"), None)
    if ctype == 3 and palette is None:
        raise OracleError("palette image without PLTE")
    if palette is not None and (len(palette) % 3 or not 3 <= len(palette) <= 768 or len(palette) // 3 > 1 << depth):
        raise OracleError("invalid PLTE")
    if palette is not None and ctype in (0, 4):
        raise OracleError("PLTE in a greyscale image")
    trns = next((b for t, b in chunks if t == b"tRNS"), None)
    if trns is not None and ctype in (4, 6):
        raise OracleError("tRNS in an image that has an alpha channel")

    stream = b"".join(b for t, b in chunks if t == b"IDAT")
    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(stream)
    except zlib.error as exc:
        raise OracleError(f"pixel data does not inflate: {exc}") from None
    if not inflater.eof:
        raise OracleError("pixel data ends before the deflate stream does")
    if inflater.unused_data:
        raise OracleError(f"{len(inflater.unused_data)} byte(s) of data after the deflate stream")

    channels = _CHANNELS[ctype]
    bits_per_pixel = channels * depth
    row_bytes = (width * bits_per_pixel + 7) // 8
    rows = _unfilter(raw, height, row_bytes, max(1, bits_per_pixel // 8))

    if depth == 16:
        samples = rows.reshape(height, width * channels, 2).astype(np.uint16)
        samples = (samples[..., 0] << 8) | samples[..., 1]
    elif depth == 8:
        samples = rows.astype(np.uint16)
    else:
        bits = np.unpackbits(rows, axis=1).reshape(height, -1, depth)
        weights = (1 << np.arange(depth - 1, -1, -1)).astype(np.uint16)
        samples = (bits.astype(np.uint16) * weights).sum(axis=2, dtype=np.uint16)[:, : width * channels]
    samples = samples.reshape(height, width, channels)

    if ctype == 3:
        table = np.frombuffer(palette, dtype=np.uint8).reshape(-1, 3)
        index = samples[..., 0]
        if int(index.max()) >= len(table):
            raise OracleError("pixel refers to a palette entry that does not exist")
        alpha = np.full(len(table), 255, dtype=np.uint8)
        if trns is not None:
            if len(trns) > len(table):
                raise OracleError("tRNS has more entries than the palette")
            alpha[: len(trns)] = np.frombuffer(trns, dtype=np.uint8)
        rgba = np.concatenate([table, alpha[:, None]], axis=1)[index].astype(np.uint16) * 257
        frame = normalise(rgba)
    else:
        if depth < 8:  # 1, 2 and 4-bit grey scale to the full 8-bit range
            scaled, scale_bits = samples * (255 // ((1 << depth) - 1)), 8
        else:
            scaled, scale_bits = samples, depth
        frame = expand(scaled, scale_bits)
        if trns is not None:  # a colour key makes matching pixels fully transparent
            key = struct.unpack(f">{len(trns) // 2}H", trns)
            if (ctype == 0 and len(key) != 1) or (ctype == 2 and len(key) != 3):
                raise OracleError("tRNS has the wrong size")
            raw_samples = samples.astype(np.uint16)
            hit = (raw_samples == np.array(key, dtype=np.uint16)).all(axis=2)
            if not hit.any():
                raise OracleError("tRNS colour key matches no pixel")
            frame = frame.copy()
            frame[hit, 3] = 0
            frame = normalise(frame)
    return RefPng([t.decode("ascii") for t, _ in chunks], width, height, depth, ctype, [frame])


# --------------------------------------------------------------------------
# External readers
# --------------------------------------------------------------------------

_EXT = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "GIF": ".gif",
    "TIFF": ".tif",
    "BMP": ".bmp",
    "AVIF": ".avif",
    "HEIF": ".heic",
}


def _run(cmd: list[str], timeout: int = 60) -> bytes:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise OracleError(f"{cmd[0]} timed out") from None
    if proc.returncode:
        text = proc.stderr.decode("utf-8", "replace")
        if "Unsupported codec" in text or "Unsupported feature" in text or "no decode delegate" in text:
            raise Unsupported(f"{cmd[0]} lacks a decoder for this file: {text.strip()[:120]}")
        raise OracleError(f"{cmd[0]} could not read the file: {proc.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return proc.stdout


def _split(raw: bytes, sizes: list[tuple[int, int]], bytes_per_pixel: int) -> list[np.ndarray]:
    frames, pos = [], 0
    for w, h in sizes:
        n = w * h * bytes_per_pixel
        if pos + n > len(raw):
            raise OracleError("the reader produced less pixel data than the frames need")
        frames.append(np.frombuffer(raw, dtype=np.uint8, count=n, offset=pos).reshape(h, w, bytes_per_pixel))
        pos += n
    if pos != len(raw):
        raise OracleError("the reader produced more pixel data than the frames need")
    return frames


def _to16(frames: list[np.ndarray]) -> list[np.ndarray]:
    """Frames of big-endian 16-bit RGBA bytes (H, W, 8) to uint16 RGBA (H, W, 4)."""
    return [normalise((f[..., 0::2].astype(np.uint16) << 8) | f[..., 1::2]) for f in frames]


@functools.cache
def _passthrough_flags(ffmpeg: str) -> tuple[str, ...]:
    """The options that make FFmpeg emit every decoded frame once, with no dropping or duplicating.

    ``-fps_mode passthrough`` replaces ``-vsync 0`` from FFmpeg 5.1 on (``-vsync`` is deprecated there and may go);
    older releases only know ``-vsync``.  A build that does not say its version (a git build) is taken to be new.
    """
    try:
        banner = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ("-vsync", "0")
    found = re.search(r"version\s+n?(\d+)\.(\d+)", banner)
    if found and (int(found[1]), int(found[2])) < (5, 1):
        return ("-vsync", "0")
    return ("-fps_mode", "passthrough")


class _External:
    name = ""
    formats: frozenset[str] = frozenset()

    def available(self) -> bool:
        raise NotImplementedError

    def decode(self, data: bytes, fmt: str, *, animated: bool = False) -> list[np.ndarray]:
        raise NotImplementedError

    def reads(self, fmt: str, animated: bool) -> bool:
        return self.available() and fmt in self.formats


class ImageMagick(_External):
    """ImageMagick 6 or 7 (``convert``/``identify`` or ``magick``)."""

    name = "ImageMagick"
    formats = frozenset({"JPEG", "PNG", "WEBP", "GIF", "TIFF", "BMP", "AVIF", "HEIF"})

    def __init__(self) -> None:
        magick = shutil.which("magick")
        convert = shutil.which("convert")
        self.convert = [magick] if magick else ([convert] if convert else None)
        self.identify = (
            [magick, "identify"] if magick else ([shutil.which("identify")] if shutil.which("identify") else None)
        )

    def available(self) -> bool:
        return self.convert is not None and self.identify is not None

    def reads(self, fmt: str, animated: bool) -> bool:
        if not self.available() or fmt not in self.formats:
            return False
        # ImageMagick shows an APNG's default image only; animated AVIF/HEIF are not read as animations.
        return not (animated and fmt in ("PNG", "AVIF", "HEIF"))

    def decode(self, data: bytes, fmt: str, *, animated: bool = False) -> list[np.ndarray]:
        if not self.reads(fmt, animated):
            raise Unsupported(f"ImageMagick does not read {fmt}{' animations' if animated else ''} faithfully")
        with tempfile.TemporaryDirectory(prefix="imgstrip-oracle-") as tmp:
            path = Path(tmp) / f"in{_EXT[fmt]}"
            path.write_bytes(data)
            flags = ["-coalesce"] if animated else []
            info = _run([*self.convert, str(path), *flags, "-format", "%w %h\n", "info:"]).decode().split("\n")
            sizes = [tuple(map(int, line.split())) for line in info if line.strip()]
            if not sizes:
                raise OracleError("ImageMagick found no image")
            raw = _run([*self.convert, str(path), *flags, "-depth", "16", "-endian", "MSB", "rgba:-"])
        return _to16(_split(raw, sizes, 8))


class FFmpeg(_External):
    """FFmpeg's own decoders (not libwebp-wrapped, not Pillow-derived)."""

    name = "FFmpeg"
    formats = frozenset({"PNG", "GIF", "BMP", "TIFF", "JPEG", "WEBP"})

    def __init__(self) -> None:
        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = shutil.which("ffprobe")

    def available(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe)

    def reads(self, fmt: str, animated: bool) -> bool:
        if not self.available() or fmt not in self.formats:
            return False
        # FFmpeg's WebP decoder is still-only, its TIFF reader takes the first page, and an APNG or GIF
        # is a video stream to it, which is what we want for those two.
        return not (animated and fmt in ("WEBP", "TIFF", "BMP", "JPEG"))

    def decode(self, data: bytes, fmt: str, *, animated: bool = False) -> list[np.ndarray]:
        if not self.reads(fmt, animated):
            raise Unsupported(f"FFmpeg does not read {fmt}{' animations' if animated else ''} faithfully")
        with tempfile.TemporaryDirectory(prefix="imgstrip-oracle-") as tmp:
            path = Path(tmp) / f"in{_EXT[fmt]}"
            path.write_bytes(data)
            probe = _run(
                [
                    self.ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height,pix_fmt",
                    "-of",
                    "csv=p=0",
                    str(path),
                ]
            )
            try:
                w, h, pix_fmt = probe.decode().strip().split(",")[:3]
                w, h = int(w), int(h)
            except ValueError:
                raise OracleError("FFmpeg found no video stream") from None
            # swscale's 8-to-16-bit conversion is not an exact scaling, so only 16-bit sources are asked for 16 bits.
            deep = any(tag in pix_fmt for tag in ("16", "48", "64"))
            raw = _run(
                [
                    self.ffmpeg,
                    "-v",
                    "error",
                    "-noautorotate",
                    "-i",
                    str(path),
                    *_passthrough_flags(self.ffmpeg),
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgba64be" if deep else "rgba",
                    "-",
                ]
            )
        per_pixel = 8 if deep else 4
        count = len(raw) // (w * h * per_pixel)
        if not count:
            raise OracleError("FFmpeg decoded no frames")
        frames = _split(raw, [(w, h)] * count, per_pixel)
        return _to16(frames) if deep else [normalise(f.astype(np.uint16) * 257) for f in frames]


_EXTERNAL = [ImageMagick(), FFmpeg()]


def available() -> list[str]:
    """Names of the independent readers installed here (the built-in PNG reader is always there)."""
    return ["reference-png"] + [o.name for o in _EXTERNAL if o.available()]


def externals(fmt: str, animated: bool = False) -> list[_External]:
    return [o for o in _EXTERNAL if o.reads(fmt, animated)]


# --------------------------------------------------------------------------
# What imgstrip's own decoders make of a file (the view the others are compared with)
# --------------------------------------------------------------------------


def our_view(data: bytes) -> list[np.ndarray]:
    """Every frame as 16-bit RGBA through Pillow (and imgstrip's 16-bit decoder where Pillow would truncate)."""
    from . import codecs
    from .core import _pillow_guard, open_image

    frames = []
    with _pillow_guard([]):
        im = open_image(data)
        for i in range(getattr(im, "n_frames", 1) or 1):
            im.seek(i)
            kind = codecs.high_bit_depth_kind(im)
            if kind:
                arr = codecs.decode_16bit(lambda: open_image(data), i, kind)
                frames.append(expand(arr, 16))
            else:
                im.load()
                frames.append(expand(np.asarray(im.convert("RGBA")), 8))
    return frames


# --------------------------------------------------------------------------
# The check imgstrip runs on its own output
# --------------------------------------------------------------------------

# Every chunk a cleaned PNG may hold; an ICC profile only when a rebuilt one was asked for (--icc sanitize).
PNG_CHUNKS = frozenset({"IHDR", "PLTE", "tRNS", "IDAT", "IEND", "acTL", "fcTL", "fdAT"})


@dataclass
class Differential:
    problems: list[str]
    readers: list[str]  # the independent readers that gave an opinion


def differential(data: bytes, fmt: str, exact: bool, *, allow_icc: bool = False) -> Differential:
    """Ask every independent reader what ``data`` (a file imgstrip wrote, in format ``fmt``) shows.

    ``exact`` says whether its pixels are stored losslessly.  A reader that cannot read the format gives
    no opinion; a reader that rejects the file or sees different pixels is a problem, because a viewer
    using it would show something other than what imgstrip verified.
    """
    problems: list[str] = []
    readers: list[str] = []
    try:
        mine = our_view(data)
    except Exception as exc:  # noqa: BLE001 - any failure to re-read our own output is a finding
        return Differential([f"Pillow cannot re-read the output: {exc}"], readers)
    if fmt == "PNG":
        try:
            ref = reference_png(data)
        except Unsupported:
            pass
        except OracleError as exc:
            problems.append(f"the strict PNG reader rejects the output: {exc}")
        else:
            readers.append("reference-png")
            stray = sorted(set(ref.chunks) - PNG_CHUNKS - ({"iCCP"} if allow_icc else set()))
            if stray:
                problems.append(f"the PNG holds chunks beyond the picture: {', '.join(stray)}")
            diff = compare(ref.frames, mine[:1])
            if diff:
                problems.append(f"the strict PNG reader sees different pixels: {diff}")
    tolerance = tolerance_for(data, fmt, exact)
    for reader in externals(fmt, len(mine) > 1):
        try:
            got = reader.decode(data, fmt, animated=len(mine) > 1)
        except Unsupported:
            continue
        except OracleError as exc:
            problems.append(f"{reader.name} cannot read the output: {exc}")
            continue
        readers.append(reader.name)
        diff = compare(got, mine, tolerance)
        if diff:
            problems.append(f"{reader.name} sees different pixels: {diff}")
    return Differential(problems, readers)
