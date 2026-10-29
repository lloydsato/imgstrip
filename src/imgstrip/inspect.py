"""Container-level inspection of image files.

Every parser in this module walks the raw byte structure of a file (JPEG
segments, PNG chunks, RIFF chunks, GIF blocks, TIFF IFDs, BMP headers,
ISO-BMFF boxes) and reports everything that is *not* strictly required to
render the pixels.  The same code serves two purposes:

* telling the user what an input file carries (``imgstrip --check``), and
* fail-closed verification that a file produced by imgstrip contains nothing
  but pixel data: any finding, or any structure the parser does not fully
  understand, makes verification fail.
"""

from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass, field

METADATA = "metadata"  # descriptive / identifying data (EXIF, XMP, comments, ...)
HIDDEN = "hidden"  # payload that is not part of the visible image
COLOR = "color"  # colour-management data (ICC profiles, gamma, ...)
STRUCTURE = "structure"  # anomalies: bad checksums, unknown structures

_MAX_TEXT_INFLATE = 64 << 20

# Every container reports an embedded C2PA manifest store with this text in its label, so the
# credential code can tell "the credential we just wrote" from every other kind of metadata.
C2PA_LABEL = "C2PA content credentials"
C2PA_BMFF_UUID = bytes.fromhex("d8fec3d61b0e483c92975828877ec481")


def is_c2pa_finding(finding: Finding) -> bool:
    return C2PA_LABEL in finding.label


@dataclass(frozen=True)
class Finding:
    category: str
    label: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.label}: {self.detail}" if self.detail else self.label


@dataclass
class Report:
    format: str | None
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None
    info: dict = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return self.error is None and not self.findings

    def add(self, category: str, label: str, detail: str = "") -> None:
        # A label can embed text taken from the file; the sandbox's validator accepts at most 400 characters, so
        # bound it here and the in-process and sandboxed paths agree for every input.
        if len(label) > 300:
            label = label[:299] + "…"
        finding = Finding(category, label, detail)
        if finding not in self.findings:
            self.findings.append(finding)


class _Malformed(Exception):
    pass


# --------------------------------------------------------------------------
# Format sniffing
# --------------------------------------------------------------------------

_ISOBMFF_AVIF = {b"avif", b"avis"}
_ISOBMFF_HEIF = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1", b"mif2", b"heif"}


def sniff_format(data: bytes) -> str | None:
    """Identify the container format from magic bytes (never from file names)."""
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF"
    if data[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
        return "TIFF"
    if data[:2] == b"BM" and len(data) >= 18:
        return "BMP"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brands = _ftyp_brands(data)
        if brands & _ISOBMFF_AVIF:
            return "AVIF"
        if brands & _ISOBMFF_HEIF:
            return "HEIF"
    return None


def _ftyp_brands(data: bytes) -> set[bytes]:
    try:
        (size,) = struct.unpack_from(">I", data, 0)
        size = min(size, len(data))
        brands = {data[8:12]}
        for off in range(16, size - 3, 4):
            brands.add(data[off : off + 4])
        return brands
    except struct.error:
        return set()


def inspect_bytes(data: bytes) -> Report:
    """Inspect a complete file held in memory and report what it contains."""
    fmt = sniff_format(data)
    report = Report(fmt)
    parser = {
        "JPEG": _inspect_jpeg,
        "PNG": _inspect_png,
        "WEBP": _inspect_webp,
        "GIF": _inspect_gif,
        "TIFF": _inspect_tiff,
        "BMP": _inspect_bmp,
        "AVIF": _inspect_isobmff,
        "HEIF": _inspect_isobmff,
    }.get(fmt)
    if parser is None:
        report.error = "unrecognised container format"
        return report
    try:
        parser(data, report)
    except _Malformed as exc:
        report.error = f"malformed {fmt}: {exc}"
    except (struct.error, IndexError, ValueError, OverflowError) as exc:
        report.error = f"malformed {fmt}: truncated or inconsistent structure ({exc})"
    return report


# Chunk sizes of the IDAT stream that imgstrip's two PNG writers produce: Pillow cuts the stream into 64 KiB chunks
# and ``codecs.encode_png`` into 1 MiB ones.  Every chunk but the last has that size; the last is not empty.
_PNG_IDAT_SIZES = (1 << 16, 1 << 20)


def canonical_form_problems(data: bytes, fmt: str) -> list[str]:
    """Ways in which an *output* departs from the exact layout imgstrip's writers choose.

    ``inspect_bytes`` reports what a file holds that a viewer does not show.  Some structures are legal, invisible and
    different between encoders (the sizes into which a PNG's pixel stream is cut, the identifiers a JPEG gives its
    components, which GIF version it declares).  A cleaned file has no reason to vary in any of them, so a value
    that is not the writer's own is either an encoder defect or a message; the verifier refuses it.  These rules
    are not applied to inputs, where every other encoder's choices are normal.
    """
    try:
        if fmt == "PNG":
            return _png_canonical(data)
        if fmt == "JPEG":
            return _jpeg_canonical(data)
        if fmt == "GIF":
            return _gif_canonical(data)
    except (struct.error, IndexError, ValueError):
        return ["the layout could not be read"]
    return []


def _png_canonical(d: bytes) -> list[str]:
    sizes, pos = [], 8
    while pos + 12 <= len(d):
        length, ctype = struct.unpack_from(">I4s", d, pos)
        if ctype == b"IDAT":
            sizes.append(length)
        pos += 12 + length
    if len(sizes) <= 1:
        return []
    if not any(all(s == step for s in sizes[:-1]) and 0 < sizes[-1] <= step for step in _PNG_IDAT_SIZES):
        return [f"the pixel stream is cut into IDAT chunks of {sorted(set(sizes))[:4]} bytes, not a writer's fixed size"]
    return []


def _jpeg_canonical(d: bytes) -> list[str]:
    ids = None
    pos, n = 2, len(d)
    while pos + 4 <= n:
        if d[pos] != 0xFF:
            pos += 1
            continue
        marker = d[pos + 1]
        if marker in (0xFF, 0x00, 0x01) or 0xD0 <= marker <= 0xD8:
            pos += 1 if marker == 0xFF else 2
            continue
        if marker == 0xD9:
            break
        (length,) = struct.unpack_from(">H", d, pos + 2)
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF) and ids is None:
            count = d[pos + 9]
            ids = [d[pos + 10 + 3 * i] for i in range(count)]
        if marker == 0xDA:
            break
        pos += 2 + length
    # Pillow's CMYK and RGB writers use the Adobe names ('C','M','Y','K' and 'R','G','B'); every other file is 1..n.
    allowed = [list(range(1, len(ids) + 1)), [67, 77, 89, 75], [82, 71, 66]] if ids is not None else []
    if ids is not None and ids not in allowed:
        return [f"the components are numbered {ids}, not 1..{len(ids)}"]
    return []


def _gif_canonical(d: bytes) -> list[str]:
    return [] if d[:6] == b"GIF89a" else [f"the signature is {d[:6]!r}, not GIF89a"]


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

_TRAILING_SIGNATURES = [
    (b"PK\x03\x04", "ZIP archive"),
    (b"PK\x05\x06", "ZIP archive"),
    (b"Rar!\x1a\x07", "RAR archive"),
    (b"7z\xbc\xaf\x27\x1c", "7-Zip archive"),
    (b"%PDF-", "PDF document"),
    (b"\xff\xd8\xff", "JPEG image"),
    (b"\x89PNG\r\n\x1a\n", "PNG image"),
    (b"GIF8", "GIF image"),
    (b"\x1f\x8b\x08", "gzip data"),
    (b"BZh", "bzip2 data"),
    (b"\xfd7zXZ\x00", "xz data"),
    (b"<?xpacket", "XMP packet"),
    (b"<x:xmpmeta", "XMP packet"),
    (b"SEFH", "Samsung SEF trailer"),
    (b"SEFT", "Samsung SEF trailer"),
]


def _describe_blob(blob: bytes, scan_limit: int = 4 << 20) -> str:
    """Best-effort description of an opaque byte blob (trailing data etc.)."""
    if not blob.strip(b"\x00"):
        return "all zero bytes"
    window = blob[:scan_limit]
    found = []
    for sig, name in _TRAILING_SIGNATURES:
        idx = window.find(sig)
        if idx >= 0 and name not in found:
            found.append(name if idx == 0 else f"{name} at +{idx}")
    if b"IDAT" in window or b"IEND" in window:
        found.append("PNG chunk remnants (possible un-truncated earlier version, 'aCropalypse')")
    if b"\xff\xda" in window and b"\xff\xd9" in window and not any("JPEG" in f for f in found):
        found.append("JPEG scan remnants")
    if b"Exif\x00\x00" in window:
        found.append("EXIF block")
    return ", ".join(found) if found else "unidentified data"


def _check_trailing(data: bytes, end: int, report: Report, where: str) -> None:
    extra = len(data) - end
    if extra > 0:
        report.add(HIDDEN, f"Data after {where}", f"{extra} bytes ({_describe_blob(data[end:])})")


def _printable(raw: bytes, limit: int = 60) -> str:
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _rational(value) -> float | None:
    try:
        if isinstance(value, tuple) and len(value) == 2:
            return float(value[0]) / float(value[1]) if value[1] else None
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _gps_decimal(dms, ref) -> float | None:
    try:
        d, m, s = (_rational(x) for x in dms)
        if d is None or m is None or s is None:
            return None
        value = d + m / 60.0 + s / 3600.0
        ref = ref.decode() if isinstance(ref, bytes) else str(ref or "")
        return -value if ref.strip().upper() in ("S", "W") else value
    except (TypeError, ValueError):
        return None


_EXIF_INTERESTING = {
    0x010F: "make",
    0x0110: "model",
    0x0131: "software",
    0x0132: "date",
    0x013B: "artist",
    0x013C: "host",
    0x8298: "copyright",
    0x010E: "description",
}
_EXIF_SUB_INTERESTING = {
    0x9003: "taken",
    0xA431: "body serial",
    0xA430: "owner",
    0xA434: "lens",
    0xA435: "lens serial",
    0xA420: "unique id",
    0x9286: "user comment",
}


def _exif_findings(raw: bytes, report: Report, label: str = "EXIF") -> None:
    """Summarise an EXIF/TIFF blob: camera, timestamps, serials, GPS, thumbnail."""
    from PIL import Image

    report.info.setdefault("exif", raw)
    detail = f"{len(raw)} bytes"
    try:
        exif = Image.Exif()
        exif.load(raw if raw[:4] in (b"II*\x00", b"MM\x00*", b"Exif") else b"Exif\x00\x00" + raw)
        parts = []

        def text(value) -> str:
            if isinstance(value, bytes):
                if value[:8] in (b"ASCII\x00\x00\x00", b"UNICODE\x00", b"JIS\x00\x00\x00\x00\x00", b"\x00" * 8):
                    value = value[8:]  # UserComment character-set prefix
                value = value.decode("utf-8", "replace")
            return str(value).strip("\x00 ")

        for tag, name in _EXIF_INTERESTING.items():
            if tag in exif and text(exif[tag]):
                parts.append(f"{name}={_printable(text(exif[tag]).encode(), 30)}")
        sub = exif.get_ifd(0x8769)
        for tag, name in _EXIF_SUB_INTERESTING.items():
            if tag in sub and text(sub[tag]):
                parts.append(f"{name}={_printable(text(sub[tag]).encode(), 30)}")
        if 0x927C in sub:
            parts.append("maker notes")
        if parts:
            detail += "; " + ", ".join(parts)
        report.add(METADATA, label, detail)
        gps = exif.get_ifd(0x8825)
        if gps:
            lat = _gps_decimal(gps.get(2), gps.get(1)) if 2 in gps else None
            lon = _gps_decimal(gps.get(4), gps.get(3)) if 4 in gps else None
            if lat is not None and lon is not None:
                report.add(METADATA, "GPS location", f"{lat:.6f}, {lon:.6f}")
            else:
                report.add(METADATA, "GPS data", f"{len(gps)} GPS tags")
        ifd1 = exif.get_ifd(-1)
        if ifd1 and (0x0201 in ifd1 or 0x0111 in ifd1):
            report.add(HIDDEN, "Embedded EXIF thumbnail", f"{ifd1.get(0x0202, '?')} bytes")
    except Exception:  # untrusted data, any parser failure is "opaque"
        report.add(METADATA, label, detail + " (unparseable)")


_XMP_KEYS = [
    ("xmp:CreatorTool", "tool"),
    ("dc:creator", "creator"),
    ("photoshop:City", "city"),
    ("Iptc4xmpCore:Location", "location"),
    ("exif:GPSLatitude", "GPS"),
    ("drone-dji:", "DJI drone data"),
    ("xmpMM:History", "edit history"),
    ("xmpMM:DocumentID", "document id"),
    ("GCamera:", "Google camera data"),
    ("GDepth:", "depth map"),
    ("GImage:", "embedded image"),
    ("hdrgm:", "HDR gain map"),
    ("aux:SerialNumber", "serial number"),
    ("c2pa", "C2PA"),
    ("crs:", "Camera Raw settings"),
]


def _xmp_detail(raw: bytes) -> str:
    found = []
    text = raw[: 8 << 20].decode("utf-8", "replace")
    for key, name in _XMP_KEYS:
        if key in text and name not in found:
            found.append(name)
    return f"{len(raw)} bytes" + (f"; contains {', '.join(found)}" if found else "")


def _icc_description(profile: bytes) -> str:
    """Extract the 'desc' text of an ICC profile without a CMS library."""
    try:
        (count,) = struct.unpack_from(">I", profile, 128)
        for i in range(min(count, 256)):
            sig, off, size = struct.unpack_from(">4sII", profile, 132 + 12 * i)
            if sig != b"desc":
                continue
            body = profile[off : off + size]
            if body[:4] == b"desc":
                (n,) = struct.unpack_from(">I", body, 8)
                return _printable(body[12 : 12 + n])
            if body[:4] == b"mluc":
                (recs,) = struct.unpack_from(">I", body, 8)
                if recs:
                    length, offset = struct.unpack_from(">II", body, 20)
                    return _printable(body[offset : offset + length].decode("utf-16-be", "replace").encode())
    except struct.error:
        pass
    return "unnamed"


def _icc_finding(profile: bytes, report: Report) -> None:
    report.info.setdefault("icc", profile)
    report.add(COLOR, "ICC color profile", f"'{_icc_description(profile)}', {len(profile)} bytes")


# --------------------------------------------------------------------------
# JPEG
# --------------------------------------------------------------------------

# DNL/DHP/EXP (0xDC/0xDE/0xDF) are ignored or rejected by decoders and are reported as unknown markers.
_JPEG_STRUCTURAL = {m for m in range(0xC0, 0xD0) if m != 0xC8} | {0xDA, 0xDB, 0xDD}
_JPEG_ARITHMETIC = {0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
_JPEG_PROGRESSIVE = {0xC2, 0xC6, 0xCA, 0xCE}
_JPEG_LOSSLESS = {0xC3, 0xC7, 0xCB, 0xCF}


class _JpegTables:
    """Which quantisation, Huffman and arithmetic-conditioning table definitions a decoder actually uses.

    A table that no component or scan uses, or that is redefined before anything used it, is never read by a
    decoder, so whatever it holds is hidden data.  The rules are a decoder's: a component's quantisation table is
    latched when the first scan that contains the component starts; a Huffman scan reads the DC and AC tables of its
    components (a progressive DC scan only the DC ones, a DC refinement none, an AC scan only the AC one, a lossless
    scan only the DC ones); conditioning tables are read only by arithmetic-coded frames, Huffman tables never.
    """

    _NAMES = {0xDB: "quantisation", 0xC4: "Huffman", 0xCC: "arithmetic conditioning"}

    def __init__(self) -> None:
        self.defs: list[list] = []  # [marker, key, offset, used]
        self.in_force: dict[tuple[int, int], int] = {}  # (marker, key) -> index into defs
        self.sof = 0
        self.comps: list[tuple[int, int]] = []  # (component id, quantisation table slot)
        self.latched: set[int] = set()

    def _define(self, marker: int, key: int, offset: int) -> None:
        self.in_force[(marker, key)] = len(self.defs)
        self.defs.append([marker, key, offset, False])

    def _use(self, marker: int, key: int) -> None:
        index = self.in_force.get((marker, key))
        if index is not None:
            self.defs[index][3] = True

    def define(self, marker: int, p: bytes, offset: int, r: Report) -> None:
        i = 0
        while i < len(p):
            if marker == 0xDB:
                size = 1 + 64 * ((p[i] >> 4) + 1)
                bad = p[i] >> 4 > 1 or p[i] & 15 > 3
                key = p[i] & 15
            elif marker == 0xC4:
                size = 17 + sum(p[i + 1 : i + 17])
                bad = i + 17 > len(p) or p[i] >> 4 > 1 or p[i] & 15 > 3
                key = p[i]
            else:  # DAC: (class << 4 | slot, value) pairs
                size, bad, key = 2, p[i] >> 4 > 1 or p[i] & 15 > 3, p[i]
            if bad or i + size > len(p):
                r.add(STRUCTURE, f"Malformed {self._NAMES[marker]} table", f"at offset {offset}")
                return
            self._define(marker, key, offset)
            i += size

    def frame(self, marker: int, p: bytes) -> None:
        if len(p) >= 6 and len(p) >= 6 + 3 * p[5]:
            self.sof = marker
            self.comps = [(p[6 + 3 * i], p[8 + 3 * i]) for i in range(p[5])]

    def scan(self, p: bytes) -> None:
        if not p or len(p) < 4 + 2 * p[0]:
            return
        ns = p[0]
        ss, ah = p[1 + 2 * ns], p[3 + 2 * ns] >> 4
        for k in range(ns):
            ident, selectors = p[1 + 2 * k], p[2 + 2 * k]
            if ident not in self.latched:
                self.latched.add(ident)
                for comp, slot in self.comps:
                    if comp == ident:
                        self._use(0xDB, slot)
            dc, ac = selectors >> 4, 0x10 | (selectors & 15)
            if self.sof in _JPEG_PROGRESSIVE:
                keys = ([dc] if not ah else []) if ss == 0 else [ac]
            elif self.sof in _JPEG_LOSSLESS:
                keys = [dc]
            else:
                keys = [dc, ac]
            for key in keys:
                self._use(0xCC if self.sof in _JPEG_ARITHMETIC else 0xC4, key)

    def report(self, r: Report) -> None:
        for marker, key, offset, used in self.defs:
            if used:
                continue
            name = self._NAMES[marker]
            if marker == 0xDB:
                which = f"table {key}"
            else:
                which = f"{'AC' if key >> 4 else 'DC'} table {key & 15}"
            r.add(HIDDEN, f"Unused {name} table", f"{which} at offset {offset}: no decoder reads it")


_RST = re.compile(rb"\xff[\xd0-\xd7]")


def _inspect_jpeg(d: bytes, r: Report) -> None:
    n = len(d)
    pos = 2
    icc_chunks: list[tuple[int, bytes]] = []
    scans = 0
    sequential = False
    tables = _JpegTables()
    fill = fill_at = 0  # 0xFF fill bytes before markers (decoders skip them), and where the first one is
    restart_interval = 0  # the last DRI value, and whether any restart marker ever appears in the scans
    restarts_seen = False
    while True:
        if pos >= n:
            raise _Malformed("missing end-of-image marker")
        if d[pos] != 0xFF:
            nxt = d.find(b"\xff", pos)
            if nxt < 0:
                raise _Malformed("garbage after last segment")
            r.add(HIDDEN, "Stray bytes between segments", f"{nxt - pos} bytes at offset {pos}")
            pos = nxt
            continue
        start = pos
        while pos < n and d[pos] == 0xFF:
            pos += 1
        if pos - start > 1:
            fill_at = fill_at if fill else start
            fill += pos - start - 1
        if pos >= n:
            raise _Malformed("truncated marker")
        marker = d[pos]
        pos += 1
        if marker == 0xD9:
            break
        if marker in (0x00, 0xD8):
            raise _Malformed(f"unexpected marker 0xFF{marker:02X}")
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            continue
        (length,) = struct.unpack_from(">H", d, pos)
        if length < 2 or pos + length > n:
            raise _Malformed(f"segment 0xFF{marker:02X} overruns the file")
        payload = d[pos + 2 : pos + length]
        segment_at = pos - 2
        pos += length
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack_from(">HH", payload, 1)
            r.info.setdefault("size", (w, h))
            r.info["components"] = payload[5]
            sequential = marker in (0xC0, 0xC1)
            tables.frame(marker, payload)
        elif marker in (0xC4, 0xCC, 0xDB):
            tables.define(marker, payload, segment_at, r)
        if marker == 0xE2 and payload.startswith(b"ICC_PROFILE\x00") and len(payload) >= 14:
            icc_chunks.append((payload[12], payload[14:]))
            continue
        _classify_jpeg_segment(marker, payload, r)
        if marker == 0xDD and len(payload) == 2:
            restart_interval = int.from_bytes(payload, "big")
        if marker == 0xDA:
            scans += 1
            tables.scan(payload)
            # A sequential scan always covers coefficients 0..63 in one pass; decoders ignore these bytes.
            if sequential and payload[-3:] != b"\x00\x3f\x00":
                r.add(HIDDEN, "Non-standard scan parameters", payload[-3:].hex())
            entropy_start = pos
            pos, inside = _skip_entropy(d, pos)
            if restart_interval and _RST.search(d, entropy_start, pos):
                restarts_seen = True
            if inside:
                fill_at = fill_at if fill else inside[1]
                fill += inside[0]
    if scans == 0:
        raise _Malformed("no image scan")
    tables.report(r)
    if restart_interval and not restarts_seen:
        # A restart interval that is longer than the scan, or that no scan reaches, has no effect on any decoder: 16
        # bits that the picture does not show.
        r.add(HIDDEN, "Restart interval that no restart marker uses", str(restart_interval))
    if fill:
        r.add(HIDDEN, "Fill bytes before markers", f"{fill} extra 0xFF byte(s), the first at offset {fill_at}")
    if icc_chunks:
        icc_chunks.sort(key=lambda c: c[0])
        _icc_finding(b"".join(c[1] for c in icc_chunks), r)
    _check_trailing(d, pos, r, "end of JPEG image")


def _skip_entropy(d: bytes, pos: int) -> tuple[int, tuple[int, int] | None]:
    """(offset of the marker that ends the entropy-coded data, (extra 0xFF bytes inside it, first offset) or None).

    Inside the data a 0xFF is followed by 0x00 (a stuffed data byte) or by a restart marker.  Decoders skip any
    further 0xFF bytes before either (libjpeg reads ``FF FF 00`` as one data byte), so a run of them is fill: room
    for data that no decoder reads.
    """
    n = len(d)
    fill = first = 0
    while True:
        i = d.find(b"\xff", pos)
        if i < 0 or i + 1 >= n:
            raise _Malformed("truncated entropy-coded data")
        j = i + 1
        while j < n and d[j] == 0xFF:
            j += 1
        if j >= n:
            raise _Malformed("truncated entropy-coded data")
        b = d[j]
        if b == 0x00 or 0xD0 <= b <= 0xD7:
            if j - i > 1:
                first = first if fill else i
                fill += j - i - 1
            pos = j + 1
            continue
        return i, ((fill, first) if fill else None)


def _app_ident(payload: bytes) -> str:
    ident = payload[:32].split(b"\x00", 1)[0]
    return _printable(ident, 32) or f"{len(payload)} bytes"


def _classify_jpeg_segment(m: int, p: bytes, r: Report) -> None:
    if m == 0xE0:
        if p.startswith(b"JFIF\x00"):
            if len(p) < 14:
                raise _Malformed("short JFIF header")
            if p[5] != 1 or p[6] > 2:
                r.add(HIDDEN, "Non-standard JFIF version", f"{p[5]}.{p[6]:02}")
            units, xd, yd, tw, th = struct.unpack_from(">BHHBB", p, 7)
            if tw or th:
                r.add(HIDDEN, "JFIF thumbnail", f"{tw}x{th} pixels")
            elif len(p) != 14:
                r.add(HIDDEN, "Extra bytes in JFIF header", f"{len(p) - 14} bytes")
            if units != 0:
                r.add(METADATA, "Print resolution (JFIF)", f"{xd}x{yd} {'dpi' if units == 1 else 'dots/cm'}")
            elif (xd, yd) != (1, 1):
                r.add(METADATA, "Pixel aspect ratio (JFIF)", f"{xd}:{yd}")
        elif p.startswith(b"JFXX\x00"):
            r.add(HIDDEN, "JFIF extension thumbnail", f"{len(p)} bytes")
        else:
            r.add(METADATA, "APP0 segment", _app_ident(p))
    elif m == 0xE1:
        if p.startswith(b"Exif\x00"):
            _exif_findings(p[6:], r)
        elif p.startswith(b"http://ns.adobe.com/xap/1.0/\x00"):
            r.add(METADATA, "XMP", _xmp_detail(p[29:]))
        elif p.startswith(b"http://ns.adobe.com/xmp/extension/\x00"):
            r.add(METADATA, "Extended XMP", f"{len(p)} bytes")
        else:
            r.add(METADATA, "APP1 segment", _app_ident(p))
    elif m == 0xE2:
        if p.startswith(b"MPF\x00"):
            r.add(HIDDEN, "Multi-Picture index (MPF)", "secondary images such as depth/gain maps or previews")
        elif p.startswith(b"FPXR\x00"):
            r.add(METADATA, "FlashPix extension data", f"{len(p)} bytes")
        else:
            r.add(METADATA, "APP2 segment", _app_ident(p))
    elif m == 0xEB:
        r.add(METADATA, "JUMBF / C2PA content credentials", f"{len(p)} bytes")
    elif m == 0xEC and p.startswith(b"Ducky"):
        r.add(METADATA, "Photoshop 'Ducky' segment", f"{len(p)} bytes")
    elif m == 0xED:
        if p.startswith(b"Photoshop 3.0\x00"):
            _photoshop_irb(p[14:], r)
        else:
            r.add(METADATA, "APP13 segment", _app_ident(p))
    elif m == 0xEE:
        if not (p.startswith(b"Adobe") and len(p) == 12):
            r.add(METADATA, "APP14 segment", _app_ident(p))
    elif 0xE3 <= m <= 0xEF:
        r.add(METADATA, f"APP{m - 0xE0} segment", _app_ident(p))
    elif m == 0xFE:
        r.add(METADATA, "Comment", f"'{_printable(p)}'")
    elif m not in _JPEG_STRUCTURAL:
        r.add(STRUCTURE, f"Unknown marker 0xFF{m:02X}", f"{len(p)} bytes")


def _photoshop_irb(data: bytes, r: Report) -> None:
    """Photoshop Image Resource Blocks (IPTC, thumbnails, paths, ...)."""
    names = []
    pos = 0
    try:
        while pos + 12 <= len(data) and data[pos : pos + 4] == b"8BIM":
            (rid,) = struct.unpack_from(">H", data, pos + 4)
            nlen = data[pos + 6]
            pos += 7 + nlen + ((nlen + 1) & 1)
            (size,) = struct.unpack_from(">I", data, pos)
            pos += 4 + size + (size & 1)
            if rid == 0x0404:
                names.append("IPTC")
            elif rid in (0x0409, 0x040C):
                r.add(HIDDEN, "Photoshop thumbnail", f"{size} bytes")
            elif rid == 0x0425:
                pass  # IPTC digest
            elif rid not in names:
                names.append(f"0x{rid:04X}")
    except (struct.error, IndexError):
        names.append("unparseable")
    r.add(METADATA, "Photoshop resources / IPTC", ", ".join(map(str, names[:8])) or f"{len(data)} bytes")


# --------------------------------------------------------------------------
# PNG
# --------------------------------------------------------------------------

_PNG_STRUCTURAL = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"acTL", b"fcTL", b"fdAT"}
_PNG_COLOR = {
    b"sRGB": "sRGB rendering intent",
    b"gAMA": "Gamma",
    b"cHRM": "Chromaticities",
    b"cICP": "Coding-independent code points",
    b"mDCV": "Mastering display color volume",
    b"mDCv": "Mastering display color volume",
    b"cLLI": "Content light level",
    b"cLLi": "Content light level",
    b"sBIT": "Significant bits",
}
_PNG_META = {
    b"tIME": "Modification time",
    b"pHYs": "Physical pixel size / DPI",
    b"bKGD": "Background color",
    b"hIST": "Palette histogram",
    b"sPLT": "Suggested palette",
    b"oFFs": "Image offset",
    b"pCAL": "Pixel calibration",
    b"sCAL": "Physical scale",
    b"sTER": "Stereo layout",
    b"dSIG": "Digital signature",
    b"caBX": "C2PA content credentials",
    b"iDOT": "Apple iDOT chunk",
    b"vpAg": "Virtual page",
    b"orNT": "Orientation",
}
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def png_raw_size(width: int, height: int, depth: int, color_type: int, interlace: int) -> int:
    """Exact size of the decompressed (filtered) PNG pixel stream."""
    bpp_bits = depth * _PNG_CHANNELS[color_type]
    if not interlace:
        return height * (1 + (width * bpp_bits + 7) // 8)
    total = 0
    for x0, y0, dx, dy in _ADAM7:
        pw = (width - x0 + dx - 1) // dx if width > x0 else 0
        ph = (height - y0 + dy - 1) // dy if height > y0 else 0
        if pw and ph:
            total += ph * (1 + (pw * bpp_bits + 7) // 8)
    return total


def _inflate_measure(data, limit: int) -> tuple[int, bool, int]:
    """Return (decompressed size capped just above ``limit``, eof, unused bytes).

    ``data`` is the compressed stream, whole or as a list of consecutive pieces (a PNG's IDAT bodies, which are
    never copied or joined).  Input is fed in small slices and output is capped per call, so memory use is
    bounded (zip bombs) and the work is linear in the input size.
    """
    pieces = data if isinstance(data, list) else [data]
    dobj = zlib.decompressobj()
    total = 0
    remaining = sum(len(p) for p in pieces)
    for buf in _slices(pieces, 1 << 16):
        remaining -= len(buf)
        while buf and not dobj.eof:
            out = dobj.decompress(buf, 1 << 22)
            total += len(out)
            buf = dobj.unconsumed_tail
            if total > limit:
                return total, dobj.eof, len(dobj.unused_data)
        if dobj.eof:
            return total, True, len(dobj.unused_data) + remaining
    return total, dobj.eof, len(dobj.unused_data)


def _slices(pieces: list, step: int):
    """The concatenation of ``pieces`` in consecutive slices of ``step`` bytes (the last one may be shorter).

    Slices that lie inside one piece are views of it; only a slice that straddles pieces is assembled (in a
    bytearray, so a stream cut into many tiny pieces still costs linear time).
    """
    pending = bytearray()
    for piece in pieces:
        view = memoryview(piece)
        pos = 0
        if pending:
            pos = min(len(view), step - len(pending))
            pending += view[:pos]
            if len(pending) < step:
                continue
            yield bytes(pending)
            pending = bytearray()
        while pos + step <= len(view):
            yield view[pos : pos + step]
            pos += step
        pending += view[pos:]
    if pending:
        yield bytes(pending)


def _inflate_limited(data: bytes, limit: int = _MAX_TEXT_INFLATE) -> bytes:
    dobj = zlib.decompressobj()
    out = dobj.decompress(data, limit)
    return out


def _inspect_png(d: bytes, r: Report) -> None:
    pos = 8
    n = len(d)
    view = memoryview(d)
    ihdr = None
    idat: list[memoryview] = []  # views into ``d``: the pixel stream is measured where it lies
    idat_done = False
    actl = False
    fctl_before_idat = False
    frames = 0
    ended = False
    first = True
    while pos < n:
        if pos + 12 > n:
            raise _Malformed("truncated chunk header")
        length, ctype = struct.unpack_from(">I4s", d, pos)
        if length > 0x7FFFFFFF or pos + 12 + length > n:
            raise _Malformed(f"chunk {ctype!r} overruns the file")
        chunk = view[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack_from(">I", d, pos + 8 + length)
        pos += 12 + length
        if zlib.crc32(chunk, zlib.crc32(ctype)) & 0xFFFFFFFF != crc:
            r.add(STRUCTURE, "Chunk with bad checksum", ctype.decode("latin-1"))
        body = chunk if ctype == b"IDAT" else bytes(chunk)
        if first:
            if ctype != b"IHDR" or length != 13:
                raise _Malformed("first chunk is not IHDR")
            first = False
        if ctype == b"IEND":
            ended = True
            break
        if ctype == b"IHDR":
            w, h, depth, color_type, comp, filt, interlace = struct.unpack(">IIBBBBB", body)
            if color_type not in _PNG_CHANNELS or comp or filt or interlace > 1 or w == 0 or h == 0:
                raise _Malformed("invalid IHDR")
            ihdr = (w, h, depth, color_type, interlace)
            r.info["size"] = (w, h)
            r.info["bit_depth"] = depth
            r.info["color_type"] = color_type
        elif ctype == b"IDAT":
            if idat_done:
                r.add(STRUCTURE, "Non-contiguous IDAT chunks")
            if length == 0:  # a chunk boundary carries no pixels; an empty one is only a place to put a choice
                r.add(HIDDEN, "Empty IDAT chunk")
            idat.append(body)
        elif ctype in _PNG_STRUCTURAL:
            if ctype == b"PLTE" and ihdr is not None and ihdr[3] != 3:
                # In a greyscale image the chunk is forbidden; in a truecolour one it only suggests a palette to
                # viewers with a limited display, and none renders it: up to 768 bytes nobody sees.
                r.add(HIDDEN, "Suggested palette in an image that does not use one", f"{length} bytes")
            elif ctype == b"tRNS" and ihdr is not None:
                _png_trns_findings(body, ihdr[2], ihdr[3], r)
            if ctype == b"acTL":
                actl = True
                (frames,) = struct.unpack_from(">I", body, 0)
                r.info["frames"] = frames
            elif ctype == b"fcTL" and not idat:
                fctl_before_idat = True
        elif ctype in _PNG_COLOR:
            r.add(COLOR, _PNG_COLOR[ctype])
        elif ctype == b"iCCP":
            name, _, rest = body.partition(b"\x00")
            try:
                _icc_finding(_inflate_limited(rest[1:]), r)
            except zlib.error:
                r.add(COLOR, "ICC color profile", f"'{_printable(name)}' (corrupt)")
        elif ctype == b"eXIf":
            _exif_findings(body, r)
        elif ctype in (b"tEXt", b"zTXt", b"iTXt"):
            _png_text(ctype, body, r)
        elif ctype in _PNG_META:
            detail = ""
            if ctype == b"tIME" and len(body) == 7:
                y, mo, dd, hh, mi, ss = struct.unpack(">HBBBBB", body)
                detail = f"{y:04}-{mo:02}-{dd:02} {hh:02}:{mi:02}:{ss:02}"
            elif ctype == b"pHYs" and len(body) == 9:
                x, y, unit = struct.unpack(">IIB", body)
                detail = f"{x * 0.0254:.0f}x{y * 0.0254:.0f} dpi" if unit == 1 else f"aspect {x}:{y}"
            r.add(METADATA, _PNG_META[ctype], detail)
        else:
            name = ctype.decode("latin-1")
            r.add(HIDDEN if ctype[:1].islower() else STRUCTURE, f"Unknown chunk '{name}'", f"{length} bytes")
        if idat and ctype != b"IDAT":
            idat_done = True
    if not ended:
        raise _Malformed("missing IEND chunk")
    if ihdr is None or not idat:
        raise _Malformed("no image data")
    if actl and not fctl_before_idat:
        r.add(HIDDEN, "Default image not part of the animation", "shown only by non-APNG viewers")
    expected = png_raw_size(ihdr[0], ihdr[1], ihdr[2], ihdr[3], ihdr[4])
    try:
        total, eof, unused = _inflate_measure(idat, expected)
    except zlib.error as exc:
        raise _Malformed(f"corrupt compressed pixel data ({exc})") from None
    if total > expected:
        r.add(HIDDEN, "Extra data inside the compressed pixel stream", "more pixel bytes than the dimensions need")
    elif total < expected:
        raise _Malformed("compressed pixel data is shorter than the image needs")
    elif not eof:
        r.add(STRUCTURE, "Unterminated compressed pixel stream")
    if unused:
        r.add(HIDDEN, "Data after the compressed pixel stream", f"{unused} bytes")
    _check_trailing(d, pos, r, "IEND")


def _png_trns_findings(body: bytes, depth: int, color_type: int, r: Report) -> None:
    """Bytes of a ``tRNS`` chunk that no decoder can show: bits above the sample depth in a colour key, and an
    alpha table that makes every entry opaque (it says what the absence of the chunk says).  Whether a colour key
    matches a pixel needs the pixels; the independent reader checks that (``oracles.reference_png``)."""
    if color_type in (0, 2):
        want = 2 if color_type == 0 else 6
        if len(body) == want and any(v >> depth for v in struct.unpack(f">{want // 2}H", body)):
            r.add(HIDDEN, "Colour key with bits above the sample depth", f"{want} bytes")
    elif color_type == 3 and body and all(b == 255 for b in body):
        r.add(HIDDEN, "Transparency table in which every entry is opaque", f"{len(body)} bytes")


def _png_text(ctype: bytes, body: bytes, r: Report) -> None:
    keyword, _, rest = body.partition(b"\x00")
    key = keyword.decode("latin-1", "replace")
    text = b""
    try:
        if ctype == b"tEXt":
            text = rest
        elif ctype == b"zTXt":
            text = _inflate_limited(rest[1:])
        else:
            flag, _method = rest[0], rest[1]
            rest = rest[2:]
            _lang, _, rest = rest.partition(b"\x00")
            _tkey, _, rest = rest.partition(b"\x00")
            text = _inflate_limited(rest) if flag else rest
    except (zlib.error, IndexError):
        r.add(METADATA, f"Text chunk '{_printable(key.encode(), 30)}'", "corrupt")
        return
    if key == "XML:com.adobe.xmp":
        r.add(METADATA, "XMP", _xmp_detail(text))
    elif key.lower().startswith("raw profile type"):
        kind = key[16:].strip().lower()
        blob = _decode_raw_profile(text)
        if kind in ("exif", "app1") and blob is not None:
            _exif_findings(blob[6:] if blob.startswith(b"Exif") else blob, r)
        elif kind in ("icc", "icm") and blob is not None:
            _icc_finding(blob, r)
        else:
            r.add(METADATA, f"Embedded {_printable(kind.upper().encode(), 30)} profile", f"{len(text)} bytes")
    else:
        r.add(METADATA, f"Text '{_printable(key.encode(), 30)}'", f"'{_printable(text)}'")


def _decode_raw_profile(text: bytes) -> bytes | None:
    """ImageMagick 'Raw profile type xxx' text: name, length, hex dump."""
    try:
        parts = text.split(None, 2)
        return bytes.fromhex(parts[2].decode("ascii").replace("\n", "").replace(" ", ""))
    except (IndexError, ValueError, UnicodeDecodeError):
        return None


# --------------------------------------------------------------------------
# WebP
# --------------------------------------------------------------------------

_VP8X_ICC, _VP8X_ALPHA, _VP8X_EXIF, _VP8X_XMP, _VP8X_ANIM = 0x20, 0x10, 0x08, 0x04, 0x02


def _inspect_webp(d: bytes, r: Report) -> None:
    (riff_size,) = struct.unpack_from("<I", d, 4)
    end = 8 + riff_size
    if end > len(d):
        raise _Malformed("RIFF size exceeds file size")
    if riff_size & 1:
        r.add(STRUCTURE, "Odd RIFF size")
    pos = 12
    image_chunks = 0
    while pos < end:
        if pos + 8 > end:
            raise _Malformed("truncated chunk header")
        fourcc, size = struct.unpack_from("<4sI", d, pos)
        body_end = pos + 8 + size
        if body_end > end:
            raise _Malformed(f"chunk {fourcc!r} overruns the RIFF container")
        body = d[pos + 8 : body_end]
        if size & 1:
            if body_end < end and d[body_end] != 0:
                r.add(HIDDEN, "Non-zero chunk padding byte")
            body_end += 1
        pos = body_end
        if fourcc == b"VP8X":
            flags = body[0]
            if flags & 0xC1 or body[1:4] != b"\x00\x00\x00":
                r.add(HIDDEN, "Reserved VP8X bits set")
            w = 1 + int.from_bytes(body[4:7], "little")
            h = 1 + int.from_bytes(body[7:10], "little")
            r.info["size"] = (w, h)
            r.info["animated"] = bool(flags & _VP8X_ANIM)
        elif fourcc in (b"VP8 ", b"VP8L"):
            image_chunks += 1
            r.info["lossless"] = fourcc == b"VP8L"
        elif fourcc in (b"ALPH", b"ANIM"):
            pass
        elif fourcc == b"ANMF":
            image_chunks += 1
            _webp_frame(body, r)
        elif fourcc == b"ICCP":
            _icc_finding(body, r)
        elif fourcc == b"EXIF":
            _exif_findings(body[6:] if body.startswith(b"Exif\x00\x00") else body, r)
        elif fourcc == b"XMP ":
            r.add(METADATA, "XMP", _xmp_detail(body))
        elif fourcc == b"C2PA":
            r.add(METADATA, C2PA_LABEL, f"{size} bytes")
        else:
            r.add(HIDDEN, f"Unknown chunk '{fourcc.decode('latin-1')}'", f"{size} bytes")
    if image_chunks == 0:
        raise _Malformed("no image data")
    _check_trailing(d, end, r, "RIFF container")


def _webp_frame(body: bytes, r: Report) -> None:
    pos = 16
    while pos < len(body):
        if pos + 8 > len(body):
            raise _Malformed("truncated animation frame")
        fourcc, size = struct.unpack_from("<4sI", body, pos)
        if fourcc in (b"VP8 ", b"VP8L"):
            r.info["lossless"] = r.info.get("lossless", True) and fourcc == b"VP8L"
        elif fourcc != b"ALPH":
            r.add(HIDDEN, f"Unknown chunk '{fourcc.decode('latin-1')}' inside animation frame", f"{size} bytes")
        pos += 8 + size + (size & 1)


# --------------------------------------------------------------------------
# GIF
# --------------------------------------------------------------------------


def _gif_subblocks(d: bytes, pos: int) -> tuple[bytes, int]:
    out = bytearray()
    while True:
        size = d[pos]
        pos += 1
        if size == 0:
            return bytes(out), pos
        if pos + size > len(d):
            raise _Malformed("truncated data sub-block")
        out += d[pos : pos + size]
        pos += size


def _inspect_gif(d: bytes, r: Report) -> None:
    w, h, packed, bg, aspect = struct.unpack_from("<HHBBB", d, 6)
    r.info["size"] = (w, h)
    outside = 0
    if bg and not packed & 0x80:  # the background index points into a global table that is not there
        r.add(HIDDEN, "Non-zero GIF background index without a global colour table", str(bg))
    if aspect:
        r.add(METADATA, "Pixel aspect ratio", str(aspect))
    pos = 13
    global_table = b""
    if packed & 0x80:
        size = 3 << ((packed & 7) + 1)
        global_table = d[pos : pos + size]
        pos += size
    frames = 0
    delay = 0  # the longest frame delay, and whether a loop count was given: both only matter with a second frame
    loop_seen = False
    transparent = None
    # Frames share the global colour table, so it is judged once, against every frame that draws with it.
    global_used = global_shown = 0
    global_known = True  # False once a frame drawing with it cannot be read
    while True:
        if pos >= len(d):
            raise _Malformed("missing trailer")
        block = d[pos]
        if block == 0x3B:
            pos += 1
            break
        if block == 0x2C:
            frames += 1
            left, top, fw, fh, ipacked = struct.unpack_from("<HHHHB", d, pos + 1)
            if left + fw > w or top + fh > h:
                outside += 1
            if ipacked & 0x18:  # two bits the format reserves
                r.add(HIDDEN, "Non-zero reserved bits in a GIF frame header", f"{ipacked & 0x18:#04x}")
            pos += 10
            local = None
            if ipacked & 0x80:
                size = 3 << ((ipacked & 7) + 1)
                local = d[pos : pos + size]
                pos += size
            min_code = d[pos]
            pos += 1  # LZW minimum code size
            data, pos = _gif_subblocks(d, pos)
            used = _gif_used_indices(min_code, data) if 1 <= min_code <= 8 else None
            if used is None:
                global_known = global_known and local is not None
                if 1 <= min_code <= 8:
                    r.add(STRUCTURE, "Corrupt GIF image data")
            else:
                shown = used & ~(1 << transparent) if transparent is not None else used
                if local is not None:
                    _gif_palette(local, used, shown, r)
                else:
                    global_used, global_shown = global_used | used, global_shown | shown
            transparent = None
        elif block == 0x21:
            label = d[pos + 1]
            pos += 2
            data, end = _gif_subblocks(d, pos)
            if label == 0xF9:
                if d[pos] != 4:
                    r.add(STRUCTURE, "Malformed graphic control extension")
                else:
                    packed_gce = d[pos + 1]
                    if packed_gce & 0xE0:  # three bits the format reserves
                        r.add(HIDDEN, "Non-zero reserved bits in a GIF graphic control extension")
                    delay = max(delay, int.from_bytes(d[pos + 2 : pos + 4], "little"))
                    if packed_gce & 1:
                        transparent = d[pos + 4]
                    elif d[pos + 4]:  # the index only means something when the transparency flag is on
                        r.add(HIDDEN, "Transparent colour index set without transparency", str(d[pos + 4]))
            elif label == 0xFE:
                r.add(METADATA, "Comment", f"'{_printable(data)}'")
            elif label == 0x01:
                r.add(METADATA, "Plain-text extension", f"'{_printable(data[12:])}'")
            elif label == 0xFF:
                if d[pos + 1 : pos + 12] in (b"NETSCAPE2.0", b"ANIMEXTS1.0"):
                    loop_seen = True
                _gif_app_extension(d, pos, data, r)
            else:
                r.add(STRUCTURE, f"Unknown extension 0x{label:02X}", f"{len(data)} bytes")
            pos = end
        else:
            raise _Malformed(f"unexpected block 0x{block:02X}")
    r.info["frames"] = frames
    if frames == 0:
        raise _Malformed("no image data")
    if frames == 1:
        if delay:
            r.add(HIDDEN, "Frame delay on a GIF with a single frame", f"{delay} centiseconds")
        if loop_seen:
            r.add(HIDDEN, "Loop count on a GIF with a single frame")
    if global_table and global_known:
        _gif_palette(global_table, global_used, global_shown, r)
    if outside:
        r.add(HIDDEN, "Frame area outside the canvas", f"{outside} frame(s) extend past {w}x{h}; viewers clip them")
    _check_trailing(d, pos, r, "GIF trailer")


def _gif_used_indices(min_code: int, data: bytes) -> int | None:
    """The palette indices a frame uses, as a bit mask, by walking its LZW codes (None if they are corrupt).

    Every index a frame draws is first drawn by a literal code: a table entry only ever expands to indices that
    codes since the last clear code already drew.  So the literal codes alone name the indices used, and neither
    the pixels nor the table entries are built; only the table's size is followed, for the code width and to
    reject codes that name entries that do not exist yet.
    """
    clear, eoi = 1 << min_code, (1 << min_code) + 1
    literal = bytearray(clear)
    width, size, acc, bits, first = min_code + 1, clear + 2, 0, 0, True
    mask, limit = (1 << width) - 1, 1 << width
    for byte in data:
        acc |= byte << bits
        bits += 8
        while bits >= width:
            code = acc & mask
            acc >>= width
            bits -= width
            if code < clear:
                literal[code] = 1
            elif code == clear:
                width, size, first = min_code + 1, clear + 2, True
                mask, limit = (1 << width) - 1, 1 << width
                continue
            elif code == eoi:
                return _index_mask(literal)
            elif first or code > size:  # code == size is the entry this very code creates (KwKwK)
                return None
            if first:
                first = False
                continue
            if size < 4096:
                size += 1
            if size == limit and width < 12:
                width += 1
                mask, limit = (1 << width) - 1, 1 << width
    return _index_mask(literal)


def _index_mask(flags: bytearray) -> int:
    return sum(1 << i for i, flag in enumerate(flags) if flag)


def _gif_palette(table: bytes, used: int, shown: int, r: Report) -> None:
    """Colour-table entries nobody sees: ``used`` and ``shown`` are bit masks of the indices the pixels use,
    and of those drawn opaque."""
    entries = len(table) // 3
    coloured = [i for i in range(entries) if any(table[3 * i : 3 * i + 3])]
    unused = sum(1 for i in coloured if not used >> i & 1)
    if unused:
        r.add(HIDDEN, "Unused palette entries with colour data", f"{unused} of {entries} entries")
    if any(used >> i & 1 and not shown >> i & 1 for i in coloured):
        r.add(HIDDEN, "Colour data under a transparent palette entry")


def _gif_app_extension(d: bytes, pos: int, data: bytes, r: Report) -> None:
    if d[pos] != 11:
        r.add(STRUCTURE, "Malformed application extension")
        return
    app = d[pos + 1 : pos + 12]
    rest = data[11:]
    if app in (b"NETSCAPE2.0", b"ANIMEXTS1.0"):
        # Loop count is the only rendering-relevant application data.
        if not (len(rest) == 3 and rest[0] == 1):
            r.add(HIDDEN, "Non-standard NETSCAPE extension", f"{len(rest)} bytes")
    elif app == b"C2PA_GIF\x01\x00\x00":
        r.add(METADATA, C2PA_LABEL, f"{len(rest)} bytes")
    elif app == b"XMP DataXMP":
        r.add(METADATA, "XMP", _xmp_detail(rest))
    elif app == b"ICCRGBG1012":
        _icc_finding(rest, r)
    elif app.startswith(b"MGK8BIM"):
        r.add(METADATA, "Photoshop resources", f"{len(rest)} bytes")
    elif app.startswith(b"MGKIPTC"):
        r.add(METADATA, "IPTC", f"{len(rest)} bytes")
    else:
        r.add(METADATA, f"Application extension '{_printable(app)}'", f"{len(rest)} bytes")


# --------------------------------------------------------------------------
# TIFF (classic and BigTIFF)
# --------------------------------------------------------------------------

_TIFF_TYPE_SIZE = {
    1: 1,
    2: 1,
    3: 2,
    4: 4,
    5: 8,
    6: 1,
    7: 1,
    8: 2,
    9: 4,
    10: 8,
    11: 4,
    12: 8,
    13: 4,
    16: 8,
    17: 8,
    18: 8,
}
_TIFF_STRUCTURAL = {
    254,
    255,
    256,
    257,
    258,
    259,
    262,
    263,
    264,
    265,
    266,
    273,
    277,
    278,
    279,
    280,
    281,
    284,
    290,
    291,
    292,
    293,
    317,
    320,
    322,
    323,
    324,
    325,
    338,
    339,
    340,
    341,
    347,
    529,
    530,
    531,
    532,
}
_TIFF_COLOR = {301: "Transfer function", 318: "White point", 319: "Primary chromaticities"}
_TIFF_NAMES = {
    269: "Document name",
    270: "Image description",
    271: "Camera make",
    272: "Camera model",
    274: "Orientation",
    282: "X resolution",
    283: "Y resolution",
    285: "Page name",
    286: "X position",
    287: "Y position",
    296: "Resolution unit",
    297: "Page number",
    305: "Software",
    306: "Date/time",
    315: "Artist",
    316: "Host computer",
    33432: "Copyright",
    700: "XMP",
    33723: "IPTC",
    34377: "Photoshop resources",
    37724: "Photoshop layer data",
    50341: "PrintIM",
    18246: "Rating",
    18249: "Rating percent",
    40091: "Windows title",
    40092: "Windows comment",
    40093: "Windows author",
    40094: "Windows keywords",
    40095: "Windows subject",
    37500: "Maker notes",
    50708: "Unique camera model (DNG)",
    50735: "Camera serial number (DNG)",
    52545: C2PA_LABEL,
}
_TIFF_SUBIFD_TAGS = {34665: "EXIF", 34853: "GPS", 40965: "Interoperability", 330: "Sub-images (SubIFDs)"}
_TIFF_TEXT_TAGS = {269, 270, 271, 272, 285, 305, 306, 315, 316, 33432}


class _Tiff:
    def __init__(self, d: bytes):
        self.d = d
        self.e = "<" if d[:2] == b"II" else ">"
        (self.version,) = struct.unpack_from(self.e + "H", d, 2)
        self.big = self.version == 43
        if self.version not in (42, 43):
            raise _Malformed("bad TIFF version")
        self.ranges: list[tuple[int, int]] = []
        self.padding = 0  # non-zero bytes in the unused tail of inline tag values

    def cover(self, start: int, length: int) -> None:
        if start < 0 or length < 0 or start + length > len(self.d):
            raise _Malformed("offset points outside the file")
        if length:
            self.ranges.append((start, start + length))

    def read_ifd(self, off: int) -> tuple[list[tuple[int, int, int, int, bytes]], int]:
        e, d = self.e, self.d
        if self.big:
            (count,) = struct.unpack_from(e + "Q", d, off)
            esize, hsize, nsize = 20, 8, 8
        else:
            (count,) = struct.unpack_from(e + "H", d, off)
            esize, hsize, nsize = 12, 2, 4
        if count > 4096:
            raise _Malformed("implausible IFD entry count")
        self.cover(off, hsize + esize * count + nsize)
        entries = []
        for i in range(count):
            base = off + hsize + esize * i
            if self.big:
                tag, typ, cnt = struct.unpack_from(e + "HHQ", d, base)
                inline, voff = 8, base + 12
            else:
                tag, typ, cnt = struct.unpack_from(e + "HHI", d, base)
                inline, voff = 4, base + 8
            size = _TIFF_TYPE_SIZE.get(typ, 1) * cnt
            if size > inline:
                (ptr,) = struct.unpack_from(e + ("Q" if self.big else "I"), d, voff)
                self.cover(ptr, size)
                raw = d[ptr : ptr + size]
            else:
                raw = d[voff : voff + size]
                # A value shorter than the field leaves its tail unused (a SHORT fills two of four bytes).
                self.padding += sum(1 for b in d[voff + size : voff + inline] if b)
            entries.append((tag, typ, cnt, size, raw))
        (nxt,) = struct.unpack_from(e + ("Q" if self.big else "I"), d, off + hsize + esize * count)
        return entries, nxt

    def values(self, typ: int, cnt: int, raw: bytes) -> list[int]:
        fmt = {1: "B", 3: "H", 4: "I", 16: "Q", 13: "I", 18: "Q", 6: "b", 8: "h", 9: "i", 17: "q"}.get(typ)
        if fmt is None:
            return []
        return list(struct.unpack_from(self.e + fmt * cnt, raw, 0))


def _inspect_tiff(d: bytes, r: Report) -> None:
    t = _Tiff(d)
    if t.big:
        bytesize, _zero, first = struct.unpack_from(t.e + "HHQ", d, 4)
        if bytesize != 8:
            raise _Malformed("bad BigTIFF header")
        t.cover(0, 16)
        r.info["bigtiff"] = True
    else:
        (first,) = struct.unpack_from(t.e + "I", d, 4)
        t.cover(0, 8)
    seen: set[int] = set()
    page = 0
    off = first
    while off:
        if off in seen:
            raise _Malformed("IFD loop")
        seen.add(off)
        page += 1
        entries, off = t.read_ifd(off)
        _tiff_ifd(t, entries, r, page, seen)
        if page > 10000:
            raise _Malformed("too many pages")
    if page == 0:
        raise _Malformed("no image directory")
    r.info["pages"] = page
    if t.padding:
        r.add(HIDDEN, "Non-zero padding inside TIFF tag fields", f"{t.padding} bytes")
    _tiff_gaps(t, r)


def _tiff_ifd(t: _Tiff, entries, r: Report, page: int, seen: set[int], sub: str | None = None) -> None:
    tags = {tag: (typ, cnt, raw) for tag, typ, cnt, _size, raw in entries}
    where = f" (page {page})" if page > 1 else ""
    if sub is None:
        if 256 in tags and 257 in tags:
            r.info.setdefault("size", (t.values(*tags[256])[0], t.values(*tags[257])[0]))
        nst = t.values(*tags[254])[0] if 254 in tags else 0
        if nst & 1:
            r.add(HIDDEN, "Reduced-resolution image (thumbnail/preview)" + where)
        if nst & 4:
            r.add(HIDDEN, "Transparency-mask subfile" + where)
    # Every byte of strip / tile data must be accounted for.
    for off_tag, cnt_tag in ((273, 279), (324, 325), (513, 514)):
        if off_tag in tags and cnt_tag in tags:
            offsets, counts = t.values(*tags[off_tag]), t.values(*tags[cnt_tag])
            if len(offsets) != len(counts):
                r.add(STRUCTURE, "Strip/tile offset and byte-count tables disagree")
            for o, n in zip(offsets, counts, strict=False):  # mismatch reported above
                t.cover(o, n)
                if 259 in tags and t.values(*tags[259])[0] in (8, 32946):
                    _tiff_deflate_strip(t.d[o : o + n], r)
    for tag, (typ, cnt, raw) in tags.items():
        if tag in _TIFF_SUBIFD_TAGS:
            for ptr in t.values(typ, cnt, raw) if typ in (4, 13, 16, 18) else []:
                if ptr == 0 or ptr in seen:
                    continue
                seen.add(ptr)
                sub_entries, _nxt = t.read_ifd(ptr)
                _tiff_ifd(t, sub_entries, r, page, seen, sub=_TIFF_SUBIFD_TAGS[tag])
                if tag == 34853:
                    _tiff_gps(t, sub_entries, r)
                elif tag == 34665 and sub is None:
                    r.add(METADATA, "EXIF" + where, f"{len(sub_entries)} tags")
            if sub is None and tag == 330:
                r.add(HIDDEN, "Sub-images (SubIFDs)" + where, "thumbnails, previews or raw data")
            elif sub is None and tag == 40965:
                r.add(METADATA, "EXIF interoperability" + where)
            continue
        if sub is not None:
            continue  # contents of EXIF/GPS/sub-image IFDs are summarised by the parent finding
        if tag == 320 and typ == 3:
            # Decoders that work in 8 bits keep only the high byte of each entry; an entry that is not v * 257
            # carries a low byte nobody sees.
            values = t.values(typ, cnt, raw)
            odd = sum(1 for v in values if (v & 0xFF) != (v >> 8))
            if odd:
                r.add(HIDDEN, "TIFF colour map holds more than 8 bits per entry", f"{odd} of {len(values)} values")
        if tag in _TIFF_STRUCTURAL or tag in (513, 514):
            continue
        if tag == 34675:
            _icc_finding(raw, r)
        elif tag in _TIFF_COLOR:
            r.add(COLOR, _TIFF_COLOR[tag] + where)
        elif tag == 700:
            r.add(METADATA, "XMP" + where, _xmp_detail(raw))
        elif tag in _TIFF_NAMES:
            detail = f"'{_printable(raw)}'" if tag in _TIFF_TEXT_TAGS else f"{len(raw)} bytes"
            r.add(METADATA, _TIFF_NAMES[tag] + where, detail)
        else:
            r.add(METADATA, f"TIFF tag {tag}" + where, f"{len(raw)} bytes")


def _tiff_deflate_strip(strip: bytes, r: Report) -> None:
    """Decoders inflate a Deflate strip without checking it ends where the data does.

    The strip is inflated in pieces and thrown away, so a whole page in one strip costs no memory; imgstrip's own
    writer puts a page in a single strip.  Past 2 GiB of output the walk gives up: the limit on image size
    applies before anything is decoded, and this only guards against a bomb.
    """
    inflater = zlib.decompressobj()
    data, produced = strip, 0
    try:
        while data and not inflater.eof and produced < (2 << 30):
            produced += len(inflater.decompress(data, 1 << 20))
            data = inflater.unconsumed_tail
    except zlib.error:
        r.add(STRUCTURE, "Corrupt compressed TIFF strip")
        return
    if produced >= (2 << 30):
        return
    if not inflater.eof:
        r.add(STRUCTURE, "Unterminated compressed TIFF strip")
    elif inflater.unused_data:
        r.add(HIDDEN, "Data after a compressed TIFF strip", f"{len(inflater.unused_data)} bytes")


def _tiff_gps(t: _Tiff, entries, r: Report) -> None:
    tags = {tag: (typ, cnt, raw) for tag, typ, cnt, _s, raw in entries}

    def rationals(tag):
        typ, cnt, raw = tags[tag]
        vals = struct.unpack_from(t.e + "I" * (2 * cnt), raw, 0)
        return [(vals[i], vals[i + 1]) for i in range(0, len(vals), 2)]

    try:
        lat = _gps_decimal(rationals(2), tags[1][2][:1])
        lon = _gps_decimal(rationals(4), tags[3][2][:1])
        r.add(METADATA, "GPS location", f"{lat:.6f}, {lon:.6f}")
    except (KeyError, struct.error, TypeError, ValueError, IndexError):
        r.add(METADATA, "GPS data", f"{len(entries)} GPS tags")


def _tiff_gaps(t: _Tiff, r: Report) -> None:
    ranges = sorted(t.ranges)
    pos = 0
    hidden = 0
    for start, end in ranges:
        if start > pos:
            gap = t.d[pos:start]
            # A single zero byte is legitimate word-alignment padding.
            if not (len(gap) == 1 and gap == b"\x00"):
                hidden += len(gap)
        pos = max(pos, end)
    tail = len(t.d) - pos
    if hidden:
        r.add(HIDDEN, "Unreferenced bytes inside the file", f"{hidden} bytes not used by the image or any tag")
    if tail > 0 and not (tail == 1 and t.d[-1:] == b"\x00"):
        r.add(HIDDEN, "Data after the last referenced structure", f"{tail} bytes ({_describe_blob(t.d[pos:])})")


# --------------------------------------------------------------------------
# BMP
# --------------------------------------------------------------------------


def _inspect_bmp(d: bytes, r: Report) -> None:
    file_size, res1, res2, pix_off, dib = struct.unpack_from("<IHHII", d, 2)
    if res1 or res2:
        r.add(HIDDEN, "Non-zero reserved header fields", f"{res1:#06x} {res2:#06x}")
    if dib == 12:
        w, h, planes, bpp = struct.unpack_from("<HHHH", d, 18)
        comp, img_size, colors = 0, 0, 0
        pal_entry = 3
    elif dib in (40, 52, 56, 64, 108, 124):
        w, h, planes, bpp, comp, img_size, xppm, yppm, colors, _imp = struct.unpack_from("<iiHHIIiiII", d, 18)
        pal_entry = 4
    else:
        raise _Malformed(f"unsupported DIB header size {dib}")
    r.info["size"] = (w, abs(h))
    if w <= 0 or h == 0 or planes != 1:
        raise _Malformed("invalid dimensions")
    header_end = 14 + dib
    if dib == 40 and comp in (3, 6):
        header_end += 12 if comp == 3 else 16
    used = [(0, header_end)]
    if dib in (108, 124):
        cs_type = d[14 + 56 : 14 + 60]
        _bmp_colour_space(d, dib, cs_type, r)
    if dib == 124:
        # The V5 fields follow the endpoints and gamma: intent at +108, profile offset (from the start of the
        # header) at +112, its size at +116 and a reserved word at +120.
        _intent, prof_off, prof_size, reserved = struct.unpack_from("<IIII", d, 14 + 108)
        if cs_type == b"DEBM":  # 'MBED' stored little-endian
            start = 14 + prof_off
            if start + prof_size > len(d):
                raise _Malformed("embedded profile overruns the file")
            _icc_finding(d[start : start + prof_size], r)
            used.append((start, start + prof_size))
        elif cs_type == b"KNIL":  # 'LINK'
            start = 14 + prof_off
            r.add(METADATA, "Linked color profile path", f"'{_printable(d[start : start + prof_size])}'")
            used.append((start, start + prof_size))
        elif prof_off or prof_size:
            r.add(HIDDEN, "Profile fields set without a profile", f"offset {prof_off}, size {prof_size}")
        if reserved:
            r.add(HIDDEN, "Non-zero reserved V5 header field")
    n_colors = (colors or (1 << bpp)) if 0 < bpp <= 8 else colors  # bpp 0 is a JPEG or PNG stored inside the BMP
    table_end = header_end + pal_entry * n_colors
    if n_colors:
        used.append((header_end, table_end))
    if table_end > pix_off:
        raise _Malformed("the headers or colour table run into the pixel data")
    if comp in (0, 3, 6):
        stride = ((w * bpp + 31) // 32) * 4
        pix_size = stride * abs(h)
    else:
        stride = 0
        pix_size = img_size
    if pix_off + pix_size > len(d):
        raise _Malformed("pixel data overruns the file")
    used.append((pix_off, pix_off + pix_size))
    if dib >= 40:
        _bmp_header_fields(r, bpp, comp, img_size, pix_size, xppm, yppm, colors, _imp)
    if 0 < bpp <= 8 and pal_entry == 4:
        _bmp_palette(d, header_end, n_colors, r)
        if comp == 0:
            _bmp_indices(d, pix_off, w, abs(h), bpp, stride, n_colors, r)
    if comp in (0, 3, 6) and bpp:
        _bmp_row_padding(d, pix_off, w, abs(h), bpp, stride, r)
    used.sort()
    pos = 0
    gap = 0
    for start, end in used:
        if start > pos:
            gap += start - pos
        pos = max(pos, end)
    if gap:
        r.add(HIDDEN, "Unreferenced bytes inside the file", f"{gap} bytes")
    if file_size != len(d):
        r.add(STRUCTURE, "Header file size does not match the actual size", f"{file_size} vs {len(d)}")
    _check_trailing(d, pos, r, "pixel data")


def unused_palette_entries(im) -> int:
    """How many palette entries no pixel uses yet hold colour (they would carry data a viewer never shows)."""
    import numpy as np

    pal = im.getpalette("RGB") or []
    n = len(pal) // 3
    used = set(np.flatnonzero(np.bincount(np.asarray(im).ravel(), minlength=256)).tolist())
    return sum(1 for i in range(n) if i not in used and any(pal[3 * i : 3 * i + 3]))


def transparent_palette_colour(im) -> int:
    """How many fully transparent palette entries hold colour (a viewer never shows it)."""
    transparency = im.info.get("transparency")
    pal = im.getpalette("RGB") or []
    if isinstance(transparency, int):
        clear = [transparency]
    elif isinstance(transparency, (bytes, bytearray)):
        clear = [i for i, a in enumerate(transparency) if a == 0]
    else:
        return 0
    return sum(1 for i in clear if any(pal[3 * i : 3 * i + 3]))


def _bmp_header_fields(
    r: Report, bpp: int, comp: int, img_size: int, pix_size: int, xppm: int, yppm: int, colors: int, important: int
) -> None:
    """DIB header fields that no decoder needs, and that carry data or metadata when they are not at rest."""
    if (xppm, yppm) not in ((0, 0), (2835, 2835), (3780, 3780)):  # none, or the 72 / 96 dpi encoders write by default
        r.add(METADATA, "Print resolution (BMP)", f"{xppm * 0.0254:.0f}x{yppm * 0.0254:.0f} dpi")
    if comp in (0, 3, 6) and img_size not in (0, pix_size):
        r.add(HIDDEN, "BMP image size field disagrees with the pixel data", f"{img_size} vs {pix_size}")
    palette = colors or (1 << bpp if bpp <= 8 else 0)
    if important not in (0, palette):
        r.add(HIDDEN, "Non-zero unused BMP header field", f"important colours {important}")


def _bmp_colour_space(d: bytes, dib: int, cs_type: bytes, r: Report) -> None:
    """Endpoints and gamma only mean something for a calibrated colour space; elsewhere they must be empty."""
    endpoints_and_gamma = d[14 + 60 : 14 + 108]
    if cs_type not in (b"\x00\x00\x00\x00", b"BGRs", b" niW", b"DEBM", b"KNIL"):
        r.add(HIDDEN, "Unknown BMP colour space type", cs_type.hex())
    elif cs_type == b"\x00\x00\x00\x00":  # LCS_CALIBRATED_RGB: the numbers are real colour data
        if any(endpoints_and_gamma):
            r.add(COLOR, "Calibrated colour space endpoints and gamma (BMP)")
    elif any(endpoints_and_gamma):
        r.add(HIDDEN, "Non-zero BMP colour endpoints under a colour space that ignores them")


def _bmp_palette(d: bytes, start: int, entries: int, r: Report) -> None:
    """Palette entries are B, G, R and a reserved byte; the reserved byte and unused entries must be at rest."""
    table = d[start : start + 4 * entries]
    if len(table) < 4 * entries:
        return
    if any(table[3::4]):
        r.add(HIDDEN, "Non-zero reserved bytes in the BMP palette")


def _bmp_indices(d: bytes, pix_off: int, w: int, h: int, bpp: int, stride: int, entries: int, r: Report) -> None:
    """A pixel must point at a colour the table holds."""
    import numpy as np

    rows = np.frombuffer(d, dtype=np.uint8, count=stride * h, offset=pix_off).reshape(h, stride)
    if bpp == 8:
        top = int(rows[:, :w].max(initial=0))
    elif bpp == 4:
        packed = rows[:, : (w + 1) // 2]
        nibbles = np.stack([packed >> 4, packed & 15], axis=-1).reshape(h, -1)[:, :w]
        top = int(nibbles.max(initial=0))
    elif bpp == 1:
        top = int(np.unpackbits(rows, axis=1)[:, :w].max(initial=0))
    else:
        return
    if top >= entries:
        r.add(STRUCTURE, "Pixel refers to a palette entry that does not exist", f"entry {top} of {entries}")


def _bmp_row_padding(d: bytes, pix_off: int, w: int, h: int, bpp: int, stride: int, r: Report) -> None:
    """Every row is padded to four bytes, and a row of 1, 2 or 4-bit pixels may end in unused bits."""
    row_bytes = (w * bpp + 7) // 8
    pad = stride - row_bytes
    spare_bits = (8 - (w * bpp) % 8) % 8
    mask = (1 << spare_bits) - 1
    dirty = 0
    for y in range(h):
        base = pix_off + y * stride
        if pad and d[base + row_bytes : base + stride].strip(b"\x00"):
            dirty += 1
        elif spare_bits and d[base + row_bytes - 1] & mask:
            dirty += 1
    if dirty:
        r.add(HIDDEN, "Non-zero padding at the end of BMP pixel rows", f"{dirty} of {h} rows")


# --------------------------------------------------------------------------
# ISO-BMFF (HEIF / HEIC / AVIF)
# --------------------------------------------------------------------------

_IMAGE_ITEM_TYPES = {
    b"hvc1",
    b"av01",
    b"grid",
    b"iovl",
    b"iden",
    b"jpeg",
    b"hvt1",
    b"unci",
    b"j2k1",
    b"vvc1",
    b"avc1",
    b"mski",
}
_ALPHA_URNS = {b"urn:mpeg:mpegB:cicp:systems:auxiliary:alpha", b"urn:mpeg:hevc:2015:auxid:1"}
_STRUCTURAL_PROPS = {
    b"ispe",
    b"pixi",
    b"hvcC",
    b"av1C",
    b"pasp",
    b"lsel",
    b"a1op",
    b"a1lx",
    b"auxC",
    b"clap",
    b"irot",
    b"imir",
    b"colr",
    b"rloc",
    b"vvcC",
    b"jpgC",
    b"j2kH",
    b"uncC",
    b"cmpd",
}
_MOOV_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"dinf", b"edts", b"tref"}
_MOOV_LEAVES = {
    b"mvhd",
    b"tkhd",
    b"mdhd",
    b"hdlr",
    b"vmhd",
    b"nmhd",
    b"dref",
    b"stsd",
    b"stts",
    b"stsc",
    b"stsz",
    b"stco",
    b"co64",
    b"stss",
    b"elst",
    b"auxl",
    b"ctts",
    b"sdtp",
    b"sgpd",
    b"sbgp",
    b"stz2",
}


def _boxes(d: bytes, start: int, end: int, top: bool = False):
    pos = start
    while pos < end:
        if pos + 8 > end:
            raise _Malformed("truncated box header")
        size, typ = struct.unpack_from(">I4s", d, pos)
        hdr = 8
        if size == 1:
            (size,) = struct.unpack_from(">Q", d, pos + 8)
            hdr = 16
        elif size == 0:
            if not top:  # only the last box of the file may run "to the end"; elsewhere it would swallow its siblings
                raise _Malformed(f"box '{typ.decode('latin-1')}' claims to extend to the end inside another box")
            size = end - pos
        if size < hdr or pos + size > end:
            raise _Malformed(f"box '{typ.decode('latin-1')}' overruns its container")
        if typ == b"uuid":
            hdr += 16
        yield typ, pos + hdr, pos + size
        pos += size


def _cstr(d: bytes, pos: int, end: int) -> tuple[bytes, int]:
    z = d.find(b"\x00", pos, end)
    if z < 0:
        return d[pos:end], end
    return d[pos:z], z + 1


def _inspect_isobmff(d: bytes, r: Report) -> None:
    ctx = {"items": {}, "props": [], "assoc": {}, "refs": [], "primary": None, "iloc": {}, "mdat": []}
    has_meta = False
    for typ, s, e in _boxes(d, 0, len(d), top=True):
        if typ == b"ftyp":
            continue
        if typ == b"meta":
            has_meta = True
            _heif_meta(d, s + 4, e, r, ctx)
        elif typ == b"mdat":
            ctx["mdat"].append((s, e))
        elif typ in (b"free", b"skip"):
            if d[s:e].strip(b"\x00"):
                r.add(HIDDEN, f"'{typ.decode()}' box containing data", f"{e - s} bytes ({_describe_blob(d[s:e])})")
        elif typ == b"moov":
            r.info["sequence"] = True
            _moov(d, s, e, r)
        elif typ == b"uuid" and d[s - 16 : s] == C2PA_BMFF_UUID:
            r.add(METADATA, C2PA_LABEL, f"{e - s} bytes")
        elif typ == b"uuid":
            r.add(HIDDEN, "Vendor-specific 'uuid' box", f"{e - s} bytes")
        else:
            r.add(HIDDEN, f"Unknown top-level box '{typ.decode('latin-1')}'", f"{e - s} bytes")
    if not has_meta:
        raise _Malformed("no 'meta' box")
    _heif_items(d, r, ctx)


def _heif_meta(d: bytes, start: int, end: int, r: Report, ctx: dict) -> None:
    for typ, s, e in _boxes(d, start, end):
        if typ == b"hdlr":
            if d[s + 8 : s + 12] != b"pict":
                r.add(STRUCTURE, "Non-picture handler", _printable(d[s + 8 : s + 12]))
        elif typ == b"pitm":
            version = d[s]
            (ctx["primary"],) = struct.unpack_from(">H" if version == 0 else ">I", d, s + 4)
        elif typ == b"iinf":
            version = d[s]
            pos = s + 4 + (2 if version == 0 else 4)
            for ityp, is_, ie in _boxes(d, pos, e):
                if ityp != b"infe":
                    continue
                v = d[is_]
                if v < 2:
                    continue
                if v == 2:
                    item_id, _prot, item_type = struct.unpack_from(">HH4s", d, is_ + 4)
                    p = is_ + 12
                else:
                    item_id, _prot, item_type = struct.unpack_from(">IH4s", d, is_ + 4)
                    p = is_ + 14
                name, p = _cstr(d, p, ie)
                content_type = b""
                if item_type == b"mime":
                    content_type, p = _cstr(d, p, ie)
                ctx["items"][item_id] = {"type": item_type, "name": name, "ctype": content_type}
        elif typ == b"iref":
            version = d[s]
            fmt = ">H" if version == 0 else ">I"
            step = 2 if version == 0 else 4
            for rtyp, rs, _end in _boxes(d, s + 4, e):
                (src,) = struct.unpack_from(fmt, d, rs)
                (count,) = struct.unpack_from(">H", d, rs + step)
                dsts = [struct.unpack_from(fmt, d, rs + step + 2 + step * i)[0] for i in range(count)]
                ctx["refs"].append((rtyp, src, dsts))
        elif typ == b"iprp":
            for ptyp, ps, pe in _boxes(d, s, e):
                if ptyp == b"ipco":
                    for prop, qs, qe in _boxes(d, ps, pe):
                        ctx["props"].append((prop, qs, qe))
                elif ptyp == b"ipma":
                    version, flags = d[ps], int.from_bytes(d[ps + 1 : ps + 4], "big")
                    (count,) = struct.unpack_from(">I", d, ps + 4)
                    pos = ps + 8
                    for _ in range(count):
                        if version < 1:
                            (item_id,) = struct.unpack_from(">H", d, pos)
                            pos += 2
                        else:
                            (item_id,) = struct.unpack_from(">I", d, pos)
                            pos += 4
                        n = d[pos]
                        pos += 1
                        assoc = []
                        for _ in range(n):
                            if flags & 1:
                                (v,) = struct.unpack_from(">H", d, pos)
                                pos += 2
                                assoc.append(v & 0x7FFF)
                            else:
                                assoc.append(d[pos] & 0x7F)
                                pos += 1
                        ctx["assoc"][item_id] = assoc
        elif typ == b"iloc":
            _heif_iloc(d, s, e, ctx)
        elif typ in (b"idat", b"dinf", b"grpl"):
            continue
        else:
            r.add(HIDDEN, f"Unknown metadata box '{typ.decode('latin-1')}'", f"{e - s} bytes")


def _heif_iloc(d: bytes, s: int, e: int, ctx: dict) -> None:
    version = d[s]
    sizes = d[s + 4]
    off_size, len_size = sizes >> 4, sizes & 15
    sizes2 = d[s + 5]
    base_size, idx_size = sizes2 >> 4, (sizes2 & 15 if version in (1, 2) else 0)
    pos = s + 6

    def read(nbytes):
        nonlocal pos
        if nbytes == 0:
            return 0
        v = int.from_bytes(d[pos : pos + nbytes], "big")
        pos += nbytes
        return v

    count = read(2 if version < 2 else 4)
    for _ in range(count):
        item_id = read(2 if version < 2 else 4)
        method = read(2) & 15 if version in (1, 2) else 0
        read(2)  # data reference index
        base = read(base_size)
        n_ext = read(2)
        extents = []
        for _ in range(n_ext):
            read(idx_size)
            extents.append((base + read(off_size), read(len_size)))
        ctx["iloc"][item_id] = (method, extents)


def _heif_item_data(d: bytes, ctx: dict, item_id: int) -> bytes:
    method, extents = ctx["iloc"].get(item_id, (None, []))
    if method != 0:
        return b""
    return b"".join(d[o : o + n] for o, n in extents)


def _heif_items(d: bytes, r: Report, ctx: dict) -> None:
    items, props, assoc = ctx["items"], ctx["props"], ctx["assoc"]
    primary = ctx["primary"]
    if primary is None or primary not in items:
        raise _Malformed("no primary item")
    # Items that legitimately make up the primary picture: grid tiles + alpha.
    legit = {primary}
    changed = True
    while changed:
        changed = False
        for rtyp, src, dsts in ctx["refs"]:
            if rtyp == b"dimg" and src in legit:
                for dst in dsts:
                    if dst not in legit:
                        legit.add(dst)
                        changed = True
    aux_types: dict[int, bytes] = {}
    for item_id, plist in assoc.items():
        for idx in plist:
            if 1 <= idx <= len(props) and props[idx - 1][0] == b"auxC":
                _, s, e = props[idx - 1]
                aux_types[item_id] = _cstr(d, s + 4, e)[0]
    for rtyp, src, dsts in ctx["refs"]:
        if rtyp == b"auxl" and set(dsts) & legit and aux_types.get(src) in _ALPHA_URNS:
            legit.add(src)
    reported_props: set[int] = set()
    for item_id, item in items.items():
        itype = item["type"]
        if itype == b"Exif":
            blob = _heif_item_data(d, ctx, item_id)
            if len(blob) >= 4:
                (skip,) = struct.unpack_from(">I", blob, 0)
                _exif_findings(blob[4 + skip :], r)
            else:
                r.add(METADATA, "EXIF")
        elif itype == b"mime":
            if b"rdf+xml" in item["ctype"] or b"xmp" in item["ctype"].lower():
                r.add(METADATA, "XMP", _xmp_detail(_heif_item_data(d, ctx, item_id)))
            else:
                r.add(METADATA, "Embedded MIME item", _printable(item["ctype"]))
        elif itype == b"uri ":
            r.add(METADATA, "URI item")
        elif itype == b"tmap":
            r.add(HIDDEN, "HDR gain-map image")
        elif itype in _IMAGE_ITEM_TYPES:
            if item_id not in legit:
                kind = "Additional image"
                for rtyp, src, _dsts in ctx["refs"]:
                    if src == item_id and rtyp == b"thmb":
                        kind = "Thumbnail image"
                if item_id in aux_types:
                    urn = aux_types[item_id]
                    kind = (
                        "Depth map"
                        if b"depth" in urn.lower()
                        else (
                            "HDR gain map" if b"gainmap" in urn.lower() else f"Auxiliary image ({_printable(urn, 50)})"
                        )
                    )
                if (
                    not any(src == item_id and rtyp == b"dimg" for rtyp, src, _ in ctx["refs"])
                    or kind != "Additional image"
                ):
                    r.add(HIDDEN, kind, f"item {item_id}")
        else:
            r.add(HIDDEN, f"Unknown item type '{itype.decode('latin-1')}'", f"item {item_id}")
        for idx in assoc.get(item_id, []):
            if idx == 0 or idx > len(props) or idx in reported_props:
                continue
            ptyp, s, e = props[idx - 1]
            if ptyp == b"colr":
                if d[s : s + 4] in (b"prof", b"rICC"):
                    reported_props.add(idx)
                    _icc_finding(d[s + 4 : e], r)
            elif ptyp == b"ispe" and item_id == primary:
                r.info["ispe"] = struct.unpack_from(">II", d, s + 4)
            elif ptyp == b"av1C" and item_id == primary and e - s >= 3:
                b = d[s + 2]
                r.info["chroma"] = {(1, 1): "4:2:0", (1, 0): "4:2:2"}.get(((b >> 3) & 1, (b >> 2) & 1), "4:4:4")
            elif ptyp == b"hvcC" and item_id == primary and e - s >= 17:
                r.info["chroma"] = {1: "4:2:0", 2: "4:2:2"}.get(d[s + 16] & 3, "4:4:4")
            elif ptyp == b"udes":
                reported_props.add(idx)
                r.add(METADATA, "User description property", f"{e - s} bytes")
            elif ptyp in (b"irot", b"imir", b"clap"):
                if item_id == primary:
                    r.info.setdefault("transforms", []).append(ptyp.decode())
                    if ptyp == b"clap":
                        r.info["clap"] = struct.unpack_from(">IIIIiIiI", d, s)
                    elif ptyp == b"irot":
                        r.info["irot"] = d[s] & 3
                    else:
                        r.info["imir"] = d[s] & 1
            elif ptyp not in _STRUCTURAL_PROPS:
                reported_props.add(idx)
                r.add(HIDDEN, f"Unknown item property '{ptyp.decode('latin-1')}'", f"{e - s} bytes")
    clap, ispe = r.info.get("clap"), r.info.get("ispe")
    if clap and ispe and clap[1] and clap[3] and clap[5] and clap[7]:
        width, height = ispe
        cw, ch = clap[0] / clap[1], clap[2] / clap[3]
        left = (width - 1) / 2 + clap[4] / clap[5] - (cw - 1) / 2
        top = (height - 1) / 2 + clap[6] / clap[7] - (ch - 1) / 2
        right, bottom = width - left - cw, height - top - ch
        hidden = int(round(width * height - cw * ch))
        if hidden > 0:
            if left == 0 and top == 0 and 0 <= right < 64 and 0 <= bottom < 64:
                # Encoders pad to the codec block grid on the right/bottom edge.
                r.info["codec_padding"] = hidden
            else:
                r.add(HIDDEN, "Pixels outside the visible crop (clean aperture)", f"{hidden} pixels")
    if "chroma" not in r.info:
        for ptyp, s, e in props:
            if ptyp == b"av1C" and e - s >= 3:
                b = d[s + 2]
                r.info["chroma"] = {(1, 1): "4:2:0", (1, 0): "4:2:2"}.get(((b >> 3) & 1, (b >> 2) & 1), "4:4:4")
                break
            if ptyp == b"hvcC" and e - s >= 17:
                r.info["chroma"] = {1: "4:2:0", 2: "4:2:2"}.get(d[s + 16] & 3, "4:4:4")
                break
    # Media data not referenced by any item is hidden payload.
    if ctx["mdat"] and not r.info.get("sequence"):
        covered = sorted(ext for _m, exts in ctx["iloc"].values() if _m == 0 for ext in exts)
        for ms, me in ctx["mdat"]:
            pos = ms
            unref = 0
            for o, n in covered:
                if o + n <= ms or o >= me:
                    continue
                if o > pos:
                    unref += o - pos
                pos = max(pos, o + n)
            unref += max(0, me - pos)
            if unref:
                r.add(HIDDEN, "Unreferenced media data", f"{unref} bytes")


def _moov(d: bytes, start: int, end: int, r: Report) -> None:
    for typ, s, e in _boxes(d, start, end):
        if typ in _MOOV_CONTAINERS:
            _moov(d, s, e, r)
        elif typ in (b"mvhd", b"tkhd", b"mdhd"):
            version = d[s]
            if version == 1:
                created, modified = struct.unpack_from(">QQ", d, s + 4)
            else:
                created, modified = struct.unpack_from(">II", d, s + 4)
            if created or modified:
                r.add(METADATA, "Creation/modification timestamps", f"in '{typ.decode()}'")
        elif typ in _MOOV_LEAVES:
            continue
        elif typ in (b"udta", b"meta"):
            r.add(METADATA, f"Track metadata box '{typ.decode()}'", f"{e - s} bytes")
        else:
            r.add(HIDDEN, f"Unknown box '{typ.decode('latin-1')}' in image sequence", f"{e - s} bytes")


__all__ = [
    "Finding",
    "Report",
    "canonical_form_problems",
    "inspect_bytes",
    "sniff_format",
    "png_raw_size",
    "METADATA",
    "HIDDEN",
    "COLOR",
    "STRUCTURE",
]
