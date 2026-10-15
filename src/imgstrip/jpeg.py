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

# Segments required to decode the image.
_KEEP = {m for m in range(0xC0, 0xD0) if m != 0xC8} | {0xDB, 0xDC, 0xDD, 0xDE, 0xDF}
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


def rebuild(data: bytes, orientation: int = 1, icc: bytes | None = None) -> tuple[bytes, int]:
    """Return (clean JPEG, number of embedded images dropped after the main image)."""
    if not data.startswith(b"\xff\xd8"):
        raise JpegError("not a JPEG file")
    n = len(data)
    pos = 2
    body = bytearray()
    had_jfif = False
    adobe_transform = None
    sequential = False
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
        elif marker == 0xDA:
            if sequential and len(payload) >= 4:
                # A sequential scan always covers coefficients 0..63 in one pass, so decoders ignore these three
                # bytes; write their standard values rather than copy whatever the original put there.
                payload = payload[:-3] + b"\x00\x3f\x00"
            body += _segment(marker, payload)
            end = _scan_end(data, pos)
            body += data[pos:end]
            pos = end
        elif marker in _KEEP:
            if marker in (0xC0, 0xC1):
                sequential = True
            body += _segment(marker, payload)
    trailing = data[pos:]
    dropped = trailing.count(b"\xff\xd8\xff")
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
