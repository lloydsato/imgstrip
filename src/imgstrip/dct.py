"""JPEG coefficient codec: read the quantised DCT coefficients, normalise them, write them back canonically.

JPEG steganography (JSteg, JPHide, OutGuess, steghide, F3/F4/F5, nsF5, MME, J-UNIWARD, UERD, ...) hides data
in the quantised DCT coefficients, which a bit-exact copy of the compressed data carries along.  This module
decodes a JPEG down to those coefficients with no dependency beyond numpy, so they can be normalised and the
file rebuilt from them:

``read``                 baseline, extended and progressive Huffman JPEGs (8-bit) to a ``Frame``.
``write``                a ``Frame`` to one canonical baseline scan.  The bytes are a pure function of the
                         coefficients, the quantisation tables and the geometry, so every other place a
                         payload could sit (Huffman table layout, unused tables, redundant tokens, padding
                         bits, restart markers, scan scripts, blocks outside the picture) is gone.  The
                         decoded pixels do not change.
``scrub``                destroys payloads stored in coefficient parities (see its docstring).

Anything this module cannot represent exactly (arithmetic coding, 12-bit samples, lossless or hierarchical
JPEG, coefficients no 8-bit image can produce) raises ``JpegError``; callers must fail closed or say so.
"""

from __future__ import annotations

import re
from array import array
from dataclasses import dataclass, field

import numpy as np

from .jpeg import JpegError, _scan_end

# Natural (row-major) index of the k-th coefficient in zig-zag order.
ZIGZAG = (
    0, 1, 8, 16, 9, 2, 3, 10,
    17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34,
    27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36,
    29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46,
    53, 60, 61, 54, 47, 55, 62, 63,
)  # fmt: skip
_ZIG = np.array(ZIGZAG, dtype=np.intp)

_SOF = {0xC0, 0xC1, 0xC2}  # Huffman baseline, extended sequential, progressive
_UNSUPPORTED_SOF = {0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
_RST_SPLIT = re.compile(rb"\xff+[\xd0-\xd7]")
# The canonical writer holds whole-image token arrays.  Measured peaks (tracemalloc, numpy) fit about 32 bytes per
# pixel plus 120 per non-zero coefficient: ~56 MB/MP for a smooth photo, ~340 MB/MP for pure noise.  Anything
# above this budget is refused rather than left to exhaust the machine.
DCT_MEMORY_BUDGET = 3 << 30
_WRITE_BYTES_PER_PIXEL = 32
_WRITE_BYTES_PER_COEFFICIENT = 120
_MAX_DC_DIFF = 2048  # an 8-bit image cannot produce a DC difference or an AC coefficient beyond these
_MAX_AC = 1024


@dataclass
class Component:
    ident: int
    h: int
    v: int
    qt: np.ndarray  # (64,) uint16 quantisation steps, natural order
    blocks_w: int  # blocks that cover the picture
    blocks_h: int
    coef: np.ndarray  # (padded_h, padded_w, 64) int16, natural order; padded to whole MCUs


@dataclass
class Frame:
    width: int
    height: int
    comps: list[Component]
    mcus_x: int
    mcus_y: int
    progressive: bool = False
    scans: int = 0
    # Bytes inside the entropy-coded data that the decoder never used (beyond the final byte's padding bits); a
    # well-formed file has none, so anything here is data a decoder ignores.
    slack: int = 0
    tq: list[int] = field(default_factory=list, repr=False)  # quantisation table id each component names


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def _build_lut(counts: list[int], symbols: list[int]) -> list[int]:
    """16-bit look-ahead table: (code length << 8) | symbol, 0 where no code starts."""
    if sum(counts) != len(symbols) or len(symbols) > 256:
        raise JpegError("malformed Huffman table")
    lut = np.zeros(65536, dtype=np.uint16)
    code = 0
    k = 0
    for length in range(1, 17):
        for _ in range(counts[length - 1]):
            if code >= (1 << length):
                raise JpegError("oversubscribed Huffman table")
            lo = code << (16 - length)
            lut[lo : lo + (1 << (16 - length))] = (length << 8) | symbols[k]
            k += 1
            code += 1
        code <<= 1
    return lut.tolist()


def _parse_dht(payload: bytes, dc: dict, ac: dict) -> None:
    pos = 0
    while pos < len(payload):
        if pos + 17 > len(payload):
            raise JpegError("truncated Huffman table")
        tc, th = payload[pos] >> 4, payload[pos] & 15
        if tc > 1 or th > 3:
            raise JpegError("bad Huffman table class or id")
        counts = list(payload[pos + 1 : pos + 17])
        n = sum(counts)
        symbols = list(payload[pos + 17 : pos + 17 + n])
        if len(symbols) != n:
            raise JpegError("truncated Huffman table")
        if tc == 0 and any(s > 15 for s in symbols):
            raise JpegError("bad DC Huffman symbol")
        (dc if tc == 0 else ac)[th] = _build_lut(counts, symbols)
        pos += 17 + n


def _parse_dqt(payload: bytes, tables: dict) -> None:
    pos = 0
    while pos < len(payload):
        pq, tq = payload[pos] >> 4, payload[pos] & 15
        if pq > 1 or tq > 3:
            raise JpegError("bad quantisation table")
        size = 64 * (pq + 1)
        raw = payload[pos + 1 : pos + 1 + size]
        if len(raw) != size:
            raise JpegError("truncated quantisation table")
        zz = np.frombuffer(raw, dtype=">u2" if pq else "u1").astype(np.uint16)
        nat = np.empty(64, dtype=np.uint16)
        nat[_ZIG] = zz
        tables[tq] = nat
        pos += 1 + size


def _parse_sof(marker: int, payload: bytes, max_pixels: int) -> Frame:
    if marker in _UNSUPPORTED_SOF:
        raise JpegError("arithmetic-coded, lossless and hierarchical JPEG are not supported")
    if len(payload) < 6:
        raise JpegError("truncated frame header")
    precision = payload[0]
    height = int.from_bytes(payload[1:3], "big")
    width = int.from_bytes(payload[3:5], "big")
    n = payload[5]
    if precision != 8:
        raise JpegError(f"{precision}-bit JPEG is not supported")
    if not width or not height:
        raise JpegError("zero image dimension (DNL) is not supported")
    if width * height > max_pixels:
        raise JpegError(f"image is larger than {max_pixels / 1e6:.0f} MP")
    if n < 1 or n > 4 or len(payload) != 6 + 3 * n:
        raise JpegError("malformed frame header")
    raw = []
    for i in range(n):
        ident, hv, tq = payload[6 + 3 * i], payload[7 + 3 * i], payload[8 + 3 * i]
        h, v = hv >> 4, hv & 15
        if not (1 <= h <= 4 and 1 <= v <= 4) or tq > 3:
            raise JpegError("bad sampling factors")
        raw.append((ident, h, v, tq))
    if len({r[0] for r in raw}) != n:
        raise JpegError("duplicate component identifiers")
    if n == 1:  # a single-component scan has one block per MCU whatever the stated factors
        raw = [(raw[0][0], 1, 1, raw[0][3])]
    hmax = max(r[1] for r in raw)
    vmax = max(r[2] for r in raw)
    mcus_x = -(-width // (8 * hmax))
    mcus_y = -(-height // (8 * vmax))
    comps = []
    for ident, h, v, _tq in raw:
        cw = -(-width * h // hmax)
        ch = -(-height * v // vmax)
        coef = np.zeros((mcus_y * v, mcus_x * h, 64), dtype=np.int16)
        comps.append(
            Component(ident, h, v, np.zeros(64, np.uint16), -(-cw // 8), -(-ch // 8), coef)
        )
    return Frame(width, height, comps, mcus_x, mcus_y, progressive=marker == 0xC2, tq=[r[3] for r in raw])


def _scan_items(frame: Frame, slots: list[int]) -> tuple[list[int], list[int], int]:
    """Block visiting order of a scan: (component slot per block, flat coefficient offset, blocks per MCU)."""
    if len(slots) == 1:
        c = frame.comps[slots[0]]
        pw = c.coef.shape[1]
        flat = (np.arange(c.blocks_h)[:, None] * pw + np.arange(c.blocks_w)[None, :]).reshape(-1) * 64
        return [0] * flat.size, flat.tolist(), 1
    parts, slot_parts = [], []
    per_mcu = 0
    for s, i in enumerate(slots):
        c = frame.comps[i]
        ph, pw = c.coef.shape[:2]
        idx = np.arange(ph * pw).reshape(frame.mcus_y, c.v, frame.mcus_x, c.h)
        idx = idx.transpose(0, 2, 1, 3).reshape(frame.mcus_y, frame.mcus_x, c.v * c.h) * 64
        parts.append(idx)
        slot_parts.append(np.full((frame.mcus_y, frame.mcus_x, c.v * c.h), s, dtype=np.intp))
        per_mcu += c.v * c.h
    base = np.concatenate(parts, axis=2).reshape(-1)
    slot = np.concatenate(slot_parts, axis=2).reshape(-1)
    return slot.tolist(), base.tolist(), per_mcu


def _refill_check(pos: int, cnt: int, real: int) -> int:
    """Raise if the data ran out early; otherwise return how many whole bytes the decoder did not need."""
    used = pos * 8 - cnt
    if used > real * 8:
        raise JpegError("entropy-coded data ends early")
    return real - (used + 7) // 8


def _decode_sequential(data, real, slots, bases, dcl, acl, planes):
    zz = ZIGZAG
    buf = cnt = pos = 0
    preds = [0] * len(planes)
    try:
        for slot, base in zip(slots, bases, strict=True):
            plane = planes[slot]
            if cnt < 32:
                buf = ((buf & ((1 << cnt) - 1)) << 32) | int.from_bytes(data[pos : pos + 4], "big")
                pos += 4
                cnt += 32
            e = dcl[slot][(buf >> (cnt - 16)) & 0xFFFF]
            if not e:
                raise JpegError("invalid Huffman code")
            cnt -= e >> 8
            s = e & 0xFF
            if s:
                cnt -= s
                v = (buf >> cnt) & ((1 << s) - 1)
                if v < (1 << (s - 1)):
                    v -= (1 << s) - 1
                preds[slot] += v
            plane[base] = preds[slot]
            ac = acl[slot]
            k = 1
            while k < 64:
                if cnt < 32:
                    buf = ((buf & ((1 << cnt) - 1)) << 32) | int.from_bytes(data[pos : pos + 4], "big")
                    pos += 4
                    cnt += 32
                e = ac[(buf >> (cnt - 16)) & 0xFFFF]
                if not e:
                    raise JpegError("invalid Huffman code")
                cnt -= e >> 8
                rs = e & 0xFF
                s = rs & 15
                if s:
                    k += rs >> 4
                    if k > 63:
                        raise JpegError("coefficient index out of range")
                    cnt -= s
                    v = (buf >> cnt) & ((1 << s) - 1)
                    if v < (1 << (s - 1)):
                        v -= (1 << s) - 1
                    plane[base + zz[k]] = v
                    k += 1
                elif rs == 0xF0:
                    k += 16
                else:  # EOB (libjpeg also ends the block on 0x10..0xE0)
                    break
    except OverflowError:
        raise JpegError("coefficient out of range") from None
    return _refill_check(pos, cnt, real)


def _decode_dc_first(data, real, slots, bases, dcl, planes, al):
    buf = cnt = pos = 0
    preds = [0] * len(planes)
    try:
        for slot, base in zip(slots, bases, strict=True):
            if cnt < 32:
                buf = ((buf & ((1 << cnt) - 1)) << 32) | int.from_bytes(data[pos : pos + 4], "big")
                pos += 4
                cnt += 32
            e = dcl[slot][(buf >> (cnt - 16)) & 0xFFFF]
            if not e:
                raise JpegError("invalid Huffman code")
            cnt -= e >> 8
            s = e & 0xFF
            if s:
                cnt -= s
                v = (buf >> cnt) & ((1 << s) - 1)
                if v < (1 << (s - 1)):
                    v -= (1 << s) - 1
                preds[slot] += v
            planes[slot][base] = preds[slot] << al
    except OverflowError:
        raise JpegError("coefficient out of range") from None
    return _refill_check(pos, cnt, real)


def _decode_dc_refine(data, real, slots, bases, planes, al):
    buf = cnt = pos = 0
    p1 = 1 << al
    for slot, base in zip(slots, bases, strict=True):
        if cnt < 1:
            buf = int.from_bytes(data[pos : pos + 4], "big")
            pos += 4
            cnt = 32
        cnt -= 1
        if (buf >> cnt) & 1:
            planes[slot][base] |= p1
    return _refill_check(pos, cnt, real)


def _decode_ac_first(data, real, bases, ac, plane, ss, se, al):
    zz = ZIGZAG
    buf = cnt = pos = 0
    eobrun = 0
    try:
        for base in bases:
            if eobrun:
                eobrun -= 1
                continue
            k = ss
            while k <= se:
                if cnt < 32:
                    buf = ((buf & ((1 << cnt) - 1)) << 32) | int.from_bytes(data[pos : pos + 4], "big")
                    pos += 4
                    cnt += 32
                e = ac[(buf >> (cnt - 16)) & 0xFFFF]
                if not e:
                    raise JpegError("invalid Huffman code")
                cnt -= e >> 8
                rs = e & 0xFF
                r, s = rs >> 4, rs & 15
                if s:
                    k += r
                    if k > se:
                        raise JpegError("coefficient index out of range")
                    cnt -= s
                    v = (buf >> cnt) & ((1 << s) - 1)
                    if v < (1 << (s - 1)):
                        v -= (1 << s) - 1
                    plane[base + zz[k]] = v << al
                    k += 1
                elif r == 15:
                    k += 16
                else:
                    eobrun = 1 << r
                    if r:
                        cnt -= r
                        eobrun += (buf >> cnt) & ((1 << r) - 1)
                    eobrun -= 1
                    break
    except OverflowError:
        raise JpegError("coefficient out of range") from None
    return _refill_check(pos, cnt, real)


def _decode_ac_refine(data, real, bases, ac, plane, ss, se, al):
    zz = ZIGZAG
    buf = cnt = pos = 0
    eobrun = 0
    p1 = 1 << al
    m1 = -1 << al
    try:
        for base in bases:
            k = ss
            if not eobrun:
                while k <= se:
                    if cnt < 32:
                        buf = ((buf & ((1 << cnt) - 1)) << 32) | int.from_bytes(data[pos : pos + 4], "big")
                        pos += 4
                        cnt += 32
                    e = ac[(buf >> (cnt - 16)) & 0xFFFF]
                    if not e:
                        raise JpegError("invalid Huffman code")
                    cnt -= e >> 8
                    rs = e & 0xFF
                    r, s = rs >> 4, rs & 15
                    if s:
                        if s != 1:
                            raise JpegError("bad refinement symbol")
                        cnt -= 1
                        s = p1 if (buf >> cnt) & 1 else m1
                    elif r != 15:
                        eobrun = 1 << r
                        if r:
                            cnt -= r
                            eobrun += (buf >> cnt) & ((1 << r) - 1)
                        break
                    while k <= se:
                        i = base + zz[k]
                        cur = plane[i]
                        if cur:
                            if cnt < 1:
                                buf = int.from_bytes(data[pos : pos + 4], "big")
                                pos += 4
                                cnt = 32
                            cnt -= 1
                            if (buf >> cnt) & 1 and not cur & p1:
                                plane[i] = cur + (p1 if cur >= 0 else m1)
                        else:
                            r -= 1
                            if r < 0:
                                break
                        k += 1
                    if s:
                        if k > se:
                            raise JpegError("coefficient index out of range")
                        plane[base + zz[k]] = s
                    k += 1
            if eobrun:
                while k <= se:
                    i = base + zz[k]
                    cur = plane[i]
                    if cur:
                        if cnt < 1:
                            buf = int.from_bytes(data[pos : pos + 4], "big")
                            pos += 4
                            cnt = 32
                        cnt -= 1
                        if (buf >> cnt) & 1 and not cur & p1:
                            plane[i] = cur + (p1 if cur >= 0 else m1)
                    k += 1
                eobrun -= 1
    except OverflowError:
        raise JpegError("coefficient out of range") from None
    return _refill_check(pos, cnt, real)


def _decode_scan(frame: Frame, payload: bytes, entropy: bytes, ri: int, dct: dict, act: dict, qts: dict, planes):
    if len(payload) < 1:
        raise JpegError("truncated scan header")
    ns = payload[0]
    if ns < 1 or ns > 4 or len(payload) != 4 + 2 * ns:
        raise JpegError("malformed scan header")
    ids = [c.ident for c in frame.comps]
    slots, td, ta = [], [], []
    for i in range(ns):
        cs, t = payload[1 + 2 * i], payload[2 + 2 * i]
        if cs not in ids:
            raise JpegError("scan names an unknown component")
        idx = ids.index(cs)
        if idx in slots:
            raise JpegError("component repeated in a scan")
        slots.append(idx)
        td.append(t >> 4)
        ta.append(t & 15)
    ss, se, a = payload[1 + 2 * ns], payload[2 + 2 * ns], payload[3 + 2 * ns]
    ah, al = a >> 4, a & 15
    for idx in slots:  # latch the quantisation table the way decoders do: when the component is first coded
        comp = frame.comps[idx]
        if not comp.qt.any():
            tq = frame.tq[idx]
            if tq not in qts:
                raise JpegError("missing quantisation table")
            comp.qt = qts[tq].copy()
    if len(slots) > 1 and sum(frame.comps[i].h * frame.comps[i].v for i in slots) > 10:
        raise JpegError("too many blocks in an MCU")
    slot_list, base_list, bpm = _scan_items(frame, slots)
    progressive = frame.progressive
    if progressive:
        if ss > se or se > 63 or ah > 13 or al > 13:
            raise JpegError("bad progressive scan parameters")
        if ss == 0 and se != 0:
            raise JpegError("progressive DC scan with AC coefficients")
        if ss > 0 and ns != 1:
            raise JpegError("progressive AC scan with several components")
        if ah and ah != al + 1:
            raise JpegError("bad successive approximation")
    n_mcus = len(base_list) // bpm
    parts = _RST_SPLIT.split(entropy)
    expected = -(-n_mcus // ri) if ri else 1
    if len(parts) != expected:
        raise JpegError("restart markers do not match the restart interval")
    per = ri * bpm if ri else len(base_list)
    scan_planes = [planes[i] for i in slots]
    for n, part in enumerate(parts):
        part = part.rstrip(b"\xff").replace(b"\xff\x00", b"\xff")
        real = len(part)
        data = part + bytes(16)
        sl = slot_list[n * per : (n + 1) * per]
        bs = base_list[n * per : (n + 1) * per]
        if not progressive:
            try:
                dcl = [dct[t] for t in td]
                acl = [act[t] for t in ta]
            except KeyError:
                raise JpegError("scan uses an undefined Huffman table") from None
            frame.slack += _decode_sequential(data, real, sl, bs, dcl, acl, scan_planes)
        elif ss == 0:
            if ah:
                frame.slack += _decode_dc_refine(data, real, sl, bs, scan_planes, al)
            else:
                try:
                    dcl = [dct[t] for t in td]
                except KeyError:
                    raise JpegError("scan uses an undefined Huffman table") from None
                frame.slack += _decode_dc_first(data, real, sl, bs, dcl, scan_planes, al)
        else:
            try:
                ac = act[ta[0]]
            except KeyError:
                raise JpegError("scan uses an undefined Huffman table") from None
            if ah:
                frame.slack += _decode_ac_refine(data, real, bs, ac, scan_planes[0], ss, se, al)
            else:
                frame.slack += _decode_ac_first(data, real, bs, ac, scan_planes[0], ss, se, al)
    return set(slots)


def read(data: bytes, max_pixels: int = 300_000_000) -> Frame:
    """Decode the first image in ``data`` to its quantised coefficients."""
    if not data.startswith(b"\xff\xd8"):
        raise JpegError("not a JPEG file")
    n = len(data)
    pos = 2
    qts: dict[int, np.ndarray] = {}
    dct: dict[int, list[int]] = {}
    act: dict[int, list[int]] = {}
    ri = 0
    frame: Frame | None = None
    planes: list[array] = []
    seen: set[int] = set()
    while True:
        if pos >= n:
            raise JpegError("missing end-of-image marker")
        if data[pos] != 0xFF:
            nxt = data.find(b"\xff", pos)
            if nxt < 0:
                raise JpegError("garbage after last segment")
            pos = nxt
            continue
        while pos < n and data[pos] == 0xFF:
            pos += 1
        if pos >= n:
            raise JpegError("truncated marker")
        marker = data[pos]
        pos += 1
        if marker == 0xD9:
            break
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            continue
        if marker in (0x00, 0xD8):
            raise JpegError(f"unexpected marker 0xFF{marker:02X}")
        if pos + 2 > n:
            raise JpegError("truncated segment")
        length = int.from_bytes(data[pos : pos + 2], "big")
        if length < 2 or pos + length > n:
            raise JpegError("segment overruns the file")
        payload = data[pos + 2 : pos + length]
        pos += length
        if marker in _SOF or marker in _UNSUPPORTED_SOF:
            if frame is not None:
                raise JpegError("more than one frame header")
            frame = _parse_sof(marker, payload, max_pixels)
            planes = [array("h", bytes(2 * c.coef.size)) for c in frame.comps]
        elif marker == 0xDB:
            _parse_dqt(payload, qts)
        elif marker == 0xC4:
            _parse_dht(payload, dct, act)
        elif marker == 0xDD:
            if len(payload) != 2:
                raise JpegError("bad restart interval")
            ri = int.from_bytes(payload, "big")
        elif marker == 0xDC:
            raise JpegError("DNL is not supported")
        elif marker == 0xDA:
            if frame is None:
                raise JpegError("scan before frame header")
            end = _scan_end(data, pos)
            seen |= _decode_scan(frame, payload, data[pos:end], ri, dct, act, qts, planes)
            frame.scans += 1
            pos = end
    if frame is None or not frame.scans:
        raise JpegError("no image data")
    if len(seen) != len(frame.comps):
        raise JpegError("a component has no scan data")
    for comp, plane in zip(frame.comps, planes, strict=True):
        comp.coef = np.frombuffer(plane, dtype=np.int16).reshape(comp.coef.shape).copy()
    return frame


# --------------------------------------------------------------------------
# Canonical form
# --------------------------------------------------------------------------


def canonicalise_padding(frame: Frame) -> None:
    """Blocks past the edge of the picture are never shown; give them the same plain content everywhere.

    They keep the DC of the nearest visible block (so they cost almost nothing) and no AC coefficients.
    """
    for c in frame.comps:
        ph, pw = c.coef.shape[:2]
        if ph == c.blocks_h and pw == c.blocks_w:
            continue
        dc = np.pad(c.coef[: c.blocks_h, : c.blocks_w, 0], ((0, ph - c.blocks_h), (0, pw - c.blocks_w)), mode="edge")
        mask = np.ones((ph, pw), dtype=bool)
        mask[: c.blocks_h, : c.blocks_w] = False
        c.coef[mask] = 0
        c.coef[mask, 0] = dc[mask]


def _bitlen(a: np.ndarray) -> np.ndarray:
    return np.frexp(a.astype(np.float64))[1].astype(np.int64)


def _optimal_table(freq: np.ndarray) -> tuple[list[int], list[int]]:
    """Annex K.2 optimal Huffman table (the procedure libjpeg uses): (code-length counts, symbols)."""
    f = [int(x) for x in freq] + [1]  # symbol 256 reserves the all-ones code
    codesize = [0] * 257
    others = [-1] * 257
    while True:
        c1, v = -1, 1_000_000_000
        for i in range(257):
            if f[i] and f[i] <= v:
                v, c1 = f[i], i
        c2, v = -1, 1_000_000_000
        for i in range(257):
            if f[i] and f[i] <= v and i != c1:
                v, c2 = f[i], i
        if c2 < 0:
            break
        f[c1] += f[c2]
        f[c2] = 0
        codesize[c1] += 1
        while others[c1] >= 0:
            c1 = others[c1]
            codesize[c1] += 1
        others[c1] = c2
        codesize[c2] += 1
        while others[c2] >= 0:
            c2 = others[c2]
            codesize[c2] += 1
    bits = [0] * 33
    for i in range(257):
        if codesize[i]:
            bits[codesize[i]] += 1
    for i in range(32, 16, -1):
        while bits[i] > 0:
            j = i - 2
            while bits[j] == 0:
                j -= 1
            bits[i] -= 2
            bits[i - 1] += 1
            bits[j + 1] += 2
            bits[j] -= 1
    i = 16
    while bits[i] == 0:
        i -= 1
    bits[i] -= 1  # drop the reserved symbol
    symbols = [j for size in range(1, 33) for j in range(256) if codesize[j] == size]
    return bits[1:17], symbols


def _codes(counts: list[int], symbols: list[int]) -> tuple[np.ndarray, np.ndarray]:
    code = np.zeros(256, dtype=np.int64)
    size = np.zeros(256, dtype=np.int64)
    c = k = 0
    for length in range(1, 17):
        for _ in range(counts[length - 1]):
            code[symbols[k]] = c
            size[symbols[k]] = length
            k += 1
            c += 1
        c <<= 1
    return code, size


def _extra(vals: np.ndarray, sizes: np.ndarray) -> np.ndarray:
    return np.where(vals < 0, vals + (1 << sizes) - 1, vals)


def _emissions(zz: np.ndarray, seq: np.ndarray):
    """Huffman symbols and raw bits of every block of one component, with keys that restore scan order."""
    dc = zz[:, 0].astype(np.int64)
    diff = np.diff(dc, prepend=0)
    if np.abs(diff).max(initial=0) >= _MAX_DC_DIFF:
        raise JpegError("DC difference is too large for an 8-bit image")
    ds = _bitlen(np.abs(diff))
    dc_part = (ds, _extra(diff, ds), ds, seq * 512)
    ac = zz[:, 1:]
    nzb, nzk = np.nonzero(ac)
    k = nzk.astype(np.int64) + 1
    vals = ac[nzb, nzk].astype(np.int64)
    if np.abs(vals).max(initial=0) >= _MAX_AC:
        raise JpegError("AC coefficient is too large for an 8-bit image")
    sz = _bitlen(np.abs(vals))
    nz_seq = seq[nzb]
    prev = np.zeros_like(k)
    if k.size > 1:
        prev[1:] = np.where(nzb[1:] == nzb[:-1], k[:-1], 0)
    run = k - prev - 1
    syms = ((run & 15) << 4) | sz
    zrl = run >> 4
    last = np.zeros(zz.shape[0], dtype=np.int64)
    if k.size:
        is_last = np.ones(k.size, dtype=bool)
        is_last[:-1] = nzb[1:] != nzb[:-1]
        last[nzb[is_last]] = k[is_last]
    eob_blocks = np.nonzero(last < 63)[0]
    rep = np.repeat(np.arange(k.size), zrl)
    ac_part = (
        np.concatenate([syms, np.full(rep.size, 0xF0), np.zeros(eob_blocks.size, dtype=np.int64)]),
        np.concatenate([_extra(vals, sz), np.zeros(rep.size + eob_blocks.size, dtype=np.int64)]),
        np.concatenate([sz, np.zeros(rep.size + eob_blocks.size, dtype=np.int64)]),
        np.concatenate([nz_seq * 512 + 4 * k + 1, nz_seq[rep] * 512 + 4 * k[rep], seq[eob_blocks] * 512 + 256]),
    )
    return dc_part, ac_part


def _pack(vals: np.ndarray, lens: np.ndarray) -> bytes:
    """Concatenate (value, bit length) tokens, most significant bit first, padding the last byte with ones."""
    keep = lens > 0
    vals, lens = vals[keep], lens[keep]
    total = int(lens.sum())
    pad = (-total) % 8
    if pad:
        vals = np.append(vals, (1 << pad) - 1)
        lens = np.append(lens, pad)
        total += pad
    start = np.cumsum(lens) - lens
    word = start >> 5
    shifted = vals.astype(np.uint64) << (64 - (start & 31) - lens).astype(np.uint64)
    n_words = (total + 31) // 32 + 1
    hi = np.bincount(word, weights=(shifted >> np.uint64(32)).astype(np.float64), minlength=n_words)
    lo = np.bincount(word + 1, weights=(shifted & np.uint64(0xFFFFFFFF)).astype(np.float64), minlength=n_words)
    words = (hi[:n_words] + lo[:n_words]).astype(np.uint64).astype(">u4")
    return words.tobytes()[: total // 8]


def _segment(marker: int, payload: bytes) -> bytes:
    return bytes((0xFF, marker)) + (len(payload) + 2).to_bytes(2, "big") + payload


def write(frame: Frame) -> bytes:
    """The canonical form of ``frame``: DQT, SOF, DHT and one interleaved baseline scan (no markers around it)."""
    comps = frame.comps
    if sum(c.h * c.v for c in comps) > 10:
        raise JpegError("too many blocks in an MCU for an interleaved scan")
    canonicalise_padding(frame)
    nonzero = sum(int(np.count_nonzero(c.coef)) for c in comps)
    need = _WRITE_BYTES_PER_PIXEL * frame.width * frame.height + _WRITE_BYTES_PER_COEFFICIENT * nonzero
    if need > DCT_MEMORY_BUDGET:
        raise JpegError(
            f"too large for the canonical writer ({frame.width}x{frame.height} pixels, {nonzero / 1e6:.1f} million "
            f"non-zero coefficients would need about {need / (1 << 30):.1f} GiB of memory)"
        )
    per_mcu = sum(c.h * c.v for c in comps)
    sets = [0 if i == 0 else 1 for i in range(len(comps))]
    dc_hist = [np.zeros(256, dtype=np.int64) for _ in range(2)]
    ac_hist = [np.zeros(256, dtype=np.int64) for _ in range(2)]
    parts = []
    offset = 0
    n_mcus = frame.mcus_x * frame.mcus_y
    for i, c in enumerate(comps):
        ph, pw = c.coef.shape[:2]
        blocks = c.coef.reshape(frame.mcus_y, c.v, frame.mcus_x, c.h, 64).transpose(0, 2, 1, 3, 4).reshape(-1, 64)
        zz = blocks[:, _ZIG]
        t = np.arange(n_mcus * c.h * c.v, dtype=np.int64)
        seq = (t // (c.h * c.v)) * per_mcu + offset + t % (c.h * c.v)
        offset += c.h * c.v
        dc_part, ac_part = _emissions(zz, seq)
        dc_hist[sets[i]] += np.bincount(dc_part[0], minlength=256)
        ac_hist[sets[i]] += np.bincount(ac_part[0], minlength=256)
        parts.append((sets[i], dc_part, ac_part))
    used = sorted(set(sets))
    dc_tabs = {s: _optimal_table(dc_hist[s]) for s in used}
    ac_tabs = {s: _optimal_table(ac_hist[s]) for s in used}
    dc_codes = {s: _codes(*dc_tabs[s]) for s in used}
    ac_codes = {s: _codes(*ac_tabs[s]) for s in used}
    code, clen, extra, elen, key = [], [], [], [], []
    for s, dc_part, ac_part in parts:
        for (sym, ex, el, ky), (cv, cs) in ((dc_part, dc_codes[s]), (ac_part, ac_codes[s])):
            code.append(cv[sym])
            clen.append(cs[sym])
            extra.append(ex)
            elen.append(el)
            key.append(ky)
    key = np.concatenate(key)
    order = np.argsort(key, kind="stable")
    code, clen = np.concatenate(code)[order], np.concatenate(clen)[order]
    extra, elen = np.concatenate(extra)[order], np.concatenate(elen)[order]
    vals = np.stack([code, extra], axis=1).reshape(-1)
    lens = np.stack([clen, elen], axis=1).reshape(-1)
    entropy = _pack(vals, lens).replace(b"\xff", b"\xff\x00")

    tables: list[np.ndarray] = []
    tq_of = []
    for c in comps:
        for j, t in enumerate(tables):
            if np.array_equal(t, c.qt):
                tq_of.append(j)
                break
        else:
            tq_of.append(len(tables))
            tables.append(c.qt)
    if len(tables) > 4:
        raise JpegError("too many distinct quantisation tables")
    wide = any(int(t.max()) > 255 for t in tables)
    out = bytearray()
    for j, t in enumerate(tables):
        zzq = t[_ZIG]
        if wide:
            out += _segment(0xDB, bytes((0x10 | j,)) + zzq.astype(">u2").tobytes())
        else:
            out += _segment(0xDB, bytes((j,)) + zzq.astype(np.uint8).tobytes())
    rgb = [c.ident for c in comps] == [82, 71, 66]
    ids = [c.ident for c in comps] if rgb else list(range(1, len(comps) + 1))
    sof = bytes((8,)) + frame.height.to_bytes(2, "big") + frame.width.to_bytes(2, "big") + bytes((len(comps),))
    for c, ident, tq in zip(comps, ids, tq_of, strict=True):
        sof += bytes((ident, (c.h << 4) | c.v, tq))
    out += _segment(0xC1 if wide else 0xC0, sof)
    for s in used:
        for tc, tab in ((0, dc_tabs[s]), (1, ac_tabs[s])):
            counts, symbols = tab
            out += _segment(0xC4, bytes(((tc << 4) | s,)) + bytes(counts) + bytes(symbols))
    sos = bytes((len(comps),))
    for ident, s in zip(ids, sets, strict=True):
        sos += bytes((ident, (s << 4) | s))
    sos += bytes((0, 63, 0))
    out += _segment(0xDA, sos)
    out += entropy
    return bytes(out)


def same_coefficients(a: Frame, b: Frame) -> bool:
    """True when two frames decode to the same picture data: geometry, tables and every coefficient."""
    if (a.width, a.height, a.mcus_x, a.mcus_y, len(a.comps)) != (b.width, b.height, b.mcus_x, b.mcus_y, len(b.comps)):
        return False
    return all(
        x.h == y.h and x.v == y.v and np.array_equal(x.qt, y.qt) and np.array_equal(x.coef, y.coef)
        for x, y in zip(a.comps, b.comps, strict=True)
    )


# --------------------------------------------------------------------------
# Scrubbing
# --------------------------------------------------------------------------


@dataclass
class ScrubStats:
    changed: int = 0  # coefficients whose value changed
    nonzero: int = 0  # non-zero coefficients before
    psnr: float | None = None  # estimated PSNR (dB) of the scrubbed picture against the input, None if unchanged
    per_component: list[float] = field(default_factory=list)
    before: list[np.ndarray] = field(default_factory=list, repr=False)  # each component's coefficients before


def scrub(frame: Frame, rng: np.random.Generator) -> ScrubStats:
    """Destroy payloads stored in the parity of quantised DCT coefficients, in place.

    Every coefficient (DC and AC, every component) is mapped so that its low bit no longer depends on the
    input, which is all any LSB-reading extractor sees.  Coefficients are grouped in the pairs ``{2k, 2k+1}``
    that a two's-complement LSB flip (``c ^ 1``) moves between, and the output depends on the pair only:

    * the pairs ``{-2, -1}`` and ``{0, 1}`` become 0.  This keeps the pair intact and is also the cheapest map
      that makes the *support* of the coefficients (which zeros became +-1) independent of the payload, the
      channel used by embedders that also change zeros (J-UNIWARD, UERD).  It costs one or two quantisation
      steps on each such coefficient and shrinks the file.
    * every other pair is re-drawn: ``(c & ~1) | r`` with ``r`` a fresh random bit.  The pair (everything an
      LSB replacement leaves alone) is kept and the low bit, in both the two's-complement and the
      sign-magnitude reading, is uniformly random.

    The picture error is exactly ``sum((dc * q)**2)`` per block (the JPEG DCT is orthonormal); the estimate
    in ``ScrubStats.psnr`` converts it to RGB for YCbCr images.  Blocks outside the picture are left to
    ``canonicalise_padding``.
    """
    stats = ScrubStats()
    mse = []
    for c in frame.comps:
        old = c.coef
        pair = old >> 1
        flip = rng.integers(0, 2, size=old.shape, dtype=np.int16)
        new = np.where((pair == 0) | (pair == -1), 0, (old & np.int16(-2)) | flip).astype(np.int16)
        # The pair {-1024, -1023} would let the draw land on -1024, which no 8-bit picture can hold in an AC
        # position; it stays -1023, still a function of the pair alone.
        new[..., 1:] = np.maximum(new[..., 1:], -(_MAX_AC - 1))
        err = (new.astype(np.float64) - old) * c.qt.astype(np.float64)
        visible = err[: c.blocks_h, : c.blocks_w]
        mse.append(float((visible**2).sum() / (c.blocks_h * c.blocks_w * 64)))
        stats.changed += int((new != old).sum())
        stats.nonzero += int((old != 0).sum())
        stats.before.append(old)
        c.coef = new
    stats.per_component = mse
    if len(frame.comps) == 3:  # YCbCr to RGB: the squared gains of each channel on Y, Cb, Cr
        total = (3 * mse[0] + 3.258 * mse[1] + 2.475 * mse[2]) / 3
    else:
        total = sum(mse) / len(mse)
    stats.psnr = round(10 * float(np.log10(255.0**2 / total)), 2) if total > 0 else None
    return stats


def visible_equal(a: Frame, b: Frame) -> bool:
    """Same geometry, tables and every coefficient of every block that is part of the picture."""
    if (a.width, a.height, a.mcus_x, a.mcus_y, len(a.comps)) != (b.width, b.height, b.mcus_x, b.mcus_y, len(b.comps)):
        return False
    return all(
        x.h == y.h
        and x.v == y.v
        and np.array_equal(x.qt, y.qt)
        and np.array_equal(x.coef[: x.blocks_h, : x.blocks_w], y.coef[: y.blocks_h, : y.blocks_w])
        for x, y in zip(a.comps, b.comps, strict=True)
    )


def check_scrub(frame: Frame, before: list[np.ndarray]) -> str | None:
    """None when ``frame`` (read back from the written file) is exactly what ``scrub`` promises, else why not.

    Every visible coefficient must be 0 where the input was -2..1, and elsewhere must share its two's-complement
    pair with the input (``c >> 1``), so only the low bit may differ.
    """
    for i, (c, old) in enumerate(zip(frame.comps, before, strict=True)):
        new = c.coef[: c.blocks_h, : c.blocks_w].astype(np.int32)
        old = old[: c.blocks_h, : c.blocks_w].astype(np.int32)
        small = (old >= -2) & (old <= 1)
        if (new[small] != 0).any():
            return f"component {i}: a coefficient between -2 and 1 survived"
        if ((new[~small] >> 1) != (old[~small] >> 1)).any():
            return f"component {i}: a coefficient moved by more than its low bit"
    return None


def psnr(a: np.ndarray, b: np.ndarray) -> float | None:
    """PSNR in dB between two decoded 8-bit pictures of the same shape (None when identical)."""
    if a.shape != b.shape:
        raise ValueError("pictures differ in shape")
    sse = 0.0
    rows = max(1, (1 << 22) // max(1, a[0].size))
    for top in range(0, a.shape[0], rows):
        d = a[top : top + rows].astype(np.float64) - b[top : top + rows].astype(np.float64)
        sse += float((d * d).sum())
    if sse == 0:
        return None
    return round(10 * float(np.log10(255.0**2 / (sse / a.size))), 2)
