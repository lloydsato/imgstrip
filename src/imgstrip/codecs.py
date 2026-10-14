"""Custom encoders and decoders used where Pillow alone is not good enough.

* 16-bit-per-channel colour images: Pillow silently truncates 16-bit RGB(A)
  PNG and TIFF data to 8 bits.  ``decode_16bit`` recovers the full precision
  by running Pillow's C decoder twice, once with an unpacker that keeps the
  high byte of every sample and once with one that keeps the low byte, and
  recombining the two.  ``encode_png`` and ``encode_tiff16`` write the result.
* PNG: a numpy implementation of PNG filtering with per-row adaptive filter
  selection, used for 16-bit output and for ``--optimize`` trials.
* GIF: an animation assembler with exact per-frame palettes and frame-delta
  optimisation (Pillow re-quantises frames with transparency lossily).
* BMP: a BITMAPV4 writer so that 32-bit images keep their alpha channel.
"""

from __future__ import annotations

import functools
import io
import struct
import sys
import zlib

import numpy as np
from PIL import Image

# --------------------------------------------------------------------------
# 16-bit decoding
# --------------------------------------------------------------------------


@functools.lru_cache(maxsize=None)
def _picked(mode: str, rawmode: str) -> tuple[int, ...] | None:
    """Which bytes (1-based) of a 16-bit-per-sample pixel an unpacker keeps."""
    width = len(mode) * 2
    try:
        px = Image.frombytes(mode, (1, 1), bytes(range(1, width + 1)), "raw", rawmode).getpixel((0, 0))
    except (ValueError, OSError):
        return None
    return tuple(px) if isinstance(px, tuple) else (px,)


def _complement_rawmode(mode: str, rawmode: str) -> str | None:
    """The unpacker that keeps the *other* byte of every 16-bit sample."""
    hi = _picked(mode, rawmode)
    if hi is None:
        return None
    want = tuple(b + 1 if (b - 1) % 2 == 0 else b - 1 for b in hi)
    prefix = rawmode.split(";")[0]
    for suffix in (";16B", ";16L", ";16N"):
        if _picked(mode, prefix + suffix) == want:
            return prefix + suffix
    return None


def _tile_rawmode(tile) -> str | None:
    args = tile.args
    if isinstance(args, str):
        return args
    if isinstance(args, tuple) and args and isinstance(args[0], str):
        return args[0]
    return None


def _replace_rawmode(tile, rawmode: str):
    args = tile.args
    new_args = rawmode if isinstance(args, str) else (rawmode,) + tuple(args[1:])
    return tile._replace(args=new_args)


def high_bit_depth_kind(im: Image.Image) -> str | None:
    """Return 'RGB', 'RGBA', 'LA' or 'PNM' if Pillow would truncate this frame from 16 bits."""
    tiles = im.tile or []
    if im.format == "PPM" and len(tiles) == 1 and tiles[0].codec_name == "ppm":
        args = tiles[0].args
        if isinstance(args, tuple) and len(args) == 2 and isinstance(args[1], int) and args[1] > 255:
            return "PNM"
    rawmodes = [_tile_rawmode(t) for t in tiles]
    if not rawmodes or any(r is None for r in rawmodes):
        return None
    if im.mode == "RGB" and all(r in ("RGB;16B", "RGB;16L", "RGB;16N") for r in rawmodes):
        return "RGB"
    if im.mode == "RGBA" and all(r in ("RGBA;16B", "RGBA;16L", "RGBA;16N") for r in rawmodes):
        return "RGBA"
    if im.mode == "RGBA" and all(r == "LA;16B" for r in rawmodes) and im.format == "PNG":
        return "LA"
    return None


def decode_16bit(open_image, frame: int, kind: str) -> np.ndarray:
    """Decode one frame at full 16-bit precision into a uint16 (H, W, C) array.

    ``open_image`` is a zero-argument callable returning a fresh, unloaded
    Pillow image for the file (each decode pass needs its own instance).
    """

    def load(transform) -> np.ndarray:
        im = open_image()
        if frame:
            im.seek(frame)
        im.tile = [transform(t) for t in im.tile]
        im.load()
        return np.asarray(im)

    if kind == "PNM":
        return decode_pnm16(open_image().fp)
    if kind == "LA":
        # Gray+alpha 16-bit has 4 bytes per pixel, exactly like 8-bit RGBA, so
        # the raw RGBA unpacker returns every byte untouched.
        raw = load(lambda t: _replace_rawmode(t, "RGBA")).astype(np.uint16)
        return np.stack([(raw[..., 0] << 8) | raw[..., 1], (raw[..., 2] << 8) | raw[..., 3]], axis=-1)
    probe = open_image()
    if frame:
        probe.seek(frame)
    lows = {}
    for t in probe.tile:
        rm = _tile_rawmode(t)
        low = _complement_rawmode(kind, rm)
        if low is None:
            raise ValueError(f"cannot recover low bytes for raw mode {rm}")
        lows[rm] = low
    hi = load(lambda t: t).astype(np.uint16)
    lo = load(lambda t: _replace_rawmode(t, lows[_tile_rawmode(t)])).astype(np.uint16)
    if hi.shape != lo.shape:
        raise ValueError("inconsistent 16-bit decode")
    return (hi << 8) | lo


def decode_pnm16(fp) -> np.ndarray:
    """Binary PGM/PPM (P5/P6) with maxval > 255, scaled to the full 16-bit range."""
    fp.seek(0)
    data = fp.read()
    if data[:2] not in (b"P5", b"P6"):
        raise ValueError("not a binary PGM/PPM file")
    fields: list[int] = []
    pos = 2
    while len(fields) < 3:
        while pos < len(data) and data[pos : pos + 1].isspace():
            pos += 1
        if data[pos : pos + 1] == b"#":
            pos = data.index(b"\n", pos) + 1
            continue
        start = pos
        while pos < len(data) and data[pos : pos + 1].isdigit():
            pos += 1
        if start == pos:
            raise ValueError("malformed PNM header")
        fields.append(int(data[start:pos]))
    pos += 1  # exactly one whitespace byte separates header and raster
    width, height, maxval = fields
    if not 255 < maxval <= 65535:
        raise ValueError("unsupported PNM maxval")
    channels = 3 if data[:2] == b"P6" else 1
    count = width * height * channels
    if pos + 2 * count > len(data):
        raise ValueError("truncated PNM raster")
    raw = np.frombuffer(data, ">u2", count=count, offset=pos).astype(np.uint32)
    if raw.max(initial=0) > maxval:
        raise ValueError("PNM sample exceeds maxval")
    if maxval != 65535:
        raw = (raw * 65535 + maxval // 2) // maxval
    return raw.astype(np.uint16).reshape(height, width, channels)


# --------------------------------------------------------------------------
# PNG encoder
# --------------------------------------------------------------------------

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _chunk(ctype: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF)


def _filter_rows(rows: np.ndarray, prev: np.ndarray, bpp: int, strategy: str) -> tuple[np.ndarray, np.ndarray]:
    """Filter a block of scanlines; returns (filter types, filtered bytes)."""
    x = rows.astype(np.int16)
    up = np.concatenate([prev[None, :].astype(np.int16), x[:-1]], axis=0)
    left = np.zeros_like(x)
    left[:, bpp:] = x[:, :-bpp]
    upleft = np.zeros_like(x)
    upleft[:, bpp:] = up[:, :-bpp]
    candidates = {0: x}
    if strategy in ("adaptive", "sub"):
        candidates[1] = x - left
    if strategy in ("adaptive", "up"):
        candidates[2] = x - up
    if strategy in ("adaptive", "avg"):
        candidates[3] = x - ((left + up) >> 1)
    if strategy in ("adaptive", "paeth"):
        p = left + up - upleft
        pa, pb, pc = np.abs(p - left), np.abs(p - up), np.abs(p - upleft)
        pred = np.where((pa <= pb) & (pa <= pc), left, np.where(pb <= pc, up, upleft))
        candidates[4] = x - pred
    if strategy != "adaptive":
        ftype = {"none": 0, "sub": 1, "up": 2, "avg": 3, "paeth": 4}[strategy]
        out = candidates[ftype].astype(np.uint8)
        return np.full(len(rows), ftype, np.uint8), out
    # Minimum sum of absolute differences heuristic (libpng's default).
    keys = sorted(candidates)
    scores = np.stack(
        [np.abs(candidates[k].astype(np.uint8).astype(np.int8).astype(np.int32)).sum(axis=1) for k in keys]
    )
    best = np.argmin(scores, axis=0)
    stacked = np.stack([candidates[k] for k in keys])
    out = stacked[best, np.arange(len(rows))].astype(np.uint8)
    return np.array(keys, np.uint8)[best], out


def pack_rows(arr: np.ndarray, bit_depth: int) -> np.ndarray:
    """Turn a (H, W[, C]) array into big-endian PNG scanline bytes (H, rowbytes)."""
    h = arr.shape[0]
    if bit_depth == 16:
        return arr.astype(">u2").reshape(h, -1).view(np.uint8)
    if bit_depth == 8:
        return arr.astype(np.uint8).reshape(h, -1)
    flat = arr.reshape(h, -1).astype(np.uint8)
    per_byte = 8 // bit_depth
    pad = (-flat.shape[1]) % per_byte
    if pad:
        flat = np.concatenate([flat, np.zeros((h, pad), np.uint8)], axis=1)
    flat = flat.reshape(h, -1, per_byte)
    out = np.zeros(flat.shape[:2], np.uint8)
    for i in range(per_byte):
        out |= flat[:, :, i] << (8 - bit_depth * (i + 1))
    return out


def encode_png(
    arr: np.ndarray,
    bit_depth: int,
    color_type: int,
    palette: bytes | None = None,
    trns: bytes | None = None,
    icc: bytes | None = None,
    strategy: str = "adaptive",
    level: int = 9,
    zstrategy: int = zlib.Z_DEFAULT_STRATEGY,
) -> bytes:
    """Encode an image array as a minimal PNG (IHDR, [iCCP], [PLTE], [tRNS], IDAT, IEND)."""
    h, w = arr.shape[:2]
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    bpp = max(1, bit_depth * channels // 8)
    rows = pack_rows(arr, bit_depth)
    comp = zlib.compressobj(level, zlib.DEFLATED, 15, 9, zstrategy)
    idat = bytearray()
    prev = np.zeros(rows.shape[1], np.uint8)
    block = max(1, (1 << 21) // max(1, rows.shape[1]))
    for top in range(0, h, block):
        chunk_rows = rows[top : top + block]
        ftypes, filtered = _filter_rows(chunk_rows, prev, bpp, strategy)
        prev = chunk_rows[-1]
        payload = np.concatenate([ftypes[:, None], filtered], axis=1)
        idat += comp.compress(payload.tobytes())
    idat += comp.flush()
    out = bytearray(_PNG_SIG)
    out += _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, bit_depth, color_type, 0, 0, 0))
    if icc is not None:
        out += _chunk(b"iCCP", b"icc\x00\x00" + zlib.compress(icc, 9))
    if palette is not None:
        out += _chunk(b"PLTE", palette)
    if trns:
        out += _chunk(b"tRNS", trns)
    step = 1 << 20
    for i in range(0, max(1, len(idat)), step):
        out += _chunk(b"IDAT", bytes(idat[i : i + step]))
    out += _chunk(b"IEND", b"")
    return bytes(out)


# --------------------------------------------------------------------------
# TIFF encoder
# --------------------------------------------------------------------------


class TiffPage:
    """One page for ``encode_tiff``.

    ``array`` is (H, W, C) with dtype uint8, uint16, int32 or float32 (bilevel
    pages use uint8 0/1 with C == 1).  ``photometric``: 0 none, 1 gray,
    2 RGB, 3 palette, 5 CMYK.  ``extra`` is the number of alpha channels.
    """

    def __init__(self, array, photometric, extra=0, palette=None, bilevel=False, icc=None):
        self.array = np.ascontiguousarray(array if array.ndim == 3 else array[..., None])
        self.photometric = photometric
        self.extra = extra
        self.palette = palette
        self.bilevel = bilevel
        self.icc = icc


def _tiff_strips(page: TiffPage) -> tuple[list[bytes], int, int, int, int]:
    arr = page.array
    h, w, c = arr.shape
    if page.bilevel:
        rows = np.packbits(arr[..., 0].astype(bool), axis=1)
        bits, fmt, predictor = 1, 1, 1
    else:
        bits = arr.dtype.itemsize * 8
        fmt = {"u": 1, "i": 2, "f": 3}[arr.dtype.kind]
        predictor = 2 if fmt in (1, 2) and bits in (8, 16) else 1
        rows = arr
    row_bytes = rows.shape[1] * (1 if page.bilevel else c * arr.dtype.itemsize)
    per_strip = max(1, min(h, (1 << 20) // max(1, row_bytes)))
    strips = []
    for top in range(0, h, per_strip):
        block = rows[top : top + per_strip]
        if predictor == 2:
            diff = block.copy()
            diff[:, 1:] = block[:, 1:] - block[:, :-1]  # wraps modulo 2^bits
            block = diff
        raw = block.astype(block.dtype.newbyteorder("<")).tobytes() if not page.bilevel else block.tobytes()
        strips.append(zlib.compress(raw, 6))
    return strips, per_strip, bits, fmt, predictor


def encode_tiff(pages: list[TiffPage]) -> bytes:
    """Little-endian multi-page TIFF; Adobe Deflate, every byte accounted for."""
    body = bytearray(b"II*\x00\x00\x00\x00\x00")
    next_ptr_pos = 4
    for page in pages:
        h, w, c = page.array.shape
        strips, per_strip, bits, fmt, predictor = _tiff_strips(page)
        offsets = []
        for strip in strips:
            if len(body) & 1:
                body += b"\x00"
            offsets.append(len(body))
            body += strip
        entries = [
            (256, 4, [w]),
            (257, 4, [h]),
            (258, 3, [bits] * c),
            (259, 3, [8]),
            (262, 3, [page.photometric]),
            (273, 4, offsets),
            (277, 3, [c]),
            (278, 4, [per_strip]),
            (279, 4, [len(x) for x in strips]),
            (284, 3, [1]),
        ]
        if predictor != 1:
            entries.append((317, 3, [predictor]))
        if page.palette is not None:
            pal = np.zeros((256, 3), np.uint16)
            pal[: len(page.palette)] = np.asarray(page.palette, np.uint16)
            entries.append((320, 3, [int(v) * 257 for v in pal.T.reshape(-1)]))
        if page.extra:
            entries.append((338, 3, [2] * page.extra))  # unassociated alpha
        if not page.bilevel:
            entries.append((339, 3, [fmt] * c))
        if page.icc is not None:
            entries.append((34675, 7, page.icc))
        entries.sort(key=lambda e: e[0])
        encoded = []
        for tag, typ, values in entries:
            raw = bytes(values) if typ == 7 else struct.pack("<" + ("H" if typ == 3 else "I") * len(values), *values)
            if len(raw) > 4:
                if len(body) & 1:
                    body += b"\x00"
                encoded.append(struct.pack("<HHII", tag, typ, len(values), len(body)))
                body += raw
            else:
                encoded.append(struct.pack("<HHI", tag, typ, len(values)) + raw.ljust(4, b"\x00"))
        if len(body) & 1:
            body += b"\x00"
        ifd = len(body)
        struct.pack_into("<I", body, next_ptr_pos, ifd)
        body += struct.pack("<H", len(encoded)) + b"".join(encoded)
        next_ptr_pos = len(body)
        body += b"\x00\x00\x00\x00"
        if len(body) > 0xFFFFFFFF:
            raise ValueError("image too large for classic TIFF")
    return bytes(body)


def encode_tiff16(arr: np.ndarray, icc: bytes | None = None) -> bytes:
    """16-bit gray/gray+alpha/RGB/RGBA page."""
    c = arr.shape[2]
    photometric = 2 if c >= 3 else 1
    extra = c - (3 if c >= 3 else 1)
    return encode_tiff([TiffPage(arr.astype(np.uint16), photometric, extra, icc=icc)])


# --------------------------------------------------------------------------
# GIF assembler
# --------------------------------------------------------------------------


class GifQuantized(Exception):
    """Raised internally to signal a frame needed lossy colour reduction."""


def _pillow_lzw(indices: np.ndarray, palette: bytes) -> tuple[bytes, int, bytes]:
    """Use Pillow's C LZW encoder on one paletted rectangle.

    Returns (palette as written, LZW minimum code size, image data sub-blocks).
    """
    im = Image.frombytes("P", (indices.shape[1], indices.shape[0]), np.ascontiguousarray(indices).tobytes())
    im.putpalette(palette, "RGB")
    buf = io.BytesIO()
    im.save(buf, "GIF", interlace=False, optimize=False)
    d = buf.getvalue()
    _w, _h, packed = struct.unpack_from("<HHB", d, 6)
    pos = 13
    gct = b""
    if packed & 0x80:
        size = 3 << ((packed & 7) + 1)
        gct = d[pos : pos + size]
        pos += size
    while d[pos] == 0x21:  # skip extensions Pillow may emit
        pos += 2
        while d[pos]:
            pos += d[pos] + 1
        pos += 1
    if d[pos] != 0x2C:
        raise ValueError("unexpected GIF structure from encoder")
    ipacked = d[pos + 9]
    pos += 10
    if ipacked & 0x80:
        size = 3 << ((ipacked & 7) + 1)
        gct = d[pos : pos + size]
        pos += size
    if ipacked & 0x40:
        raise ValueError("encoder produced an interlaced frame")
    min_code = d[pos]
    start = pos + 1
    pos = start
    while d[pos]:
        pos += d[pos] + 1
    return gct, min_code, d[start : pos + 1]


def _frame_palette(pixels: np.ndarray, transparent: np.ndarray | None, reserve_transparent: bool):
    """Exact palette for an RGBA rectangle. Returns (indices, palette bytes, transparent index)."""
    rgb = pixels[..., :3].astype(np.uint32)
    keys = (rgb[..., 0] << 16) | (rgb[..., 1] << 8) | rgb[..., 2]
    if transparent is not None and transparent.any():
        opaque_keys = keys[~transparent]
    else:
        opaque_keys = keys.reshape(-1)
    colors = np.unique(opaque_keys)
    limit = 255 if reserve_transparent else 256
    if len(colors) > limit:
        raise GifQuantized
    index = np.searchsorted(colors, keys).astype(np.uint8)
    t_index = None
    if reserve_transparent:
        t_index = len(colors)
        if transparent is not None:
            index[transparent] = t_index
    n = len(colors) + (1 if reserve_transparent else 0)
    pal = np.zeros((max(2, 1 << max(1, (n - 1).bit_length())), 3), np.uint8)
    pal[: len(colors), 0] = colors >> 16
    pal[: len(colors), 1] = (colors >> 8) & 0xFF
    pal[: len(colors), 2] = colors & 0xFF
    return index, pal.tobytes(), t_index


def _quantize_rect(pixels: np.ndarray, transparent: np.ndarray | None, reserve_transparent: bool):
    """Lossy fallback for rectangles with more colours than GIF allows."""
    colors = 255 if reserve_transparent else 256
    rgb = Image.fromarray(np.ascontiguousarray(pixels[..., :3]), "RGB")
    q = rgb.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG)
    index = np.asarray(q).copy()
    pal = np.frombuffer(bytes(q.getpalette("RGB")[: 3 * colors]), np.uint8).reshape(-1, 3)
    t_index = None
    if reserve_transparent:
        t_index = colors
        if transparent is not None:
            index[transparent] = t_index
    full = np.zeros((256, 3), np.uint8)
    full[: len(pal)] = pal
    return index, full.tobytes(), t_index


def encode_gif(frames: list[np.ndarray], durations: list[int] | None, loop: int | None) -> tuple[bytes, list[int]]:
    """Assemble a GIF from full-canvas RGBA frames.

    Every frame must be (H, W, 4) uint8 with alpha either 0 or 255.  Returns
    the file and the list of frame numbers that had to be colour-quantised.
    """
    h, w = frames[0].shape[:2]
    any_transparency = any((f[..., 3] == 0).any() for f in frames)
    quantized: list[int] = []
    out = bytearray(b"GIF89a")
    out += struct.pack("<HHBBB", w, h, 0x70, 0, 0)
    animated = len(frames) > 1
    if animated and loop is not None:
        out += b"\x21\xff\x0bNETSCAPE2.0\x03\x01" + struct.pack("<H", min(int(loop), 0xFFFF)) + b"\x00"
    prev = None
    for n, frame in enumerate(frames):
        transparent = frame[..., 3] == 0
        if any_transparency:
            # Restore-to-background after each frame: draw only the opaque area.
            disposal = 2
            ys, xs = np.nonzero(~transparent)
            if n == 0 or len(ys) == 0:
                y0, x0, y1, x1 = 0, 0, h, w
            else:
                y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
            rect = frame[y0:y1, x0:x1]
            rect_t = transparent[y0:y1, x0:x1]
            reserve = True
        else:
            disposal = 1
            if prev is None:
                y0, x0, y1, x1 = 0, 0, h, w
                rect_t = None
                reserve = False
            else:
                changed = (frame[..., :3] != prev[..., :3]).any(axis=-1)
                ys, xs = np.nonzero(changed)
                if len(ys) == 0:
                    y0, x0, y1, x1 = 0, 0, 1, 1
                else:
                    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
                # Unchanged pixels inside the rectangle become transparent so the
                # previous frame shows through (keeps palettes small).
                rect_t = ~changed[y0:y1, x0:x1]
                reserve = bool(rect_t.any())
            rect = frame[y0:y1, x0:x1]
        try:
            index, palette, t_index = _frame_palette(rect, rect_t, reserve)
        except GifQuantized:
            quantized.append(n)
            index, palette, t_index = _quantize_rect(rect, rect_t, reserve)
        written_palette, min_code, data = _pillow_lzw(index, palette)
        if written_palette[: len(palette)] != palette[: len(written_palette)]:
            raise ValueError("GIF encoder rewrote the palette")
        delay = int(round((durations[n] if durations else 0) / 10.0)) if animated else 0
        if animated or t_index is not None:
            flags = (disposal << 2) if animated else 0
            if t_index is not None:
                flags |= 1
            out += b"\x21\xf9\x04" + struct.pack("<BHB", flags, min(delay, 0xFFFF), t_index or 0) + b"\x00"
        size_bits = max(1, (len(written_palette) // 3 - 1).bit_length())
        out += b"\x2c" + struct.pack("<HHHHB", x0, y0, x1 - x0, y1 - y0, 0x80 | (size_bits - 1))
        out += written_palette.ljust(3 << size_bits, b"\x00")
        out += bytes([min_code]) + data
        prev = frame
    out += b"\x3b"
    return bytes(out), quantized


# --------------------------------------------------------------------------
# BMP (32-bit with alpha)
# --------------------------------------------------------------------------


def encode_bmp_rgba(arr: np.ndarray) -> bytes:
    """BITMAPV4HEADER, BI_BITFIELDS, 32 bpp BGRA, sRGB colour space, bottom-up."""
    h, w = arr.shape[:2]
    pixels = np.ascontiguousarray(arr[::-1][..., [2, 1, 0, 3]]).tobytes()
    header_size = 14 + 108
    v4 = struct.pack(
        "<IiiHHIIiiII4I4s36s3I",
        108,
        w,
        h,
        1,
        32,
        3,
        len(pixels),
        2835,
        2835,
        0,
        0,
        0x00FF0000,
        0x0000FF00,
        0x000000FF,
        0xFF000000,
        b"BGRs",  # LCS_sRGB ('sRGB' as a little-endian DWORD)
        b"\x00" * 36,
        0,
        0,
        0,
    )
    file_header = b"BM" + struct.pack("<IHHI", header_size + len(pixels), 0, 0, header_size)
    return file_header + v4 + pixels


def native_is_little() -> bool:
    return sys.byteorder == "little"
