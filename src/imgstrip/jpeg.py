"""Lossless JPEG rebuilding.

Instead of decoding and re-encoding, the file is reassembled from scratch
using only the segments a decoder needs (quantisation and Huffman tables,
frame header, restart interval, scans).  Every APPn segment, comment,
padding byte and anything after the end-of-image marker is dropped, then a
fresh minimal header is written.  The compressed image data itself is copied
bit-for-bit, so there is no generation loss.
"""

from __future__ import annotations

import struct

# Segments required to decode the image.  DNL (0xDC) is skipped by decoders and DHP/EXP (0xDE/0xDF) are
# unsupported, so they can only carry hidden data.  DAC (0xCC) is used only by arithmetic-coded frames and is
# rewritten from its parsed entries instead of being copied.
_DAC = 0xCC
_ARITHMETIC_SOF = {0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
_DHT, _DQT = 0xC4, 0xDB
# Copied as they are (their payload is trimmed to its defined length).  DQT and DHT are rewritten from the tables a
# scan actually uses, so a table that is defined but never used, or redefined before use, cannot carry data.
_KEEP = {m for m in range(0xC0, 0xD0) if m not in (0xC8, _DAC, _DHT)} | {0xDD}
_JFIF = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"


class JpegError(ValueError):
    pass


def orientation_exif(orientation: int) -> bytes:
    """APP1 payload holding nothing but the orientation tag."""
    ifd = struct.pack(">H", 1) + struct.pack(">HHIHH", 0x0112, 3, 1, orientation, 0) + b"\x00\x00\x00\x00"
    return b"Exif\x00\x00" + b"MM\x00\x2a\x00\x00\x00\x08" + ifd


def _segment(marker: int, payload: bytes) -> bytes:
    if len(payload) + 2 > 0xFFFF:
        raise JpegError("segment too large")
    return bytes((0xFF, marker)) + struct.pack(">H", len(payload) + 2) + payload


def _quant_tables(payload: bytes) -> dict[int, bytes]:
    """Slot -> defining entry (precision/slot byte plus table) for each table in a DQT segment."""
    out: dict[int, bytes] = {}
    i = 0
    while i < len(payload):
        pq, tq = payload[i] >> 4, payload[i] & 15
        size = 1 + 64 * (pq + 1)
        if pq > 1 or tq > 3 or i + size > len(payload):
            raise JpegError("malformed quantisation table")
        out[tq] = payload[i : i + size]
        i += size
    return out


def _huffman_tables(payload: bytes) -> dict[int, bytes]:
    """(class << 4 | slot) -> defining entry (class/slot byte, 16 counts, symbols) for each table in a DHT segment."""
    out: dict[int, bytes] = {}
    i = 0
    while i < len(payload):
        if i + 17 > len(payload) or payload[i] >> 4 > 1 or payload[i] & 15 > 3:
            raise JpegError("malformed Huffman table")
        size = 17 + sum(payload[i + 1 : i + 17])
        if size > 17 + 256 or i + size > len(payload):
            raise JpegError("malformed Huffman table")
        out[payload[i]] = payload[i : i + size]
        i += size
    return out


def _scan_tables(marker_sof: int, scan: bytes) -> tuple[list[int], list[int]]:
    """(component ids, Huffman table keys the scan decodes with) from a scan header."""
    if len(scan) < 1 or len(scan) < 4 + 2 * scan[0]:
        raise JpegError("malformed scan header")
    ns = scan[0]
    ids = [scan[1 + 2 * i] for i in range(ns)]
    selectors = [scan[2 + 2 * i] for i in range(ns)]
    ss, ah = scan[1 + 2 * ns], scan[3 + 2 * ns] >> 4
    keys: list[int] = []
    if marker_sof in (0xC2, 0xC6, 0xCA, 0xCE):  # progressive: a scan uses the DC or the AC tables, never both
        if ss == 0:
            if ah == 0:
                keys = [t >> 4 for t in selectors]
        else:
            keys = [0x10 | (t & 15) for t in selectors]
    else:
        keys = [t >> 4 for t in selectors] + [0x10 | (t & 15) for t in selectors]
    return ids, sorted(set(keys))


def rebuild(
    data: bytes, orientation: int = 1, icc: bytes | None = None, canonical: bytes | None = None
) -> tuple[bytes, int]:
    """Return (clean JPEG, number of embedded images dropped after the main image).

    ``canonical`` replaces the copied tables, frame header and scans with segments built elsewhere (the
    canonical form from ``dct.write``); everything around them is still written here.
    """
    if not data.startswith(b"\xff\xd8"):
        raise JpegError("not a JPEG file")
    n = len(data)
    pos = 2
    body = bytearray()
    had_jfif = False
    adobe_transform = None
    sequential = False
    arithmetic = False
    dac: dict[int, int] = {}  # conditioning table (class/id byte) -> value, last definition wins
    qt_defs: dict[int, bytes] = {}  # table definitions in force at this point of the file
    ht_defs: dict[int, bytes] = {}
    qt_written: dict[int, bytes] = {}  # what the output holds so far
    ht_written: dict[int, bytes] = {}
    frame_sof = 0
    frame_comps: list[tuple[int, int]] = []  # (component id, quantisation slot)
    latched: set[int] = set()  # components whose quantisation table has been written
    while True:
        if pos >= n:
            raise JpegError("missing end-of-image marker")
        if data[pos] != 0xFF:
            nxt = data.find(b"\xff", pos)  # skip stray bytes between segments
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
        (length,) = struct.unpack_from(">H", data, pos)
        if length < 2 or pos + length > n:
            raise JpegError("segment overruns the file")
        payload = data[pos + 2 : pos + length]
        pos += length
        if marker == 0xE0 and payload.startswith(b"JFIF\x00"):
            had_jfif = True
        elif marker == 0xEE and payload.startswith(b"Adobe") and len(payload) >= 12:
            adobe_transform = payload[11]
        elif marker == _DQT:
            qt_defs.update(_quant_tables(payload))
        elif marker == _DHT:
            ht_defs.update(_huffman_tables(payload))
        elif marker == 0xDA:
            ids, keys = _scan_tables(frame_sof, payload)
            payload = payload[: 4 + 2 * payload[0]]  # nothing after the defined scan header
            quant = b""
            for ident in ids:
                comp = [c for c in frame_comps if c[0] == ident]
                if not comp:
                    raise JpegError("scan names an unknown component")
                tq = comp[0][1]
                if ident not in latched:
                    latched.add(ident)
                    if tq in qt_defs and qt_written.get(tq) != qt_defs[tq]:
                        qt_written[tq] = qt_defs[tq]
                        quant += qt_defs[tq]
            if quant:
                body += _segment(_DQT, quant)
            huff = b""
            if frame_sof not in (0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                for key in keys:
                    if key in ht_defs and ht_written.get(key) != ht_defs[key]:
                        ht_written[key] = ht_defs[key]
                        huff += ht_defs[key]
            if huff:
                body += _segment(_DHT, huff)
            if sequential and len(payload) >= 4:
                # A sequential scan always covers coefficients 0..63 in one pass, so decoders ignore these three
                # bytes; write their standard values rather than copy whatever the original put there.
                payload = payload[:-3] + b"\x00\x3f\x00"
            body += _segment(marker, payload)
            end = _scan_end(data, pos)
            body += data[pos:end]
            pos = end
        elif marker == _DAC:
            if len(payload) % 2:
                raise JpegError("malformed arithmetic conditioning table")
            for i in range(0, len(payload), 2):
                dac[payload[i]] = payload[i + 1]
        elif marker in _KEEP:
            if marker == 0xDD:
                payload = payload[:2]
            else:
                if len(payload) < 6 or len(payload) < 6 + 3 * payload[5]:
                    raise JpegError("short frame header")
                payload = payload[: 6 + 3 * payload[5]]
                frame_sof = marker
                frame_comps = [(payload[6 + 3 * i], payload[8 + 3 * i]) for i in range(payload[5])]
            if marker in (0xC0, 0xC1):
                sequential = True
            if marker in _ARITHMETIC_SOF:
                arithmetic = True
            body += _segment(marker, payload)
    if canonical is not None:
        body = bytearray(canonical)
    elif arithmetic and dac:
        # Conditioning tables only matter to arithmetic decoders; write one canonical segment before the frame.
        body[0:0] = _segment(_DAC, b"".join(bytes((k, v)) for k, v in sorted(dac.items())))
    dropped = _count_images(data, pos)
    out = bytearray(b"\xff\xd8")
    if had_jfif or adobe_transform is None:
        out += _JFIF
    if orientation != 1:
        out += _segment(0xE1, orientation_exif(orientation))
    if icc:
        chunk = 65519
        parts = [icc[i : i + chunk] for i in range(0, len(icc), chunk)]
        if len(parts) > 255:
            raise JpegError("ICC profile too large")
        for i, part in enumerate(parts, 1):
            out += _segment(0xE2, b"ICC_PROFILE\x00" + bytes((i, len(parts))) + part)
    if adobe_transform is not None:
        out += _segment(0xEE, b"Adobe" + struct.pack(">HHHB", 100, 0, 0, adobe_transform))
    out += body
    out += b"\xff\xd9"
    return bytes(out), dropped


def _count_images(data: bytes, pos: int) -> int:
    """How many JPEG images follow ``pos`` (MPF secondary images, appended previews).

    Each image is walked to its end-of-image marker, so a thumbnail inside its EXIF block is not counted again.
    """
    count = 0
    start = data.find(b"\xff\xd8\xff", pos)
    while start >= 0:
        count += 1
        end = _image_end(data, start)
        start = data.find(b"\xff\xd8\xff", end if end is not None else start + 3)
    return count


def _image_end(data: bytes, start: int) -> int | None:
    """Offset just past the end-of-image marker of the JPEG at ``start``, or None if it is not well formed."""
    n = len(data)
    pos = start + 2
    try:
        while pos + 1 < n and data[pos] == 0xFF:
            while pos < n and data[pos] == 0xFF:
                pos += 1
            marker = data[pos]
            pos += 1
            if marker == 0xD9:
                return pos
            if 0xD0 <= marker <= 0xD7 or marker == 0x01:
                continue
            (length,) = struct.unpack_from(">H", data, pos)
            if length < 2:
                return None
            pos += length
            if marker == 0xDA:
                pos = _scan_end(data, pos)
    except (struct.error, IndexError, JpegError):
        pass
    return None


def _scan_end(data: bytes, pos: int) -> int:
    """Offset of the first marker after entropy-coded data (RSTn markers are part of the scan)."""
    n = len(data)
    while True:
        i = data.find(b"\xff", pos)
        if i < 0 or i + 1 >= n:
            raise JpegError("truncated entropy-coded data")
        b = data[i + 1]
        if b == 0x00 or 0xD0 <= b <= 0xD7:
            pos = i + 2
            continue
        return i
