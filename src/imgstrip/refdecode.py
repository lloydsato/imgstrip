"""A second, independently written reader for stage 2 of the verification (review item 3).

Stage 2 decodes every output again with Pillow and compares it with the pixels that were written.  Where Pillow
also wrote the file (8-bit PNG and APNG, BMP without alpha, JPEG, WebP, AVIF, HEIF), encoder and decoder share code,
and a defect common to both would hide from the check.  This module checks the same thing with code that shares
nothing with Pillow or with imgstrip's encoders (``codecs``):

``check_png``   every PNG and APNG output.  The chunk walk of ``oracles`` (CRC, order, nothing after IEND), zlib, and
                the PNG filters written again here.  Instead of undoing the filters, which is a slow per-byte loop
                in Python, it applies each row's declared filter to the raw scanlines the intended pixels must have
                under the file's own header, palette and transparency, and compares the result with the stored bytes.
                Undoing a filter is a bijection once the filter type is known, so the file decodes to exactly the
                intended pixels if and only if every row matches; the comparison is exact.  APNG frames are composed
                with their offsets, blend and dispose operations.
``check_bmp``   every BMP output: the headers, palette, row padding and bit fields read directly.
``check_jpeg``  default-mode JPEG output, only when asked (``Options.independent_jpeg``, ``--independent-jpeg-check``):
                imgstrip's own entropy decoder (``dct.read``) and an inverse DCT, chroma upsampling and colour
                conversion written here.  It is pure Python and costs about 0.3 s per megapixel, which is why it is off
                by default.  Its result must agree with the intended pixels under the same block bound as Pillow's,
                and with Pillow's own decode.

GIF, TIFF and 16-bit PNG are written by imgstrip's own encoders and read back by Pillow, which shares no code with
them, so stage 2 already uses two independent implementations there.  WebP, AVIF and HEIF have no independent decoder
among imgstrip's dependencies: for them stage 2 rests on the library that wrote them (``imgstrip --self-test`` adds
ImageMagick and FFmpeg where they are installed).

Every check takes the intended frames through ``rows(index, top, bottom)``: uint16 RGBA rows of frame ``index``, 8-bit
samples scaled by 257, colour under fully transparent pixels zeroed.  It raises ``Disagreement`` when the file shows
anything else.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from . import oracles

Rows = Callable[[int, int, int], np.ndarray]
_STRIP_PIXELS = 1 << 17  # the readers work in strips of rows of about this many pixels


@dataclass(frozen=True)
class DecoderLimits:
    """How far two correct decoders of the same lossy file may disagree, in 8-bit levels: on average, and in the
    worst 8 x 8 block (RMS of luma and of chroma)."""

    mean: float
    luma: float
    chroma: float


# imgstrip's JPEG decoder against libjpeg's (different IDCT rounding and chroma upsampling).  Measured on
# photographs, graphics and CMYK at every sampling: at most 0.45 on average and 0.95 in a block.
JPEG_DECODERS = DecoderLimits(mean=1.5, luma=3.0, chroma=6.0)


class Disagreement(Exception):
    """The independent reader does not see the intended pixels in the file (or cannot read it at all)."""


def _strip_rows(width: int) -> int:
    return max(1, _STRIP_PIXELS // max(1, width))


def _canonical(rgba: np.ndarray) -> np.ndarray:
    rgba = rgba.copy()
    rgba[rgba[..., 3] == 0] = 0
    return rgba


# --------------------------------------------------------------------------
# PNG
# --------------------------------------------------------------------------

_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


class _Header:
    def __init__(self, body: bytes):
        if len(body) != 13:
            raise Disagreement("IHDR is not 13 bytes")
        self.width, self.height, self.depth, self.ctype, comp, filt, self.interlace = struct.unpack(">IIBBBBB", body)
        if self.ctype not in _CHANNELS or comp or filt:
            raise Disagreement("unknown colour type, compression or filter method")
        if self.interlace:
            raise Disagreement("interlaced PNG (imgstrip never writes one)")
        self.channels = _CHANNELS[self.ctype]
        self.bpp = max(1, self.channels * self.depth // 8)  # the filters' byte distance

    def row_bytes(self, width: int) -> int:
        return (width * self.channels * self.depth + 7) // 8


class _Palette:
    """The file's PLTE and tRNS as a lookup from 16-bit RGBA to index (or a colour key)."""

    def __init__(self, header: _Header, plte: bytes | None, trns: bytes | None):
        self.key = None
        self.lookup = None
        if header.ctype == 3:
            if plte is None or len(plte) % 3:
                raise Disagreement("palette image without a valid PLTE")
            table = np.frombuffer(plte, np.uint8).reshape(-1, 3).astype(np.uint64)
            alpha = np.full(len(table), 255, np.uint64)
            if trns is not None:
                if len(trns) > len(table):
                    raise Disagreement("tRNS has more entries than the palette")
                alpha[: len(trns)] = np.frombuffer(trns, np.uint8)
            colour = np.where(alpha[:, None] == 0, 0, table)  # what shows under full transparency is nothing
            packed = (colour[:, 0] << 24) | (colour[:, 1] << 16) | (colour[:, 2] << 8) | alpha
            order = np.argsort(packed, kind="stable")  # the first of any duplicates wins
            self.lookup = (packed[order], order.astype(np.uint16))
        elif trns is not None:
            n = {0: 1, 2: 3}.get(header.ctype)
            if n is None or len(trns) != 2 * n:
                raise Disagreement("tRNS does not fit the colour type")
            self.key = np.array(struct.unpack(f">{n}H", trns), np.uint16)


def _raw_samples(want: np.ndarray, header: _Header, palette: _Palette) -> np.ndarray:
    """The sample values (rows, cols, channels) a PNG with this header must store to show ``want``."""
    depth, ctype = header.depth, header.ctype
    rgb, alpha = want[..., :3].astype(np.uint32), want[..., 3].astype(np.uint32)
    if ctype == 3:
        if np.any((rgb % 257 != 0) | (alpha % 257 != 0)[..., None]):
            raise Disagreement("the picture has colours a palette image cannot hold")
        c = rgb // 257
        packed = (c[..., 0].astype(np.uint64) << 24) | (c[..., 1].astype(np.uint64) << 16)
        packed |= (c[..., 2].astype(np.uint64) << 8) | (alpha // 257).astype(np.uint64)
        keys, index = palette.lookup
        pos = np.clip(np.searchsorted(keys, packed), 0, len(keys) - 1)
        if not np.array_equal(keys[pos], packed):
            raise Disagreement("a pixel's colour is not in the palette")
        indices = index[pos]
        if depth < 8 and int(indices.max(initial=0)) >= 1 << depth:
            raise Disagreement("palette index beyond the bit depth")
        return indices[..., None]
    scale = 1 if depth == 16 else 65535 // ((1 << depth) - 1)
    if ctype in (0, 4):
        if np.any((rgb[..., 0] != rgb[..., 1]) | (rgb[..., 1] != rgb[..., 2])):
            raise Disagreement("a grey image shows a colour")
        colour = rgb[..., :1]
    else:
        colour = rgb
    if ctype in (4, 6):
        samples = np.concatenate([colour, alpha[..., None]], axis=-1)
    else:
        samples = colour.copy()
        opaque = alpha == 65535
        if palette.key is None:
            if not opaque.all():
                raise Disagreement("an image without transparency shows transparent pixels")
        else:
            if np.any(~opaque & (alpha != 0)):
                raise Disagreement("a colour key cannot make a pixel half transparent")
            # A transparent pixel stores the key; an opaque one must not, or a viewer would hide it.
            key = palette.key.astype(np.uint32) * scale
            if np.any(opaque & (colour == key).all(axis=-1)):
                raise Disagreement("an opaque pixel has the colour-key value and would show as transparent")
            samples[~opaque] = key
    if np.any(samples % scale):
        raise Disagreement(f"a sample does not fit {depth} bits")
    return (samples // scale).astype(np.uint16)


def _pack(samples: np.ndarray, depth: int) -> np.ndarray:
    """Scanline bytes (rows, row_bytes) of sample values, as PNG stores them."""
    rows, cols, ch = samples.shape
    if depth == 16:
        return samples.astype(">u2").view(np.uint8).reshape(rows, cols * ch * 2)
    if depth == 8:
        return samples.astype(np.uint8).reshape(rows, cols * ch)
    flat = samples.reshape(rows, cols * ch).astype(np.uint8)
    per_byte = 8 // depth
    pad = (-flat.shape[1]) % per_byte
    flat = np.pad(flat, ((0, 0), (0, pad)))
    groups = flat.reshape(rows, -1, per_byte)
    shifts = (depth * np.arange(per_byte - 1, -1, -1)).astype(np.uint8)
    return np.bitwise_or.reduce(groups << shifts, axis=2).astype(np.uint8)


def _filtered(raw: np.ndarray, prev: np.ndarray, kinds: np.ndarray, bpp: int) -> np.ndarray:
    """Apply each row's filter (ISO 15948, section 9) to raw scanlines, given the scanline before the first row.

    Rows are grouped by filter type, so each group computes only its own predictor; uint8 arithmetic wraps modulo
    256 exactly as the filters do.
    """
    up = np.vstack([prev[None, :], raw[:-1]])
    out = np.empty_like(raw)
    for kind in np.unique(kinds).tolist():
        rows = np.flatnonzero(kinds == kind)
        x = raw[rows]
        if kind == 0:
            out[rows] = x
            continue
        b = up[rows]
        a = np.zeros_like(x)
        a[:, bpp:] = x[:, :-bpp]
        if kind == 1:
            predictor = a
        elif kind == 2:
            predictor = b
        elif kind == 3:
            predictor = ((a.astype(np.uint16) + b) >> 1).astype(np.uint8)
        else:
            c = np.zeros_like(x)
            c[:, bpp:] = b[:, :-bpp]
            ai, bi, ci = a.astype(np.int16), b.astype(np.int16), c.astype(np.int16)
            p = ai + bi - ci
            pa, pb, pc = np.abs(p - ai), np.abs(p - bi), np.abs(p - ci)
            predictor = np.where((pa <= pb) & (pa <= pc), a, np.where(pb <= pc, b, c))
        out[rows] = x - predictor
    return out


class _Stream:
    """The inflated pixel data of one image (IDAT or one frame's fdAT), read row by row."""

    def __init__(self, parts: list[bytes]):
        self.parts = list(parts)
        self.inflater = zlib.decompressobj()
        self.pending = b""

    def read(self, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            if self.inflater.unconsumed_tail:
                data = self.inflater.unconsumed_tail
            elif self.parts:
                data = self.parts.pop(0)
            else:
                break
            try:
                out += self.inflater.decompress(data, n - len(out))
            except zlib.error as exc:
                raise Disagreement(f"the pixel data does not inflate: {exc}") from None
        if len(out) != n:
            raise Disagreement("the pixel data ends early")
        return bytes(out)

    def finish(self) -> None:
        rest = self.inflater.unconsumed_tail + b"".join(self.parts)
        try:
            extra = self.inflater.decompress(rest, 1)
        except zlib.error as exc:
            raise Disagreement(f"the pixel data does not inflate: {exc}") from None
        if extra or self.inflater.unconsumed_tail:
            raise Disagreement("more pixel data than the picture needs")
        if not self.inflater.eof or self.inflater.unused_data:
            raise Disagreement("the deflate stream does not end where the pixel data does")


def _check_image(stream: _Stream, header: _Header, palette: _Palette, width: int, height: int, want) -> None:
    """Check that ``stream`` holds exactly the scanlines of ``want(top, bottom)`` (16-bit RGBA rows)."""
    row_bytes = header.row_bytes(width)
    prev = np.zeros(row_bytes, np.uint8)
    step = _strip_rows(width)
    for top in range(0, height, step):
        bottom = min(height, top + step)
        stored = np.frombuffer(stream.read((bottom - top) * (row_bytes + 1)), np.uint8).reshape(-1, row_bytes + 1)
        kinds = stored[:, 0]
        if int(kinds.max()) > 4:
            raise Disagreement(f"filter type {int(kinds.max())} does not exist")
        raw = _pack(_raw_samples(want(top, bottom), header, palette), header.depth)
        mismatch = np.flatnonzero((_filtered(raw, prev, kinds, header.bpp) != stored[:, 1:]).any(axis=1))
        if mismatch.size:
            raise Disagreement(f"row {top + int(mismatch[0])} of the file does not hold the intended pixels")
        prev = raw[-1]
    stream.finish()


def check_png(data: bytes, frames: int, rows: Rows) -> str:
    """Raise Disagreement unless ``data`` shows exactly the intended frames; returns what was checked."""
    try:
        chunks = oracles.png_chunks(data)
        oracles._check_order(chunks)
    except oracles.OracleError as exc:
        raise Disagreement(str(exc)) from None
    header = _Header(chunks[0][1])
    plte = next((b for t, b in chunks if t == b"PLTE"), None)
    trns = next((b for t, b in chunks if t == b"tRNS"), None)
    palette = _Palette(header, plte, trns)
    if not any(t == b"acTL" for t, _ in chunks):
        if frames != 1:
            raise Disagreement(f"a still PNG cannot hold {frames} frames")
        stream = _Stream([b for t, b in chunks if t == b"IDAT"])
        _check_image(stream, header, palette, header.width, header.height, lambda t, b: rows(0, t, b))
        return "imgstrip PNG reader"
    _check_animation(chunks, header, palette, frames, rows)
    return "imgstrip PNG reader (APNG)"


def _check_animation(chunks, header: _Header, palette: _Palette, frames: int, rows: Rows) -> None:
    width, height = header.width, header.height
    (count, _plays) = struct.unpack(">II", next(b for t, b in chunks if t == b"acTL"))
    if count != frames:
        raise Disagreement(f"the animation declares {count} frames, {frames} were written")
    controls: list[tuple[tuple, list[bytes]]] = []
    expected_seq = 0
    for ctype, body in chunks:
        if ctype == b"fcTL":
            if len(body) != 26:
                raise Disagreement("fcTL is not 26 bytes")
            seq, w, h, x, y, _dn, _dd, dispose, blend = struct.unpack(">IIIIIHHBB", body)
            if seq != expected_seq:
                raise Disagreement("APNG sequence numbers out of order")
            expected_seq += 1
            if not w or not h or x + w > width or y + h > height or dispose > 2 or blend > 1:
                raise Disagreement("a frame lies outside the canvas or has an unknown operation")
            controls.append(((w, h, x, y, dispose, blend), []))
        elif ctype == b"IDAT":
            if not controls:
                raise Disagreement("a default image that is not part of the animation (imgstrip never writes one)")
            controls[-1][1].append(body)
        elif ctype == b"fdAT":
            (seq,) = struct.unpack(">I", body[:4])
            if seq != expected_seq or not controls:
                raise Disagreement("APNG sequence numbers out of order")
            expected_seq += 1
            controls[-1][1].append(body[4:])
    if len(controls) != frames:
        raise Disagreement(f"{len(controls)} frame controls for {frames} frames")
    canvas = np.zeros((height, width, 4), np.uint16)
    previous = None
    for k, ((w, h, x, y, dispose, blend), parts) in enumerate(controls):
        if k == 0 and (w, h, x, y) != (width, height, 0, 0):
            raise Disagreement("the first frame does not cover the canvas")
        if blend != 0:
            raise Disagreement("a frame blends over the canvas (imgstrip never writes one)")
        want = _canonical(rows(k, 0, height))
        _check_image(
            _Stream(parts), header, palette, w, h, lambda t, b, want=want, x=x, y=y, w=w: want[y + t : y + b, x : x + w]
        )
        before = canvas.copy() if dispose == 2 else None
        outside = np.ones((height, width), bool)
        outside[y : y + h, x : x + w] = False
        if not np.array_equal(canvas[outside], want[outside]):
            raise Disagreement(f"frame {k}: the canvas around the frame's rectangle is not the intended picture")
        canvas[y : y + h, x : x + w] = want[y : y + h, x : x + w]
        if dispose == 1:  # OP_BACKGROUND: the region is cleared before the next frame
            canvas[y : y + h, x : x + w] = 0
        elif dispose == 2:  # OP_PREVIOUS: back to the canvas as it was before this frame
            canvas = before if previous is not None else np.zeros_like(canvas)
        previous = want


# --------------------------------------------------------------------------
# BMP
# --------------------------------------------------------------------------


def check_bmp(data: bytes, rows: Rows) -> str:
    """Raise Disagreement unless the BMP shows exactly frame 0 of the intended pixels."""
    if len(data) < 54 or data[:2] != b"BM":
        raise Disagreement("not a BMP file")
    file_size, offset = struct.unpack_from("<I", data, 2)[0], struct.unpack_from("<I", data, 10)[0]
    (dib,) = struct.unpack_from("<I", data, 14)
    if file_size != len(data) or dib not in (40, 108, 124) or 14 + dib > len(data):
        raise Disagreement("BMP headers do not describe this file")
    width, height, planes, bpp, compression, image_size = struct.unpack_from("<iiHHII", data, 18)
    (colours,) = struct.unpack_from("<I", data, 46)
    if planes != 1 or width <= 0 or height == 0 or bpp not in (1, 4, 8, 24, 32):
        raise Disagreement("unsupported BMP geometry")
    bottom_up = height > 0
    height = abs(height)
    if compression == 3:
        if bpp != 32 or dib < 108:
            raise Disagreement("bit fields where imgstrip writes none")
        masks = struct.unpack_from("<4I", data, 54)
        shifts = []
        for mask in masks:
            if mask not in (0xFF, 0xFF00, 0xFF0000, 0xFF000000):
                raise Disagreement("bit fields that are not whole bytes")
            shifts.append(mask.bit_length() - 8)
    elif compression != 0:
        raise Disagreement("compressed BMP (imgstrip never writes one)")
    else:
        shifts = None
    table = None
    if bpp <= 8:
        entries = colours or (1 << bpp)
        start = 14 + dib
        raw = np.frombuffer(data, np.uint8, 4 * entries, start).reshape(entries, 4)
        table = np.zeros((1 << bpp, 4), np.uint16)
        table[:entries, :3] = raw[:, 2::-1].astype(np.uint16) * 257
        table[:, 3] = 65535
        table[entries:] = 0
    stride = (width * bpp + 31) // 32 * 4
    if offset + stride * height > len(data):
        raise Disagreement("the pixel array runs past the end of the file")
    step = _strip_rows(width)
    for top in range(0, height, step):
        bottom = min(height, top + step)
        n = bottom - top
        first = height - bottom if bottom_up else top  # the file row of the topmost picture row of this strip
        block = np.frombuffer(data, np.uint8, n * stride, offset + first * stride).reshape(n, stride)
        if bottom_up:
            block = block[::-1]
        if bpp <= 8:
            bits = np.unpackbits(block, axis=1) if bpp < 8 else None
            if bpp == 8:
                index = block[:, :width]
            else:
                groups = bits[:, : width * bpp].reshape(n, width, bpp)
                index = (groups * (1 << np.arange(bpp - 1, -1, -1))).sum(axis=2)
            if colours and int(index.max(initial=0)) >= colours:
                raise Disagreement("a pixel refers to a palette entry that does not exist")
            got = table[index]
        elif bpp == 24:
            got = np.empty((n, width, 4), np.uint16)
            got[..., :3] = block[:, : width * 3].reshape(n, width, 3)[..., ::-1].astype(np.uint16) * 257
            got[..., 3] = 65535
        else:
            words = block[:, : width * 4].copy().view("<u4").reshape(n, width).astype(np.uint32)
            if shifts is None:
                got = np.empty((n, width, 4), np.uint16)
                got[..., :3] = block[:, : width * 4].reshape(n, width, 4)[..., 2::-1].astype(np.uint16) * 257
                got[..., 3] = 65535
            else:
                got = np.stack([((words >> s) & 0xFF).astype(np.uint16) * 257 for s in shifts], axis=-1)
        if not np.array_equal(_canonical(got), _canonical(rows(0, top, bottom))):
            raise Disagreement(f"rows {top} to {bottom} do not show the intended pixels")
    return "imgstrip BMP reader"


# --------------------------------------------------------------------------
# JPEG (opt-in)
# --------------------------------------------------------------------------


def _idct_matrix() -> np.ndarray:
    k = np.arange(8)
    m = np.cos((2 * k[:, None] + 1) * k[None, :] * np.pi / 16) / 2
    m[:, 0] /= np.sqrt(2)
    return m  # m[x, u]: the weight of frequency u in pixel x


_IDCT = _idct_matrix()


def _component_plane(comp, mcus_y: int, mcus_x: int) -> np.ndarray:
    """The decoded samples (float) of one component, over its whole padded block grid."""
    coef = comp.coef.astype(np.float32) * comp.qt.astype(np.float32)
    ph, pw = coef.shape[:2]
    blocks = coef.reshape(ph, pw, 8, 8)
    # blocks[i, j, r, c]: vertical frequency r, horizontal c; out[i, j, y, x]: the pixel at row y, column x.
    pixels = np.einsum("yr,ijrc,xc->ijyx", _IDCT, blocks, _IDCT, optimize=True) + 128
    return pixels.transpose(0, 2, 1, 3).reshape(ph * 8, pw * 8)


def _upsample(plane: np.ndarray, fy: int, fx: int) -> np.ndarray:
    """Triangle-filter upsampling by whole factors, the way libjpeg's 'fancy' upsampling interpolates."""
    for axis, f in ((0, fy), (1, fx)):
        if f == 1:
            continue
        n = plane.shape[axis]
        centres = (np.arange(n * f) + 0.5) / f - 0.5
        lo = np.clip(np.floor(centres).astype(int), 0, n - 1)
        hi = np.clip(lo + 1, 0, n - 1)
        w = centres - np.floor(centres)
        w = np.where(centres < 0, 0, w)
        a, b = np.take(plane, lo, axis=axis), np.take(plane, hi, axis=axis)
        shape = [1, 1]
        shape[axis] = -1
        plane = a * (1 - w.reshape(shape)) + b * w.reshape(shape)
    return plane


def _has_adobe_segment(data: bytes) -> bool:
    """Whether an APP14 'Adobe' segment comes before the first scan."""
    end = data.find(b"\xff\xda")
    i = data.find(b"\xff\xee", 2, end)
    while i >= 0:
        if data[i + 4 : i + 9] == b"Adobe":
            return True
        i = data.find(b"\xff\xee", i + 2, end)
    return False


def decode_jpeg(data: bytes, max_pixels: int) -> np.ndarray:
    """An independent decode of a Huffman JPEG to 8-bit samples (H, W, C): RGB, grey, or CMYK as stored."""
    from . import dct
    from .jpeg import JpegError

    try:
        frame = dct.read(data, max_pixels)
    except JpegError as exc:
        raise Disagreement(f"the independent JPEG reader cannot read the output: {exc}") from None
    hmax = max(c.h for c in frame.comps)
    vmax = max(c.v for c in frame.comps)
    planes = []
    for c in frame.comps:
        plane = _upsample(_component_plane(c, frame.mcus_y, frame.mcus_x), vmax // c.v, hmax // c.h)
        planes.append(plane[: frame.height, : frame.width])
    px = np.stack(planes, axis=-1)
    if len(planes) == 3:
        y, cb, cr = px[..., 0], px[..., 1] - 128, px[..., 2] - 128
        px = np.stack([y + 1.402 * cr, y - 0.344136 * cb - 0.714136 * cr, y + 1.772 * cb], axis=-1)
    out = np.clip(np.rint(px), 0, 255).astype(np.uint8)
    if len(planes) == 4 and _has_adobe_segment(data):
        out = 255 - out  # Adobe CMYK is stored inverted
    return out
