"""Synthetic "worst case" images used by the self test and the test suite.

Every fixture is packed with as much metadata and hidden data as its format
allows, and every piece of it contains a unique canary string.  A cleaned
file must not contain any canary anywhere in its bytes.
"""

from __future__ import annotations

import io
import struct
import zlib

import numpy as np
from PIL import Image, PngImagePlugin

CANARY = b"CANARY"
CANARIES = [
    b"CANARY-MAKE",
    b"CANARY-MODEL",
    b"CANARY-ARTIST",
    b"CANARY-SERIAL",
    b"CANARY-SOFTWARE",
    b"CANARY-XMP",
    b"CANARY-COMMENT",
    b"CANARY-ICC",
    b"CANARY-TRAILER",
    b"CANARY-TEXT",
    b"CANARY-IPTC",
    b"CANARY-C2PA",
    b"CANARY-MAKERNOTE",
    b"CANARY-HIDDEN",
    b"CANARY-PALETTE",
]

DISPLAY_P3 = ((0.5151, 0.2412, -0.0011), (0.2919, 0.6922, 0.0419), (0.1572, 0.0666, 0.7841))
ADOBE_RGB = ((0.6097, 0.3111, 0.0195), (0.2053, 0.6257, 0.0609), (0.1492, 0.0632, 0.7446))
SRGB_PARA = (2.4, 1 / 1.055, 0.055 / 1.055, 1 / 12.92, 0.04045)


# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------


def photo(width: int = 160, height: int = 120, seed: int = 1) -> np.ndarray:
    """A deterministic photo-like RGB image (gradients, shapes and sensor noise)."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:height, 0:width].astype(np.float32)
    r = 128 + 100 * np.sin(x / 17.0) * np.cos(y / 23.0)
    g = 255 * x / max(1, width - 1)
    b = 255 * y / max(1, height - 1)
    img = np.stack([r, g, b], axis=-1)
    cy, cx = height * 0.45, width * 0.6
    disk = (x - cx) ** 2 + (y - cy) ** 2 < (min(width, height) * 0.2) ** 2
    img[disk] = (230, 40, 60)
    img += rng.normal(0, 6, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def _s15(v: float) -> bytes:
    return struct.pack(">i", int(round(v * 65536)))


def rgb_profile(description: str, primaries=DISPLAY_P3, gamma: float | None = None) -> bytes:
    """A complete matrix/TRC ICC v2 profile with identifying text and a date."""

    def xyz(v):
        return b"XYZ \x00\x00\x00\x00" + b"".join(_s15(c) for c in v)

    if gamma is None:
        trc = b"para\x00\x00\x00\x00" + struct.pack(">HH", 3, 0) + b"".join(_s15(p) for p in SRGB_PARA)
    else:
        trc = b"curv\x00\x00\x00\x00" + struct.pack(">IH", 1, int(round(gamma * 256))) + b"\x00\x00"
    desc_text = description.encode("ascii") + b"\x00"
    desc = b"desc\x00\x00\x00\x00" + struct.pack(">I", len(desc_text)) + desc_text + b"\x00" * 12 + b"\x00" * 67
    tags = [
        (b"desc", desc),
        (b"cprt", b"text\x00\x00\x00\x00" + b"Copyright CANARY-ICC owner\x00"),
        (b"dmnd", desc),
        (b"wtpt", xyz((0.9642, 1.0, 0.8249))),
        (b"rXYZ", xyz(primaries[0])),
        (b"gXYZ", xyz(primaries[1])),
        (b"bXYZ", xyz(primaries[2])),
        (b"rTRC", trc),
        (b"gTRC", trc),
        (b"bTRC", trc),
    ]
    table_end = 128 + 4 + 12 * len(tags)
    table, data = struct.pack(">I", len(tags)), b""
    for sig, body in tags:
        data += b"\x00" * ((-len(data)) % 4)
        table += struct.pack(">4sII", sig, table_end + len(data), len(body))
        data += body
    size = table_end + len(data)
    header = struct.pack(">I4sI4s4s4s", size, b"appl", 0x02100000, b"mntr", b"RGB ", b"XYZ ")
    header += struct.pack(">6H", 2024, 5, 17, 13, 37, 0) + b"acsp" + b"APPL" + struct.pack(">I", 0)
    header += b"APPL" + b"MBP1" + struct.pack(">QI", 0, 0) + _s15(0.9642) + _s15(1.0) + _s15(0.8249)
    header += b"appl" + b"\x11" * 16 + b"\x00" * 28
    assert len(header) == 128
    return header + table + data


def _ifd(buf: bytearray, entries, next_off: int = 0) -> int:
    """Append a big-endian TIFF IFD (with its out-of-line data) to ``buf``."""
    if len(buf) % 2:
        buf += b"\x00"
    off = len(buf)
    data_off = off + 2 + 12 * len(entries) + 4
    body, data = bytearray(), bytearray()
    for tag, typ, val in sorted(entries, key=lambda e: e[0]):
        if typ == 2:
            raw, count = val + b"\x00", len(val) + 1
        elif typ == 3:
            raw, count = struct.pack(f">{len(val)}H", *val), len(val)
        elif typ == 4:
            raw, count = struct.pack(f">{len(val)}I", *val), len(val)
        elif typ == 5:
            raw, count = b"".join(struct.pack(">II", n, d) for n, d in val), len(val)
        else:
            raw, count = bytes(val), len(val)
        if len(raw) <= 4:
            field = raw.ljust(4, b"\x00")
        else:
            if (data_off + len(data)) % 2:
                data += b"\x00"
            field = struct.pack(">I", data_off + len(data))
            data += raw
        body += struct.pack(">HHI", tag, typ, count) + field
    buf += struct.pack(">H", len(entries)) + body + struct.pack(">I", next_off) + data
    return off


def exif_blob(orientation: int = 1, gps: bool = True, thumbnail: bytes | None = None) -> bytes:
    """A realistic EXIF block: camera, serial, maker note, GPS and a thumbnail."""
    buf = bytearray(b"MM\x00\x2a\x00\x00\x00\x00")
    exif_ifd = _ifd(
        buf,
        [
            (0x9003, 2, b"2024:05:17 13:37:00"),
            (0xA431, 2, b"CANARY-SERIAL-0042"),
            (0x927C, 7, b"CANARY-MAKERNOTE" + bytes(range(32))),
        ],
    )
    gps_ifd = None
    if gps:
        gps_ifd = _ifd(
            buf,
            [
                (1, 2, b"N"),
                (2, 5, [(37, 1), (25, 1), (1934, 100)]),
                (3, 2, b"W"),
                (4, 5, [(122, 1), (5, 1), (238, 100)]),
            ],
        )
    ifd1 = 0
    if thumbnail:
        if len(buf) % 2:
            buf += b"\x00"
        thumb_off = len(buf)
        buf += thumbnail
        ifd1 = _ifd(buf, [(0x0103, 3, [6]), (0x0201, 4, [thumb_off]), (0x0202, 4, [len(thumbnail)])])
    entries = [
        (0x010F, 2, b"CANARY-MAKE Corp"),
        (0x0110, 2, b"CANARY-MODEL X1"),
        (0x0112, 3, [orientation]),
        (0x0131, 2, b"CANARY-SOFTWARE 1.0"),
        (0x0132, 2, b"2024:05:17 13:37:00"),
        (0x013B, 2, b"CANARY-ARTIST Jane Doe"),
        (0x8769, 4, [exif_ifd]),
    ]
    if gps_ifd is not None:
        entries.append((0x8825, 4, [gps_ifd]))
    ifd0 = _ifd(buf, entries, ifd1)
    struct.pack_into(">I", buf, 4, ifd0)
    return b"Exif\x00\x00" + bytes(buf)


XMP = (
    b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
    b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description '
    b'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
    b'xmlns:exif="http://ns.adobe.com/exif/1.0/" xmp:CreatorTool="CANARY-XMP editor" '
    b'exif:GPSLatitude="37,25.32N"><dc:creator>CANARY-XMP Jane</dc:creator></rdf:Description>'
    b'</rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
)


def _jpeg_segment(marker: int, payload: bytes) -> bytes:
    return bytes((0xFF, marker)) + struct.pack(">H", len(payload) + 2) + payload


def _insert_after_soi(jpeg: bytes, segments: bytes) -> bytes:
    return jpeg[:2] + segments + jpeg[2:]


def _png_chunk(ctype: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF)


def _png_insert_before_idat(png: bytes, chunks: bytes) -> bytes:
    pos = png.index(b"IDAT") - 4
    return png[:pos] + chunks + png[pos:]


def _small_jpeg(seed: int = 9) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(photo(32, 24, seed)).save(buf, "JPEG", quality=70)
    return buf.getvalue()


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def jpeg_everything(orientation: int = 6) -> bytes:
    base = io.BytesIO()
    Image.fromarray(photo(160, 120)).save(base, "JPEG", quality=92)
    icc = rgb_profile("CANARY-ICC Display P3 of Jane's laptop")
    segs = _jpeg_segment(0xE1, exif_blob(orientation, True, _small_jpeg()))
    segs += _jpeg_segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00" + XMP)
    segs += _jpeg_segment(0xE2, b"ICC_PROFILE\x00\x01\x01" + icc)
    iptc = b"\x1c\x02\x50" + struct.pack(">H", 19) + b"CANARY-IPTC byline!"
    irb = b"8BIM\x04\x04\x00\x00" + struct.pack(">I", len(iptc)) + iptc + (b"\x00" if len(iptc) % 2 else b"")
    segs += _jpeg_segment(0xED, b"Photoshop 3.0\x00" + irb)
    segs += _jpeg_segment(0xEB, b"JP\x00\x00\x00\x00\x00\x01" + b"jumbCANARY-C2PA manifest")
    segs += _jpeg_segment(0xFE, b"CANARY-COMMENT shot at home")
    data = _insert_after_soi(base.getvalue(), segs)
    return data + b"PK\x03\x04CANARY-TRAILER secret.zip"


def mpo_with_depth() -> bytes:
    a = Image.fromarray(photo(96, 64, 1))
    b = Image.fromarray(photo(96, 64, 7))
    buf = io.BytesIO()
    a.save(buf, "MPO", save_all=True, append_images=[b], exif=exif_blob(1, True)[0:])
    return buf.getvalue()


def png_everything() -> bytes:
    base = io.BytesIO()
    Image.fromarray(photo(120, 90)).save(base, "PNG")
    chunks = _png_chunk(b"tEXt", b"Author\x00CANARY-TEXT Jane Doe")
    chunks += _png_chunk(b"zTXt", b"Comment\x00\x00" + zlib.compress(b"CANARY-COMMENT compressed"))
    chunks += _png_chunk(b"iTXt", b"XML:com.adobe.xmp\x00\x00\x00\x00\x00" + XMP)
    chunks += _png_chunk(b"eXIf", exif_blob(1, True)[6:])
    chunks += _png_chunk(b"iCCP", b"P3\x00\x00" + zlib.compress(rgb_profile("CANARY-ICC P3")))
    chunks += _png_chunk(b"tIME", struct.pack(">HBBBBB", 2024, 5, 17, 13, 37, 0))
    chunks += _png_chunk(b"pHYs", struct.pack(">IIB", 11811, 11811, 1))
    chunks += _png_chunk(b"prVt", b"CANARY-HIDDEN private chunk")
    data = _png_insert_before_idat(base.getvalue(), chunks)
    # aCropalypse-style remnant of an earlier, larger version after IEND.
    remnant = _png_chunk(b"IDAT", zlib.compress(b"CANARY-TRAILER old pixels")) + _png_chunk(b"IEND", b"")
    return data + remnant


def png_palette_tricks() -> bytes:
    """Paletted PNG with unused/duplicate entries that hold data, and tRNS."""
    idx = (np.arange(64 * 48).reshape(48, 64) // 97 % 6).astype(np.uint8)
    idx[0:4, 0:4] = 6  # duplicate of colour 1
    im = Image.frombytes("P", (64, 48), idx.tobytes())
    palette = [10, 20, 30, 200, 0, 0, 0, 200, 0, 0, 0, 200, 250, 250, 250, 5, 5, 5, 200, 0, 0]
    secret = list(b"CANARY-PALETTE hidden in unused palette entries!!")
    palette += secret + [0] * (768 - len(palette) - len(secret))
    im.putpalette(palette)
    buf = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("Title", "CANARY-TEXT")
    im.save(buf, "PNG", transparency=bytes([0, 255, 128, 255, 255, 255, 255]), pnginfo=info)
    return buf.getvalue()


def png_hidden_under_alpha() -> bytes:
    arr = np.zeros((40, 60, 4), np.uint8)
    arr[..., :3] = photo(60, 40)
    arr[..., 3] = 255
    arr[:, :30, 3] = 0  # left half fully transparent ...
    msg = np.frombuffer((b"CANARY-HIDDEN under alpha " * 200)[: 40 * 30 * 3], np.uint8).reshape(40, 30, 3)
    arr[:, :30, :3] = msg  # ... but carrying a hidden message
    buf = io.BytesIO()
    Image.frombytes("RGBA", (60, 40), arr.tobytes()).save(buf, "PNG")
    return buf.getvalue()


def png_16bit_adobe() -> bytes:
    from .codecs import encode_png

    rng = np.random.default_rng(3)
    base = photo(64, 48).astype(np.uint16) * 257
    arr = (base + rng.integers(0, 256, base.shape)).astype(np.uint16)
    data = encode_png(arr, 16, 2, icc=rgb_profile("CANARY-ICC Adobe RGB (1998)", ADOBE_RGB, 563 / 256))
    return _png_insert_before_idat(data, _png_chunk(b"tEXt", b"Software\x00CANARY-SOFTWARE"))


def png_lsb_stego() -> bytes:
    arr = photo(200, 150, 4).copy()
    rng = np.random.default_rng(11)
    arr = (arr & 0xFE) | rng.integers(0, 2, arr.shape, dtype=np.uint8)  # 100% LSB payload
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG")
    return buf.getvalue()


def apng_with_default_image() -> bytes:
    frames = [Image.fromarray(photo(48, 32, s)).convert("RGBA") for s in (20, 21, 22)]
    buf = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "CANARY-TEXT")
    frames[0].save(
        buf,
        "PNG",
        save_all=True,
        append_images=frames[1:],
        default_image=True,
        duration=[0, 120, 80],
        loop=2,
        pnginfo=info,
    )
    return buf.getvalue()


def gif_animated() -> bytes:
    frames = []
    for s in range(4):
        rgba = np.zeros((40, 56, 4), np.uint8)
        rgba[..., :3] = (photo(56, 40, s) // 64) * 64  # few colours
        rgba[..., 3] = 255
        rgba[5 + s * 5 : 15 + s * 5, 10:30, 3] = 0  # moving transparent hole
        frames.append(Image.frombytes("RGBA", (56, 40), rgba.tobytes()))
    buf = io.BytesIO()
    frames[0].save(
        buf,
        "GIF",
        save_all=True,
        append_images=frames[1:],
        duration=[100, 50, 70, 200],
        loop=0,
        comment=b"CANARY-COMMENT gif",
    )
    data = buf.getvalue()
    xmp_ext = b"\x21\xff\x0bXMP DataXMP" + b"".join(bytes([len(c)]) + c for c in _blocks(XMP)) + b"\x00"
    data = data[:-1] + xmp_ext + b"\x3b"
    return data + b"CANARY-TRAILER"


def _blocks(data: bytes, size: int = 255):
    return [data[i : i + size] for i in range(0, len(data), size)]


def webp_lossy_everything() -> bytes:
    buf = io.BytesIO()
    Image.fromarray(photo(120, 80)).save(
        buf, "WEBP", quality=90, exif=exif_blob(3, True), xmp=XMP, icc_profile=rgb_profile("CANARY-ICC webp")
    )
    return buf.getvalue() + b"CANARY-TRAILER"


def webp_lossless_alpha() -> bytes:
    arr = np.zeros((50, 70, 4), np.uint8)
    arr[..., :3] = photo(70, 50, 5)
    arr[..., 3] = np.linspace(0, 255, 70, dtype=np.uint8)[None, :]
    buf = io.BytesIO()
    Image.frombytes("RGBA", (70, 50), arr.tobytes()).save(buf, "WEBP", lossless=True, exact=True, exif=exif_blob(1))
    return buf.getvalue()


def webp_animated() -> bytes:
    frames = [Image.fromarray(photo(40, 30, s)) for s in (1, 2, 3)]
    buf = io.BytesIO()
    frames[0].save(
        buf,
        "WEBP",
        save_all=True,
        append_images=frames[1:],
        duration=[90, 90, 90],
        loop=0,
        lossless=True,
        exif=exif_blob(1),
        xmp=XMP,
    )
    return buf.getvalue()


def tiff_multipage() -> bytes:
    """Two real pages plus a reduced-resolution 'thumbnail' page, all tagged."""
    from PIL import TiffImagePlugin

    pages = [Image.fromarray(photo(80, 60, 1)), Image.fromarray(photo(80, 60, 2)), Image.fromarray(photo(20, 15, 3))]
    buf = io.BytesIO()
    with TiffImagePlugin.AppendingTiffWriter(buf, new=True) as tf:
        for i, page in enumerate(pages):
            tags = TiffImagePlugin.ImageFileDirectory_v2()
            tags[270] = "CANARY-TEXT description"
            tags[271] = "CANARY-MAKE"
            tags[305] = "CANARY-SOFTWARE"
            tags[315] = "CANARY-ARTIST"
            tags[700] = XMP
            tags[33432] = "CANARY-TEXT copyright"
            if i == 2:
                tags[254] = 1  # reduced-resolution image (thumbnail)
            page.save(tf, "TIFF", tiffinfo=tags, compression="tiff_lzw")
            tf.newFrame()
    return buf.getvalue() + b"CANARY-TRAILER"


def bmp_with_trailer() -> bytes:
    buf = io.BytesIO()
    Image.fromarray(photo(33, 21)).save(buf, "BMP")  # odd width exercises row padding
    return buf.getvalue() + b"CANARY-TRAILER"


def avif_everything() -> bytes:
    buf = io.BytesIO()
    Image.fromarray(photo(64, 48)).save(buf, "AVIF", quality=85, exif=exif_blob(8, True), xmp=XMP)
    return buf.getvalue()


def heic_everything() -> bytes | None:
    try:
        import pillow_heif
    except ImportError:
        return None
    pillow_heif.register_heif_opener()
    buf = io.BytesIO()
    Image.fromarray(photo(64, 48)).save(buf, "HEIF", quality=85, exif=exif_blob(6, True), xmp=XMP)
    return buf.getvalue()


def cmyk_jpeg() -> bytes:
    buf = io.BytesIO()
    Image.fromarray(photo(64, 48)).convert("CMYK").save(buf, "JPEG", quality=90)
    data = buf.getvalue()
    return _insert_after_soi(data, _jpeg_segment(0xFE, b"CANARY-COMMENT cmyk"))


def all_fixtures() -> dict[str, bytes]:
    out = {
        "jpeg_everything.jpg": jpeg_everything(),
        "mpo_depth.jpg": mpo_with_depth(),
        "png_everything.png": png_everything(),
        "png_palette.png": png_palette_tricks(),
        "png_hidden_alpha.png": png_hidden_under_alpha(),
        "png_16bit_adobe.png": png_16bit_adobe(),
        "png_stego.png": png_lsb_stego(),
        "apng_default.png": apng_with_default_image(),
        "gif_animated.gif": gif_animated(),
        "webp_lossy.webp": webp_lossy_everything(),
        "webp_lossless_alpha.webp": webp_lossless_alpha(),
        "webp_animated.webp": webp_animated(),
        "tiff_multipage.tif": tiff_multipage(),
        "bmp_trailer.bmp": bmp_with_trailer(),
        "avif_everything.avif": avif_everything(),
        "cmyk.jpg": cmyk_jpeg(),
    }
    heic = heic_everything()
    if heic is not None:
        out["heic_everything.heic"] = heic
    return out
