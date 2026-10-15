"""Test helper: name the structural region every byte of an output file belongs to.

The coverage audit flips bytes of a clean file and asks whether anything notices.  These walkers are written
independently of ``imgstrip.inspect`` (they only name regions, they never judge) so that the audit can say
*where* a byte nothing notices sits, and ``ALLOWED`` states, per format, the only places where that is accepted.
"""

from __future__ import annotations

import struct


def span_of(spans: list[tuple[int, int, str]], offset: int) -> tuple[str, int]:
    for start, end, name in spans:
        if start <= offset < end:
            return name, offset - start
    return "unmapped", 0


def png(d: bytes) -> list[tuple[int, int, str]]:
    spans, pos = [(0, 8, "signature")], 8
    while pos + 12 <= len(d):
        (n,) = struct.unpack_from(">I", d, pos)
        kind = d[pos + 4 : pos + 8].decode("latin-1")
        spans += [
            (pos, pos + 8, f"{kind} header"),
            (pos + 8, pos + 8 + n, kind),
            (pos + 8 + n, pos + 12 + n, f"{kind} crc"),
        ]
        pos += 12 + n
    return spans


def jpeg(d: bytes) -> list[tuple[int, int, str]]:
    names = {
        0xE0: "JFIF",
        0xE1: "APP1",
        0xE2: "APP2",
        0xEE: "APP14",
        0xDB: "DQT",
        0xC0: "SOF",
        0xC1: "SOF",
        0xC2: "SOF",
        0xC4: "DHT",
        0xDA: "SOS",
        0xDD: "DRI",
    }
    spans, i = [(0, 2, "SOI")], 2
    while i < len(d):
        marker = d[i + 1]
        if marker == 0xD9:
            spans.append((i, i + 2, "EOI"))
            break
        (n,) = struct.unpack_from(">H", d, i + 2)
        spans.append((i, i + 2 + n, names.get(marker, f"marker {marker:#x}")))
        if marker == 0xDA:
            j = i + 2 + n
            while True:
                k = d.find(b"\xff", j)
                if d[k + 1] == 0 or 0xD0 <= d[k + 1] <= 0xD7:
                    j = k + 2
                    continue
                break
            spans.append((i + 2 + n, k, "scan data"))
            i = k
            continue
        i += 2 + n
    return spans


def gif(d: bytes) -> list[tuple[int, int, str]]:
    spans, i, packed = [(0, 6, "header"), (6, 13, "screen")], 13, d[10]
    if packed & 0x80:
        n = 3 * (2 << (packed & 7))
        spans.append((i, i + n, "global colour table"))
        i += n
    while i < len(d):
        block = d[i]
        if block == 0x3B:
            spans.append((i, i + 1, "trailer"))
            break
        if block == 0x21:
            j = i + 2
            while d[j]:
                j += d[j] + 1
            spans.append((i, j + 1, "extension"))
            i = j + 1
        elif block == 0x2C:
            flags, j = d[i + 9], i + 10
            spans.append((i, j, "frame header"))
            if flags & 0x80:
                n = 3 * (2 << (flags & 7))
                spans.append((j, j + n, "local colour table"))
                j += n
            spans.append((j, j + 1, "LZW code size"))
            s, j = j + 1, j + 1
            while d[j]:
                j += d[j] + 1
            spans.append((s, j + 1, "LZW data"))
            i = j + 1
        else:
            break
    return spans


def riff(d: bytes) -> list[tuple[int, int, str]]:
    spans, i = [(0, 12, "RIFF header")], 12
    while i + 8 <= len(d):
        (n,) = struct.unpack_from("<I", d, i + 4)
        kind = d[i : i + 4].decode("latin-1").strip()
        spans += [(i, i + 8, f"{kind} header"), (i + 8, i + 8 + n, f"{kind} body")]
        if n & 1:
            spans.append((i + 8 + n, i + 9 + n, f"{kind} padding"))
        i += 8 + n + (n & 1)
    return spans


def bmp(d: bytes) -> list[tuple[int, int, str]]:
    pix, dib = struct.unpack_from("<I", d, 10)[0], struct.unpack_from("<I", d, 14)[0]
    return [
        (0, 14, "file header"),
        (14, 14 + dib, "DIB header"),
        (14 + dib, pix, "palette"),
        (pix, len(d), "pixel rows"),
    ]


_TIFF_TAGS = {
    256: "Width",
    257: "Length",
    258: "BitsPerSample",
    259: "Compression",
    262: "Photometric",
    273: "StripOffsets",
    277: "SamplesPerPixel",
    278: "RowsPerStrip",
    279: "StripByteCounts",
    284: "PlanarConfig",
    317: "Predictor",
    320: "ColorMap",
    338: "ExtraSamples",
    339: "SampleFormat",
}


def tiff(d: bytes) -> list[tuple[int, int, str]]:
    e = "<" if d[:2] == b"II" else ">"
    spans = [(0, 8, "file header")]
    ifd = struct.unpack_from(e + "I", d, 4)[0]
    while ifd:
        count = struct.unpack_from(e + "H", d, ifd)[0]
        spans.append((ifd, ifd + 2, "entry count"))
        tags = {}
        for k in range(count):
            o = ifd + 2 + 12 * k
            tag, typ, c = struct.unpack_from(e + "HHI", d, o)
            name = _TIFF_TAGS.get(tag, f"tag {tag}")
            size = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}.get(typ, 1) * c
            tags[tag] = (typ, c, o)
            spans += [(o, o + 4, f"{name} tag and type"), (o + 4, o + 8, f"{name} count")]
            if size <= 4:
                spans += [(o + 8, o + 8 + size, f"{name} value"), (o + 8 + size, o + 12, f"{name} padding")]
            else:
                off = struct.unpack_from(e + "I", d, o + 8)[0]
                spans += [(o + 8, o + 12, f"{name} offset"), (off, off + size, f"{name} value")]
        if 273 in tags and 279 in tags:

            def values(tag):
                typ, c, o = tags[tag]
                code = {3: "H", 4: "I"}[typ]
                if c * struct.calcsize(code) <= 4:
                    return struct.unpack_from(e + f"{c}{code}", d, o + 8)
                return struct.unpack_from(e + f"{c}{code}", d, struct.unpack_from(e + "I", d, o + 8)[0])

            spans += [(so, so + n, "strip data") for so, n in zip(values(273), values(279), strict=True)]
        spans.append((ifd + 2 + 12 * count, ifd + 6 + 12 * count, "next directory"))
        ifd = struct.unpack_from(e + "I", d, ifd + 2 + 12 * count)[0]
    return spans


_CONTAINERS = {
    b"meta": 4,
    b"iprp": 0,
    b"ipco": 0,
    b"iinf": 6,
    b"dinf": 0,
    b"moov": 0,
    b"trak": 0,
    b"mdia": 0,
    b"minf": 0,
    b"stbl": 0,
    b"iref": 4,
}


def isobmff(d: bytes, start: int = 0, end: int | None = None) -> list[tuple[int, int, str]]:
    """Box headers (size and type) and everything else, which is a box body."""
    end = len(d) if end is None else end
    spans, i = [], start
    while i + 8 <= end:
        (n,) = struct.unpack_from(">I", d, i)
        kind = d[i + 4 : i + 8]
        n = end - i if n == 0 else n
        if n < 8:
            break
        spans.append((i, i + 8, "box header"))
        if kind in _CONTAINERS:
            skip = _CONTAINERS[kind]
            if kind == b"iinf":
                skip = 6
            spans.append((i + 8, i + 8 + skip, "box body"))
            spans += isobmff(d, i + 8 + skip, i + n)
        else:
            spans.append((i + 8, i + n, "box body"))
        i += n
    return spans


WALKERS = {
    "PNG": png,
    "JPEG": jpeg,
    "GIF": gif,
    "WEBP": riff,
    "BMP": bmp,
    "TIFF": tiff,
    "AVIF": isobmff,
    "HEIF": isobmff,
}

# Where a byte may change without the picture changing and without anything noticing, and why that is accepted.
# Everything not listed here must be accounted for: a byte there that nothing notices is a finding.


def allowed(fmt: str, region: str, rel: int) -> str | None:
    """The reason this slack is accepted, or None when it is not."""
    if fmt == "PNG":
        if region in ("IDAT", "fdAT"):
            return "inside the deflate stream: header, block structure and padding bits (encoder's own choices)"
        if region == "acTL" and 4 <= rel < 8:
            return "loop count (kept on purpose)"
        if region == "fcTL" and 20 <= rel < 26:
            return "frame delay, and dispose/blend flags that make no difference to full-canvas opaque frames"
    if fmt == "JPEG":
        if region == "DQT" or region == "DHT":
            return "quantisation and Huffman table entries no coefficient uses"
        if region == "scan data":
            return "padding bits after the last Huffman code"
        if region == "SOF" and rel == 1:
            return "baseline and extended-sequential frames decode alike"
        if region == "JFIF" and rel == 10:
            return "JFIF minor version 1.00, 1.01 or 1.02"
    if fmt == "GIF":
        if region == "LZW data":
            return "inside the LZW stream: padding bits and code-table redundancy"
        if region == "screen" and rel == 4:
            return "colour resolution and sort flag"
        if region == "extension" and rel in (3, 4, 5, 6, 16, 17):
            return "frame delay and disposal, and the loop count (kept on purpose)"
        if region == "frame header" and rel == 9:
            return "sort flag"
    if fmt == "WEBP" and region in ("VP8L body", "VP8 body", "ALPH body"):
        return "inside the coded bitstream: header bits, padding and table redundancy"
    if fmt == "BMP" and region == "DIB header" and 32 <= rel < 40:
        return "colours used and important colours: 0 means every entry, so 0 and the palette size read alike"
    if fmt == "TIFF":
        if region == "strip data":
            return "inside the compressed strip: padding bits"
        if region.endswith(("tag and type", " count")) or region == "RowsPerStrip value":
            return "how a value is represented (SHORT or LONG, how many) and rows per strip when one strip holds everything"
    if fmt in ("AVIF", "HEIF") and region == "box body":
        return "full-box version and flags, brands, handler names, codec configuration and the coded payload"
    return None
