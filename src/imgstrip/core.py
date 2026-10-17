"""The stripping pipeline: decode -> normalise -> re-encode -> verify.

The guiding principle is that nothing is ever *copied* from the input file
except pixel values.  The input is decoded to raw pixels, a brand-new image
object (with an empty ``info`` dictionary) is built from those pixels, and a
new file is encoded from it.  The result is then re-parsed with the strict
container inspector and decoded again to prove that (a) it contains no
metadata or hidden data and (b) it shows the same picture.
"""

from __future__ import annotations

import contextlib
import io
import os
import struct
import threading
import warnings
import zlib
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageCms, UnidentifiedImageError

from . import codecs
from . import icc as iccmod
from . import prnu, stego
from .inspect import Finding, Report, inspect_bytes, transparent_palette_colour, unused_palette_entries

try:  # optional HEIC/HEIF support
    import pillow_heif
except ImportError:  # pragma: no cover - depends on the environment
    pillow_heif = None

# Formats we are willing to decode.  Deliberately excludes EPS/PDF/WMF (which
# call external interpreters or render vector data) and exotic scientific
# containers.
INPUT_FORMATS = (
    "JPEG",
    "MPO",
    "PNG",
    "WEBP",
    "GIF",
    "TIFF",
    "BMP",
    "DIB",
    "AVIF",
    "HEIF",
    "ICO",
    "PPM",
    "TGA",
    "PSD",
    "QOI",
    "JPEG2000",
    "PCX",
    "SGI",
    "ICNS",
    "DDS",
)
OUTPUT_FORMATS = ("JPEG", "PNG", "WEBP", "GIF", "TIFF", "BMP", "AVIF", "HEIF")
EXTENSIONS = {
    "JPEG": (".jpg", ".jpeg", ".jpe", ".jfif"),
    "PNG": (".png", ".apng"),
    "WEBP": (".webp",),
    "GIF": (".gif",),
    "TIFF": (".tif", ".tiff"),
    "BMP": (".bmp", ".dib"),
    "AVIF": (".avif", ".avifs"),
    "HEIF": (".heic", ".heif", ".hif"),
}
DEFAULT_QUALITY = {"JPEG": 95, "WEBP": 95, "AVIF": 90, "HEIF": 90}
ANIMATED_FORMATS = {"GIF", "PNG", "WEBP", "AVIF", "TIFF"}
ICC_CAPABLE = {"JPEG", "PNG", "WEBP", "TIFF", "AVIF", "HEIF"}

_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}
_TRANSPOSE_NP = {
    2: lambda a: a[:, ::-1],
    3: lambda a: a[::-1, ::-1],
    4: lambda a: a[::-1],
    5: lambda a: a.swapaxes(0, 1),
    6: lambda a: np.rot90(a, -1),
    7: lambda a: a[::-1, ::-1].swapaxes(0, 1),
    8: lambda a: np.rot90(a, 1),
}
_ORIENT_TEXT = {
    2: "mirrored",
    3: "rotated 180°",
    4: "flipped vertically",
    5: "transposed",
    6: "rotated 90° clockwise",
    7: "transversed",
    8: "rotated 90° counter-clockwise",
}


def dct_mode(options: "Options") -> str:
    """The normalised --scrub-dct setting: "off", "canonical" or "full"."""
    value = options.scrub_dct
    if value is True:
        return "full"
    if value in (False, None, "off"):
        return "off"
    if value in ("canonical", "full"):
        return value
    raise StripError(f"unknown DCT scrub mode {value!r} (use off, canonical or full)")


class StripError(Exception):
    """The file could not be cleaned (the reason is user-facing)."""


class VerificationError(StripError):
    """The produced file failed the post-write safety checks."""


@dataclass
class Options:
    format: str | None = None  # output format; None keeps the input format
    quality: int | None = None  # lossy quality; None uses DEFAULT_QUALITY
    jpeg_lossless: bool = False  # strip JPEGs without re-encoding the DCT data
    icc: str = "convert"  # convert | sanitize | drop
    auto_orient: bool = True  # bake EXIF orientation into the pixels
    scrub_lsb: bool = False  # randomise least-significant bits (anti-steganography)
    # JPEG only, off by default: "canonical" rebuilds the compressed data from its DCT coefficients in one canonical
    # form (pixels unchanged); "full" also destroys payloads stored in coefficient parities (True means "full").
    scrub_dct: str | bool = "off"
    dct_min_psnr: float = 25.0  # "full" refuses a picture it would change by more than this (dB); 0 disables
    scrub_prnu: bool = False  # desynchronise the camera sensor fingerprint (see prnu.py)
    optimize: bool = False  # extra compression trials for PNG
    max_pixels: int = 300_000_000


@dataclass
class Frame:
    image: Image.Image | None = None  # 8-bit path (fresh Pillow image)
    array: np.ndarray | None = None  # 16-bit path, uint16 (H, W, C)
    alpha: list[int] | None = None  # per-palette-entry alpha for "P" images
    key: int | None = None  # tRNS colour key for 16-bit grayscale
    duration: float | None = None

    @property
    def size(self) -> tuple[int, int]:
        if self.array is not None:
            return self.array.shape[1], self.array.shape[0]
        return self.image.size


@dataclass
class Result:
    data: bytes
    input_format: str
    output_format: str
    removed: list[Finding]
    actions: list[str]
    warnings: list[str]
    size: tuple[int, int]
    frames: int
    pixels_exact: bool
    input_report: Report = field(repr=False, default=None)
    # The written file decodes to exactly the pixels it was given (zero verification
    # tolerance).  Unlike pixels_exact this ignores deliberate pixel changes such as
    # the LSB scrub; it is false for lossy encodes and for quantised GIF frames.
    verified_exact: bool = True
    # The sensor-fingerprint disruption was requested and applied (it is skipped, with a
    # warning, for palette images, transparent colour keys and tiny images).
    prnu_applied: bool = False
    # What the engine did about --scrub-lsb (stego.SCRUB_*): "off" when it was not requested, and
    # "not_applied" when it was requested but could not be done (bit-exact JPEG, 1-bit or float
    # images).  Reports and credentials must use this, never the option.
    lsb_scrub: str = stego.SCRUB_OFF
    # What the engine did about --scrub-dct (stego.DCT_*), and the measured PSNR (dB) of the result against the input
    # when coefficients were scrubbed.
    dct_scrub: str = stego.DCT_OFF
    dct_psnr: float | None = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def heif_available() -> bool:
    return pillow_heif is not None


_registered = False


def _register_plugins() -> None:
    global _registered
    if _registered:
        return
    _registered = True
    if pillow_heif is not None:
        try:
            pillow_heif.options.THUMBNAILS = False
            pillow_heif.options.DEPTH_IMAGES = False
            pillow_heif.options.AUX_IMAGES = False
        except AttributeError:
            pass
        pillow_heif.register_heif_opener()


def _input_formats() -> list[str]:
    Image.init()
    return [f for f in INPUT_FORMATS if f in Image.OPEN]


# Pillow's decompression-bomb limit and the warning filters are process-wide, while the web app runs several jobs
# at once.  The first guard in saves the real values, the last one out restores them, and while any guard is
# active the strictest requested limit applies.  Each thread collects its own warnings.
_guard_lock = threading.Lock()
_guard_limits: list[int] = []
_guard_saved: dict = {}
_guard_local = threading.local()


def _record_warning(message, category, filename, lineno, file=None, line=None):  # noqa: ARG001 - warnings hook
    sinks = getattr(_guard_local, "sinks", None)
    if sinks:
        sinks[-1].append(warnings.WarningMessage(message, category, filename, lineno))


def _guard_enter(limit: int) -> None:
    with _guard_lock:
        if not _guard_limits:
            _guard_saved["pixels"] = Image.MAX_IMAGE_PIXELS
            cm = warnings.catch_warnings()
            cm.__enter__()
            _guard_saved["warnings"] = cm
            warnings.simplefilter("always")
            warnings.showwarning = _record_warning
        _guard_limits.append(limit)
        Image.MAX_IMAGE_PIXELS = min(_guard_limits)


def _guard_exit(limit: int) -> None:
    with _guard_lock:
        _guard_limits.remove(limit)
        if _guard_limits:
            Image.MAX_IMAGE_PIXELS = min(_guard_limits)
        else:
            Image.MAX_IMAGE_PIXELS = _guard_saved.pop("pixels")
            _guard_saved.pop("warnings").__exit__(None, None, None)


@contextlib.contextmanager
def _pillow_guard(collected: list[str], max_pixels: int = 300_000_000):
    """Tie Pillow's decompression-bomb limit to ``max_pixels`` and capture warnings.

    Pillow raises DecompressionBombError above 2 x MAX_IMAGE_PIXELS, and it
    checks again whenever a frame enlarges the canvas (a corrupt GIF frame
    can otherwise demand gigabytes).  Safe to use from several threads at once.
    """
    limit = max(1, max_pixels // 2)
    caught: list = []
    sinks = _guard_local.__dict__.setdefault("sinks", [])
    _guard_enter(limit)
    sinks.append(caught)
    try:
        yield
    finally:
        sinks.pop()
        _guard_exit(limit)
    for w in caught:
        if issubclass(w.category, Image.DecompressionBombWarning):
            continue
        msg = str(w.message)
        if msg not in collected:
            collected.append(msg)


def open_image(data: bytes) -> Image.Image:
    _register_plugins()
    try:
        return Image.open(io.BytesIO(data), formats=_input_formats())
    except Image.DecompressionBombError:
        raise
    except UnidentifiedImageError:
        from .inspect import sniff_format

        fmt = sniff_format(data)
        if fmt == "HEIF" and pillow_heif is None:
            raise StripError("HEIC/HEIF support needs the 'pillow-heif' package (pip install pillow-heif)") from None
        if fmt or data[:4] == b"8BPS":
            raise StripError(
                f"unsupported {fmt or 'PSD'} variant (e.g. 16-bit Lab or 16-bit PSD); convert it to a common format first"
            ) from None
        raise StripError("not a supported image format") from None
    except Exception as exc:  # noqa: BLE001 - corrupt headers raise arbitrary exceptions
        raise StripError(f"cannot read image: {exc}") from None


def camera_raw_kind(data: bytes) -> str | None:
    """Recognise camera RAW files (several are TIFF containers that would otherwise
    be 'cleaned' down to their small embedded preview)."""
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        if data[8:10] == b"CR":
            return "Canon CR2"
        try:
            from .inspect import _Tiff

            t = _Tiff(data)
            (first,) = struct.unpack_from(t.e + "I", data, 4)
            entries, _ = t.read_ifd(first)
            tags = {e[0] for e in entries}
            if 50706 in tags:
                return "DNG"
        except Exception:  # noqa: BLE001
            return None
    if data[:4] in (b"IIRO", b"IIRS", b"MMOR"):
        return "Olympus ORF"
    if data[:4] == b"IIU\x00":
        return "Panasonic RW2"
    if data[:8] == b"FUJIFILM":
        return "Fujifilm RAF"
    if data[4:12] == b"ftypcrx ":
        return "Canon CR3"
    return None


def canonical_format(name: str | None) -> str:
    return {"MPO": "JPEG", "DIB": "BMP"}.get(name or "", name or "")


def _from_array(arr: np.ndarray, mode: str) -> Image.Image:
    arr = np.ascontiguousarray(arr)
    h, w = arr.shape[:2]
    if mode == "I;16":
        return Image.frombytes(mode, (w, h), arr.astype("<u2").tobytes())
    return Image.frombytes(mode, (w, h), arr.astype(np.uint8).tobytes())


def fresh(img: Image.Image) -> Image.Image:
    """A new image holding only the pixel data (and palette) of ``img``."""
    out = Image.frombytes(img.mode, img.size, img.tobytes())
    if img.mode in ("P", "PA"):
        out.putpalette(img.getpalette("RGB") or [0, 0, 0], "RGB")
    return out


def _orientation(im: Image.Image, warn: list[str]) -> int:
    """EXIF/TIFF orientation only (XMP orientation is ignored by viewers)."""
    value = None
    try:
        raw = im.info.get("exif")
        if raw is None and "Raw profile type exif" in im.info:
            raw = bytes.fromhex("".join(str(im.info["Raw profile type exif"]).split("\n")[3:]))
        if raw:
            exif = Image.Exif()
            exif.load(raw)
            value = exif.get(0x0112)
        elif hasattr(im, "tag_v2"):
            value = im.tag_v2.get(274)
    except Exception:  # noqa: BLE001 - corrupt EXIF must not stop stripping
        warn.append("EXIF data is corrupt; orientation could not be read")
        return 1
    if isinstance(value, (tuple, list)) and value:
        value = value[0]
    if value is None:
        return 1
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = -1
    if value not in range(1, 9):
        warn.append(f"ignored invalid EXIF orientation value {value!r}")
        return 1
    return value


def _canonical_palette(img: Image.Image) -> tuple[Frame, int]:
    """Rebuild a paletted image with only the colours actually used.

    Unused entries, duplicate entries and colours of fully transparent
    entries are all places where data can hide; the rebuilt palette is sorted
    canonically so even the entry order carries no information.
    Returns the frame and the number of palette entries that were removed.
    """
    idx = np.asarray(img)
    rgb = img.getpalette("RGB") or []
    n = len(rgb) // 3
    alpha = [255] * 256
    if img.palette is not None and img.palette.mode == "RGBA":
        rgba = img.getpalette("RGBA") or []
        for i in range(len(rgba) // 4):
            alpha[i] = rgba[4 * i + 3]
    t = img.info.get("transparency")
    if isinstance(t, int) and 0 <= t < 256:
        alpha[t] = 0
    elif isinstance(t, (bytes, bytearray)):
        for i, v in enumerate(t[:256]):
            alpha[i] = min(alpha[i], v)
    used = np.flatnonzero(np.bincount(idx.ravel(), minlength=256))
    colors = {}
    for i in used.tolist():
        c = (rgb[3 * i], rgb[3 * i + 1], rgb[3 * i + 2], alpha[i]) if i < n else (0, 0, 0, alpha[i])
        colors[i] = (0, 0, 0, 0) if c[3] == 0 else c
    uniq = sorted(set(colors.values()), key=lambda c: (c[3] == 255, c))
    pos = {c: k for k, c in enumerate(uniq)}
    lut = np.zeros(256, np.uint8)
    for i, c in colors.items():
        lut[i] = pos[c]
    new = Image.frombytes("P", img.size, lut[idx].tobytes())
    new.putpalette([v for c in uniq for v in c[:3]], "RGB")
    alphas = [c[3] for c in uniq]
    return Frame(image=new, alpha=alphas if any(a < 255 for a in alphas) else None), max(0, n - len(uniq))


def _palette_to_rgba(frame: Frame) -> np.ndarray:
    img = frame.image
    pal = np.array(img.getpalette("RGB") or [0, 0, 0], np.uint8).reshape(-1, 3)
    table = np.zeros((256, 4), np.uint8)
    table[: len(pal), :3] = pal
    table[:, 3] = 255
    if frame.alpha:
        table[: len(frame.alpha), 3] = frame.alpha
    return table[np.asarray(img)]


def _rgba_array(frame: Frame) -> np.ndarray:
    """Comparable (H, W, 4) uint8 view of a frame (8-bit path only)."""
    img = frame.image
    if img.mode == "P":
        return _palette_to_rgba(frame)
    if img.mode == "RGBA":
        return np.asarray(img)
    return np.asarray(img.convert("RGBA"))


def _has_alpha(frame: Frame) -> bool:
    if frame.array is not None:
        return frame.array.shape[2] in (2, 4)
    img = frame.image
    return img.mode in ("LA", "RGBA") or (img.mode == "P" and frame.alpha is not None)


def _flatten_white(rgba: np.ndarray) -> np.ndarray:
    a = rgba[..., 3:4].astype(np.float32) / 255.0
    rgb = rgba[..., :3].astype(np.float32) * a + 255.0 * (1.0 - a)
    return np.rint(rgb).astype(np.uint8)


def _premultiplied(px: np.ndarray, full: int) -> np.ndarray:
    """Colour weighted by alpha (last channel): what a viewer actually shows.

    Colour under fully transparent pixels drops out, and colour under
    semi-transparent pixels counts in proportion to its visibility.
    """
    px = px.astype(np.int64)
    out = px.copy()
    out[..., :-1] = (px[..., :-1] * px[..., -1:] + full // 2) // full
    return out


def _mean_error(got: np.ndarray, want: np.ndarray, alpha: bool, exact: bool) -> float:
    """Mean absolute difference, computed in row blocks to bound memory.

    With ``exact`` it returns as soon as any difference is found.
    """
    if got.ndim == 2:
        got, want = got[..., None], want[..., None]
    full = 65535 if got.dtype == np.uint16 or want.dtype == np.uint16 else 255
    if exact and not alpha and got.dtype == want.dtype:
        return 0.0 if np.array_equal(got, want) else 1.0
    rows = max(1, (1 << 22) // max(1, got.shape[1] * got.shape[2]))
    total = 0.0
    for top in range(0, got.shape[0], rows):
        a, b = got[top : top + rows], want[top : top + rows]
        if exact:
            # Bit-exact, except colour under fully transparent pixels (zeroed on purpose).
            a, b = a.astype(np.int64), b.astype(np.int64)
            a[a[..., -1] == 0] = 0
            b[b[..., -1] == 0] = 0
            if not np.array_equal(a, b):
                return float(np.abs(a - b).max())
            continue
        if alpha:
            a, b = _premultiplied(a, full), _premultiplied(b, full)
        else:
            a, b = a.astype(np.float64), b.astype(np.float64)
        total += float(np.abs(a - b).sum())
    return total / max(1, got.size) * (255.0 / full)


def _zero_isobmff_timestamps(data: bytes) -> bytes:
    """Clear creation/modification times written by AVIF sequence encoders."""
    buf = bytearray(data)

    def walk(start: int, end: int) -> None:
        pos = start
        while pos + 8 <= end:
            size, typ = struct.unpack_from(">I4s", buf, pos)
            hdr = 8
            if size == 1:
                (size,) = struct.unpack_from(">Q", buf, pos + 8)
                hdr = 16
            elif size == 0:
                size = end - pos
            if size < hdr or pos + size > end:
                return
            body = pos + hdr
            if typ in (b"moov", b"trak", b"mdia"):
                walk(body, pos + size)
            elif typ in (b"mvhd", b"tkhd", b"mdhd"):
                width = 8 if buf[body] == 1 else 4
                buf[body + 4 : body + 4 + 2 * width] = b"\x00" * (2 * width)
            pos += size

    walk(0, len(buf))
    return bytes(buf)


# --------------------------------------------------------------------------
# The job
# --------------------------------------------------------------------------


class _Job:
    def __init__(self, data: bytes, options: Options, before: Report):
        self.data = data
        self.opts = options
        self.before = before
        self.actions: list[str] = []
        self.warnings: list[str] = []
        self.embed_icc: bytes | None = None
        self.exact = True
        self.prnu_applied = False
        self.lsb_scrub = stego.SCRUB_OFF if not options.scrub_lsb else stego.SCRUB_NOT_APPLIED

    # -- entry point ------------------------------------------------------

    def run(self) -> Result:
        caught: list[str] = []
        with _pillow_guard(caught, self.opts.max_pixels):
            try:
                result = self._run()
            except StripError:
                raise
            except Image.DecompressionBombError:
                raise StripError(
                    f"image dimensions exceed the {self.opts.max_pixels / 1e6:.0f} MP limit (raise it with --max-pixels)"
                ) from None
            except MemoryError:
                raise StripError("not enough memory to process this image") from None
            except Exception as exc:  # noqa: BLE001
                # Decoders raise all kinds of exceptions on corrupt input
                # (IndexError, KeyError, RuntimeError, ...).  Nothing has been
                # written at this point, so report it as a per-file failure.
                raise StripError(f"cannot process image ({type(exc).__name__}: {exc})") from exc
        noisy = ("palette images with transparency", "Truncated File Read")
        for msg in caught:
            if not any(n in msg for n in noisy):
                self.warnings.append(f"decoder: {msg}")
        return result

    def _run(self) -> Result:
        raw = camera_raw_kind(self.data)
        if raw:
            raise StripError(f"camera RAW files ({raw}) are not supported; export the photo to TIFF, PNG or JPEG first")
        im = open_image(self.data)
        src = canonical_format(im.format)
        out = self._output_format(src)
        w, h = im.size
        if w * h > self.opts.max_pixels:
            raise StripError(
                f"image is {w}x{h} ({w * h / 1e6:.0f} MP), above the {self.opts.max_pixels / 1e6:.0f} MP limit "
                "(raise it with --max-pixels)"
            )
        mode = dct_mode(self.opts)
        if mode != "off":
            if src != "JPEG" or out != "JPEG":
                raise StripError("--scrub-dct works on JPEG files and keeps the output a JPEG")
            if self.opts.scrub_prnu:
                raise StripError(
                    "the sensor-fingerprint scrub changes pixels, so it cannot be combined with the DCT scrub "
                    "(--scrub-dct)"
                )
            return self._jpeg_lossless(im)
        if self.opts.jpeg_lossless and out == "JPEG":
            if src != "JPEG":
                raise StripError("--jpeg-lossless requires a JPEG input")
            if self.opts.scrub_prnu:
                # Fail closed: silently skipping would leave the user believing the fingerprint is gone.
                raise StripError(
                    "the sensor-fingerprint scrub changes pixels, so it cannot be combined with bit-exact JPEG "
                    "mode (--jpeg-lossless)"
                )
            return self._jpeg_lossless(im)

        indices, loop = self._select_frames(im, src, out)
        if len(indices) * w * h > 4 * self.opts.max_pixels:
            raise StripError("animation is too large (frames x pixels above 4x --max-pixels)")
        orientation = _orientation(im, self.warnings) if self.opts.auto_orient else 1
        icc = im.info.get("icc_profile") or None
        if icc is None and src == "PNG" and "gamma" in im.info and "srgb" not in im.info:
            icc = self._png_gamma_profile(im)

        kind = None
        if len(indices) == 1:
            if indices[0]:
                im.seek(indices[0])
            kind = codecs.high_bit_depth_kind(im)
            if kind and out not in ("PNG", "TIFF"):
                self.warnings.append(f"16-bit precision reduced to 8 bits ({out} output)")
                self.exact = False
                kind = None
        if kind:
            try:
                arr = codecs.decode_16bit(lambda: open_image(self.data), indices[0], kind)
                frames = [Frame(array=arr)]
                self.actions.append("decoded at full 16-bit precision")
            except (ValueError, OSError) as exc:
                self.warnings.append(f"16-bit decode failed ({exc}); falling back to 8 bits")
                self.exact = False
                kind = None
        if not kind:
            frames = self._decode_frames(im, indices, animated=len(indices) > 1)

        self._apply_clap(frames)
        if orientation != 1:
            self._apply_orientation(frames, orientation)
        elif not self.opts.auto_orient:
            self.warnings.append("orientation tag discarded without rotating (--no-auto-orient)")
        if icc:
            self._color_manage(frames, icc, out)
        self._scrub_transparent(frames)
        if self.opts.scrub_prnu:
            self._scrub_prnu(frames)
        if self.opts.scrub_lsb:  # last, so the final low bits are random whatever the warp did
            self._scrub_lsb(frames)
        data, expected, exact = self._encode(frames, out, loop, src)
        self._verify(data, out, expected, exact)
        removed = list(self.before.findings)
        if self.before.error and self.before.format:
            self.warnings.append(f"input structure: {self.before.error}")
        if src != out:
            self.actions.insert(0, f"converted {src} to {out}")
        return Result(
            data=data,
            input_format=src,
            output_format=out,
            removed=removed,
            actions=self.actions,
            warnings=self.warnings,
            size=frames[0].size,
            frames=len(frames),
            pixels_exact=exact and self.exact,
            input_report=self.before,
            verified_exact=exact,
            prnu_applied=self.prnu_applied,
            lsb_scrub=self.lsb_scrub,
        )

    # -- format / frame selection -----------------------------------------

    def _output_format(self, src: str) -> str:
        if self.opts.format:
            out = self.opts.format.upper()
            out = {"JPG": "JPEG", "TIF": "TIFF", "HEIC": "HEIF"}.get(out, out)
            if out not in OUTPUT_FORMATS:
                raise StripError(f"unsupported output format {self.opts.format}")
        elif src in OUTPUT_FORMATS:
            out = src
        else:
            out = "PNG"
        if out == "HEIF" and not heif_available():
            raise StripError("HEIF output needs the 'pillow-heif' package")
        if out == "AVIF":
            from PIL import features

            if not features.check("avif"):
                raise StripError("this Pillow build cannot write AVIF")
        return out

    def _select_frames(self, im: Image.Image, src: str, out: str):
        n = getattr(im, "n_frames", 1) or 1
        indices = list(range(n))
        if im.format == "MPO" and n > 1:
            indices = [0]
            self.actions.append(f"removed {n - 1} embedded secondary image(s) (MPO)")
        elif src == "TIFF" and n > 1:
            keep = []
            for i in range(n):
                im.seek(i)
                nst = im.tag_v2.get(254, 0) if hasattr(im, "tag_v2") else 0
                if isinstance(nst, tuple):
                    nst = nst[0] if nst else 0
                if not (int(nst) & 0b101):
                    keep.append(i)
            dropped = n - len(keep)
            if dropped and keep:
                self.actions.append(f"removed {dropped} thumbnail/mask page(s)")
            indices = keep or [0]
            im.seek(0)
        elif src == "PNG" and n > 1 and im.info.get("default_image"):
            indices = list(range(1, n))
            self.actions.append("removed the APNG default image (not part of the animation)")
        elif src == "HEIF" and n > 1:
            primary = 0
            for i in range(n):
                im.seek(i)
                if im.info.get("primary"):
                    primary = i
            indices = [primary]
            self.actions.append(f"removed {n - 1} additional top-level image(s)")
        elif src not in ("GIF", "PNG", "WEBP", "AVIF"):
            indices = [0]
        if len(indices) > 1 and out not in ANIMATED_FORMATS:
            self.warnings.append(f"{out} cannot hold several frames; kept only the first of {len(indices)}")
            indices = indices[:1]
        loop = im.info.get("loop") if len(indices) > 1 else None
        return indices, loop

    # -- decoding -----------------------------------------------------------

    def _decode_frames(self, im: Image.Image, indices: list[int], animated: bool) -> list[Frame]:
        frames = []
        removed_entries = 0
        budget = 4 * self.opts.max_pixels
        try:
            for i in indices:
                im.seek(i)
                im.load()
                budget -= im.size[0] * im.size[1]
                if im.size[0] * im.size[1] > self.opts.max_pixels or budget < 0:
                    raise StripError("image or animation exceeds the --max-pixels limit")
                if animated:
                    if im.mode in ("I", "F") or im.mode.startswith("I;16") or codecs.high_bit_depth_kind(im):
                        # Converting to 8-bit RGBA would clip every page, and the verifier would compare against
                        # the clipped copy, so refuse rather than write a quietly damaged picture.
                        raise StripError(
                            f"this file has several frames with more than 8 bits per sample ({im.mode}), which "
                            "cannot be kept exactly; split the frames into separate files first"
                        )
                    rgba = im.convert("RGBA")
                    frames.append(Frame(image=fresh(rgba), duration=im.info.get("duration")))
                else:
                    frame, removed = self._normalize(im)
                    removed_entries += removed
                    frames.append(frame)
        except (StripError, Image.DecompressionBombError):
            raise
        except MemoryError:
            raise StripError("not enough memory to decode this image") from None
        except Exception as exc:  # noqa: BLE001 - corrupt data raises arbitrary errors
            raise StripError(f"cannot decode image: {exc}") from None
        if animated:
            # A frame extending past the logical screen makes Pillow enlarge the
            # canvas; viewers clip to the screen, so anything outside is hidden.
            width, height = frames[0].size
            clipped = 0
            for f in frames:
                if f.image.size != (width, height):
                    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                    canvas.paste(f.image.crop((0, 0, min(width, f.image.width), min(height, f.image.height))), (0, 0))
                    clipped += max(0, f.image.width * f.image.height - width * height)
                    f.image = fresh(canvas)
            if clipped:
                self.actions.append(f"removed {clipped} pixels drawn outside the animation canvas")
            # Use RGB when no frame has any transparency.
            if all(np.asarray(f.image.getchannel("A")).min() == 255 for f in frames):
                for f in frames:
                    f.image = fresh(f.image.convert("RGB"))
        if removed_entries:
            self.actions.append(f"removed {removed_entries} unused or duplicate palette entries")
        return frames

    def _normalize(self, im: Image.Image) -> tuple[Frame, int]:
        mode = im.mode
        t = im.info.get("transparency")
        if mode == "P":
            return _canonical_palette(im)
        if mode in ("I;16", "I;16L", "I;16B", "I;16N"):
            arr = np.asarray(im).astype(np.uint16)
            key = t if isinstance(t, int) else None
            return Frame(image=_from_array(arr, "I;16"), key=key), 0
        if t is not None and mode in ("1", "L", "RGB"):
            rgba = im.convert("RGBA")
            if mode == "RGB":
                return Frame(image=fresh(rgba)), 0
            return Frame(image=fresh(Image.merge("LA", (rgba.getchannel("R"), rgba.getchannel("A"))))), 0
        if mode == "LAB":
            lab = ImageCms.createProfile("LAB")
            img = ImageCms.profileToProfile(im, lab, iccmod.srgb_profile(), outputMode="RGB")
            self.actions.append("converted Lab colour to sRGB")
            return Frame(image=fresh(img)), 0
        conversions = {"PA": "RGBA", "La": "LA", "RGBa": "RGBA", "RGBX": "RGB", "YCbCr": "RGB", "HSV": "RGB"}
        if mode in conversions:
            return Frame(image=fresh(im.convert(conversions[mode]))), 0
        if mode in ("1", "L", "LA", "RGB", "RGBA", "CMYK", "I", "F"):
            return Frame(image=fresh(im)), 0
        return Frame(image=fresh(im.convert("RGBA"))), 0

    # -- geometric fixes ------------------------------------------------------

    def _apply_clap(self, frames: list[Frame]) -> None:
        """Apply an AVIF/HEIF clean-aperture crop the decoder ignored.

        Pixels outside the clean aperture are invisible in conforming viewers;
        publishing them would expose hidden image content.
        """
        clap = self.before.info.get("clap")
        if self.before.format == "AVIF" and not clap and self.before.error and b"\x00\x00\x00\x28clap" in self.data:
            # The inspector stops at the first structural error, before it reads the item properties, so it never
            # saw the crop; the decoder ignores it, and the cropped-out pixels would be published.
            raise StripError(
                "this AVIF has a clean-aperture crop but its structure is damaged "
                f"({self.before.error}), so the crop cannot be applied safely"
            )
        if not clap or self.before.format != "AVIF":
            return  # libheif (HEIC) applies the crop itself; Pillow's AVIF decoder does not
        order = self.before.info.get("transforms", [])
        wn, wd, hn, hd, hon, hod, von, vod = clap
        if not (wd and hd and hod and vod):
            raise StripError("invalid clean-aperture box")
        cw, ch = wn / wd, hn / hd
        width, height = frames[0].size
        if (round(cw), round(ch)) in ((width, height), (height, width)):
            return  # decoder already cropped
        if "clap" in order and any(t in ("irot", "imir") for t in order[: order.index("clap")]):
            raise StripError("unsupported HEIF transform order (rotation before crop)")
        cx = (width - 1) / 2 + hon / hod
        cy = (height - 1) / 2 + von / vod
        left = int(round(cx - (cw - 1) / 2))
        top = int(round(cy - (ch - 1) / 2))
        right, bottom = left + int(round(cw)), top + int(round(ch))
        if left < 0 or top < 0 or right > width or bottom > height or right <= left or bottom <= top:
            raise StripError("clean-aperture crop lies outside the image")
        for f in frames:
            if f.array is not None:
                f.array = f.array[top:bottom, left:right].copy()
            else:
                f.image = fresh(f.image.crop((left, top, right, bottom)))
        self.actions.append(
            f"applied clean-aperture crop, removing {width * height - (right - left) * (bottom - top)} hidden pixels"
        )

    def _apply_orientation(self, frames: list[Frame], orientation: int) -> None:
        for f in frames:
            if f.array is not None:
                f.array = np.ascontiguousarray(_TRANSPOSE_NP[orientation](f.array))
            else:
                f.image = fresh(f.image.transpose(_TRANSPOSE[orientation]))
        self.actions.append(f"applied EXIF orientation ({_ORIENT_TEXT[orientation]})")

    # -- colour -------------------------------------------------------------

    def _png_gamma_profile(self, im: Image.Image) -> bytes | None:
        """Turn PNG gAMA/cHRM chunks into an equivalent profile (browsers honour them)."""
        gamma = float(im.info["gamma"])
        chroma = im.info.get("chromaticity")
        try:
            if iccmod.is_srgb_like_png_gamma(gamma, chroma):
                return None
            gray = im.mode in ("L", "LA", "I;16", "I", "1") or (
                im.mode in ("RGBA",) and codecs.high_bit_depth_kind(im) == "LA"
            )
            return iccmod.synthesize_png_profile(gamma, chroma, gray=gray)
        except iccmod.ProfileError as exc:
            self.warnings.append(f"ignored unusable PNG gamma/chromaticity chunks ({exc})")
            return None

    def _color_manage(self, frames: list[Frame], icc: bytes, out: str) -> None:
        try:
            space = iccmod.colorspace(icc)
            iccmod.load(icc)
        except iccmod.ProfileError as exc:
            self.warnings.append(f"discarded an unusable ICC profile ({exc})")
            return
        name = iccmod.description(icc)
        policy = self.opts.icc
        try:
            equivalent = iccmod.is_srgb_equivalent(icc)
        except iccmod.ProfileError:
            equivalent = False
        if equivalent:
            self.actions.append(f"removed ICC profile '{name}' (sRGB-equivalent, no conversion needed)")
            return
        if policy == "drop":
            self.actions.append(f"removed ICC profile '{name}' without converting")
            self.warnings.append("colours may look different: the image was not in sRGB (--icc drop)")
            return
        if policy == "sanitize":
            if out in ICC_CAPABLE:
                try:
                    self.embed_icc = iccmod.build_sanitized(icc)
                    self.actions.append(f"replaced ICC profile '{name}' with a rebuilt anonymous profile")
                    return
                except iccmod.ProfileError as exc:
                    self.warnings.append(f"cannot sanitise ICC profile ({exc}); converting to sRGB instead")
            else:
                self.warnings.append(f"{out} cannot carry an ICC profile; converting to sRGB instead")
        # Convert every frame first and commit only if all of them succeed.
        converted: list[Frame] = []
        reduced = False
        try:
            for f in frames:
                if f.array is not None:
                    try:
                        converted.append(Frame(array=iccmod.convert_array_16bit(f.array, icc), duration=f.duration))
                    except iccmod.ProfileError:
                        # LUT-based profile: fall back to 8-bit LittleCMS.
                        mode = {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}[f.array.shape[2]]
                        img = _from_array(((f.array.astype(np.uint32) + 128) // 257).astype(np.uint8), mode)
                        converted.append(Frame(image=fresh(iccmod.convert_image_8bit(img, icc)), duration=f.duration))
                        reduced = True
                elif f.image.mode == "P":
                    pal = f.image.getpalette("RGB")
                    entries = [tuple(pal[i : i + 3]) for i in range(0, len(pal), 3)]
                    new = iccmod.convert_palette_8bit(entries, icc)
                    img = f.image.copy()
                    img.putpalette([v for c in new for v in c], "RGB")
                    rebuilt, _ = _canonical_palette(self._with_alpha_info(img, f.alpha))
                    rebuilt.duration = f.duration
                    converted.append(rebuilt)
                elif f.image.mode == "I;16":
                    arr = np.asarray(f.image).astype(np.uint16)[..., None]
                    image = _from_array(iccmod.convert_array_16bit(arr, icc)[..., 0], "I;16")
                    converted.append(Frame(image=image, key=f.key, duration=f.duration))
                elif f.image.mode in ("I", "F", "1"):
                    raise iccmod.ProfileError(f"cannot colour-convert {f.image.mode} images")
                else:
                    converted.append(Frame(image=fresh(iccmod.convert_image_8bit(f.image, icc)), duration=f.duration))
        except iccmod.ProfileError as exc:
            self.warnings.append(f"ICC profile '{name}' removed without conversion ({exc})")
            return
        frames[:] = converted
        if reduced:
            self.exact = False
            self.warnings.append("16-bit image reduced to 8 bits for colour conversion")
        self.actions.append(f"converted colours from ICC profile '{name}' ({space}) to sRGB")

    @staticmethod
    def _with_alpha_info(img: Image.Image, alpha: list[int] | None) -> Image.Image:
        if alpha:
            img.info["transparency"] = bytes(alpha)
        return img

    # -- hidden pixel data -----------------------------------------------------

    def _scrub_transparent(self, frames: list[Frame]) -> None:
        cleared = 0
        for f in frames:
            if f.array is not None:
                if f.array.shape[2] in (2, 4):
                    mask = f.array[..., -1] == 0
                    hidden = mask & (f.array[..., :-1] != 0).any(axis=-1)
                    if hidden.any():
                        cleared += int(hidden.sum())
                        f.array = f.array.copy()
                        f.array[mask] = 0
                continue
            img = f.image
            if img.mode not in ("LA", "RGBA"):
                continue
            arr = np.asarray(img)
            mask = arr[..., -1] == 0
            hidden = mask & (arr[..., :-1] != 0).any(axis=-1)
            if hidden.any():
                cleared += int(hidden.sum())
                arr = arr.copy()
                arr[mask] = 0
                f.image = _from_array(arr, img.mode)
        if cleared:
            self.actions.append(f"cleared hidden colour data under {cleared} fully transparent pixels")

    def _scrub_prnu(self, frames: list[Frame]) -> None:
        """Desynchronise the camera's sensor fingerprint with one random smooth warp.

        Every frame gets the same warp, so animated files do not flicker.  Palette
        images, colour-keyed images and tiny images cannot carry a usable
        fingerprint (and cannot be resampled without changing their colours), so
        they are left alone and the user is told.
        """
        width, height = frames[0].size
        if any(f.size != (width, height) for f in frames):
            self.warnings.append("sensor-fingerprint scrub skipped: the frames differ in size")
            return
        if not prnu.supported(width, height):
            self.warnings.append(
                f"sensor-fingerprint scrub skipped: the image is under {prnu.MIN_SIDE} pixels on a side, "
                "too small to hold a usable fingerprint"
            )
            return
        modes = ("L", "LA", "RGB", "RGBA", "CMYK", "I;16")
        for f in frames:
            if f.key is not None or f.alpha is not None or (f.array is None and f.image.mode not in modes):
                self.warnings.append(
                    "sensor-fingerprint scrub skipped: palette and colour-keyed images carry no camera fingerprint "
                    "and cannot be resampled without changing their colours"
                )
                return
        warp = prnu.plan(width, height, self._prnu_luma(frames[0]), prnu.new_rng())
        for f in frames:
            if f.array is not None:
                f.array = prnu.warp_array16(f.array, warp)
            elif f.image.mode == "I;16":
                arr = np.asarray(f.image).astype(np.uint16)[..., None]
                f.image = _from_array(prnu.warp_array16(arr, warp)[..., 0], "I;16")
            else:
                f.image = prnu.warp_image(f.image, warp)
        self.exact = False
        self.prnu_applied = True
        self.actions.append(
            "disrupted the camera sensor fingerprint (PRNU) with a smooth random warp "
            f"(typically {warp.rms:.1f} px, at most {warp.max_shift:.0f} px)"
        )

    @staticmethod
    def _prnu_luma(frame: Frame) -> Image.Image:
        """An 8-bit luminance image of the frame, used only to find edges."""
        if frame.array is not None:
            arr = frame.array
            colour = arr[..., :-1] if arr.shape[2] in (2, 4) else arr
            return Image.fromarray((colour.mean(axis=2, dtype=np.float32) / 257.0).astype(np.uint8), "L")
        if frame.image.mode == "I;16":
            return Image.fromarray((np.asarray(frame.image).astype(np.uint16) >> 8).astype(np.uint8), "L")
        return frame.image.convert("L")

    def _scrub_lsb(self, frames: list[Frame]) -> None:
        rng = np.random.default_rng(int.from_bytes(os.urandom(16), "big"))
        skipped = set()
        scrubbed = 0
        for f in frames:
            if f.array is None and f.image.mode == "P":
                rgba = _palette_to_rgba(f)
                has_alpha = f.alpha is not None
                f.image = _from_array(rgba if has_alpha else rgba[..., :3], "RGBA" if has_alpha else "RGB")
                f.alpha = None
            if f.array is not None:
                arr = f.array
                f.array = self._randomize_lsb(arr, rng, arr.shape[2] in (2, 4), 0xFFFF)
                scrubbed += 1
                continue
            mode = f.image.mode
            if mode in ("L", "LA", "RGB", "RGBA", "CMYK"):
                arr = np.asarray(f.image)
                if arr.ndim == 2:
                    arr = arr[..., None]
                out = self._randomize_lsb(arr, rng, mode in ("LA", "RGBA"), 0xFF)
                f.image = _from_array(out[..., 0] if mode == "L" else out, mode)
                scrubbed += 1
            elif mode == "I;16":
                arr = np.asarray(f.image).astype(np.uint16)[..., None]
                f.image = _from_array(self._randomize_lsb(arr, rng, False, 0xFFFF)[..., 0], "I;16")
                scrubbed += 1
            else:
                skipped.add(mode)
        if scrubbed:
            self.exact = False
            self.actions.append("randomised the least-significant bit of every colour sample (anti-steganography)")
            self.lsb_scrub = stego.SCRUB_APPLIED if scrubbed == len(frames) else stego.SCRUB_PARTIAL
        if skipped:
            self.warnings.append(f"LSB scrubbing not applicable to {', '.join(sorted(skipped))} images")

    @staticmethod
    def _randomize_lsb(arr: np.ndarray, rng, has_alpha: bool, full: int) -> np.ndarray:
        dtype = arr.dtype
        bits = rng.integers(0, 2, size=arr.shape, dtype=dtype)
        out = (arr & ~dtype.type(1)) | bits
        if has_alpha:
            alpha = arr[..., -1]
            # Keep fully opaque / fully transparent alpha exactly, and keep the
            # (already zeroed) colour of fully transparent pixels.
            keep_alpha = (alpha == 0) | (alpha == full)
            out[..., -1] = np.where(keep_alpha, alpha, out[..., -1])
            out[alpha == 0] = arr[alpha == 0]
        return out

    # -- encoding -----------------------------------------------------------

    def _quality(self, fmt: str) -> int:
        q = self.opts.quality if self.opts.quality is not None else DEFAULT_QUALITY.get(fmt, 95)
        return max(1, min(100, int(q)))

    def _encode(self, frames: list[Frame], out: str, loop, src: str):
        """Returns (file bytes, frames as written for comparison, lossless?)."""
        if frames[0].array is not None:
            return self._encode_16bit(frames[0], out)
        frames = [self._adapt(f, out) for f in frames]
        animated = len(frames) > 1
        durations = [int(round(f.duration or 0)) for f in frames]
        buf = io.BytesIO()
        icc_kw = {"icc_profile": self.embed_icc} if self.embed_icc else {}
        lossless = True
        if out == "PNG":
            if animated:
                kw = {"loop": loop} if loop is not None else {}
                imgs = [f.image for f in frames]
                imgs[0].save(buf, "PNG", save_all=True, append_images=imgs[1:], duration=durations, **kw, **icc_kw)
            else:
                frames = [self._reduce_png(frames[0])]
                data = self._encode_png_static(frames[0])
                return data, frames, True
        elif out == "JPEG":
            lossless = False
            img = frames[0].image
            kw = {"quality": self._quality("JPEG"), "optimize": True}
            if img.mode in ("RGB", "CMYK"):
                kw["subsampling"] = self._subsampling(src)
            img.save(buf, "JPEG", **kw, **icc_kw)
            self.actions.append(f"re-encoded JPEG at quality {kw['quality']}")
        elif out == "WEBP":
            lossless = bool(self.before.info.get("lossless")) if src == "WEBP" else src in ("PNG", "GIF", "BMP", "TIFF")
            kw = (
                {"lossless": True, "quality": 100, "method": 6, "exact": True}
                if lossless
                else {"quality": self._quality("WEBP"), "method": 6, "alpha_quality": 100}
            )
            imgs = [f.image for f in frames]
            if animated:
                kw.update(save_all=True, append_images=imgs[1:], duration=durations)
                if loop is not None:
                    kw["loop"] = loop
            imgs[0].save(buf, "WEBP", **kw, **icc_kw)
            if not lossless:
                self.actions.append(f"re-encoded lossy WebP at quality {kw['quality']}")
        elif out == "GIF":
            arrays = [self._gif_frame(f) for f in frames]
            data, quantized = codecs.encode_gif(arrays, durations, loop)
            if quantized:
                lossless = False
                self.warnings.append(f"{len(quantized)} frame(s) had more than 256 colours and were quantised")
            exp = [
                Frame(image=_from_array(a, "RGBA"), duration=f.duration) for a, f in zip(arrays, frames, strict=True)
            ]
            return data, exp, lossless
        elif out == "TIFF":
            return codecs.encode_tiff([self._tiff_page(f) for f in frames]), frames, True
        elif out == "BMP":
            f = frames[0]
            if _has_alpha(f):
                return codecs.encode_bmp_rgba(_rgba_array(f)), frames, True
            f.image.save(buf, "BMP")
        elif out == "AVIF":
            lossless = False
            imgs = [f.image for f in frames]
            kw = {"quality": self._quality("AVIF"), "subsampling": self._chroma(src)}
            if animated:
                kw.update(save_all=True, append_images=imgs[1:], duration=durations)
            imgs[0].save(buf, "AVIF", **kw, **icc_kw)
            self.actions.append(f"re-encoded AVIF at quality {kw['quality']}")
            data = buf.getvalue()
            return (_zero_isobmff_timestamps(data) if animated else data), frames, lossless
        elif out == "HEIF":
            lossless = False
            kw = {"quality": self._quality("HEIF"), "chroma": int(self._chroma(src).replace(":", ""))}
            frames[0].image.save(buf, "HEIF", **kw, **icc_kw)
            self.actions.append(f"re-encoded HEIF at quality {kw['quality']}")
        return buf.getvalue(), frames, lossless

    def _chroma(self, src: str) -> str:
        """Chroma subsampling of the source, so re-encoding keeps colour resolution."""
        if src in ("AVIF", "HEIF") and self.before.info.get("chroma"):
            return self.before.info["chroma"]
        if src == "JPEG":
            return {0: "4:4:4", 1: "4:2:2", 2: "4:2:0"}[self._subsampling(src)]
        if src == "WEBP" and not self.before.info.get("lossless"):
            return "4:2:0"
        return "4:4:4"

    def _subsampling(self, src: str) -> int:
        if src == "JPEG":
            try:
                from PIL import JpegImagePlugin

                im = open_image(self.data)
                sampling = JpegImagePlugin.get_sampling(im)
                if sampling in (0, 1, 2):
                    return sampling
            except Exception:  # noqa: BLE001
                pass
            return 2
        return 0 if self._quality("JPEG") >= 90 else 2

    def _encode_png_static(self, frame: Frame) -> bytes:
        img = frame.image
        kw = {}
        if img.mode == "P" and frame.alpha:
            # tRNS only needs the non-opaque entries (they are sorted first).
            n = max(i + 1 for i, a in enumerate(frame.alpha) if a < 255)
            kw["transparency"] = bytes(frame.alpha[:n])
        elif frame.key is not None:
            kw["transparency"] = frame.key
        if self.embed_icc:
            kw["icc_profile"] = self.embed_icc
        buf = io.BytesIO()
        pixels = img.size[0] * img.size[1]
        # Higher zlib levels barely help on large photos but cost a lot of time.
        level = 9 if pixels <= 8_000_000 else 6 if pixels <= 24_000_000 else 4
        img.save(buf, "PNG", compress_level=level, **kw)
        best = buf.getvalue()
        if self.opts.optimize:
            best = min([best] + self._png_trials(frame), key=len)
        return best

    def _png_trials(self, frame: Frame) -> list[bytes]:
        img = frame.image
        mode = img.mode
        palette = trns = None
        if mode == "P":
            arr, depth, ctype = np.asarray(img), 8, 3
            n = len(img.getpalette("RGB")) // 3
            depth = 1 if n <= 2 else 2 if n <= 4 else 4 if n <= 16 else 8
            palette = bytes(img.getpalette("RGB"))
            if frame.alpha:
                k = max(i + 1 for i, a in enumerate(frame.alpha) if a < 255)
                trns = bytes(frame.alpha[:k])
        elif mode in ("L", "LA", "RGB", "RGBA"):
            arr, depth, ctype = np.asarray(img), 8, {"L": 0, "LA": 4, "RGB": 2, "RGBA": 6}[mode]
        elif mode == "I;16":
            arr, depth, ctype = np.asarray(img).astype(np.uint16), 16, 0
            if frame.key is not None:
                trns = struct.pack(">H", frame.key)
        else:
            return []
        trials = []
        for strategy in ("adaptive", "none", "paeth", "up"):
            for zs in (zlib.Z_DEFAULT_STRATEGY, zlib.Z_FILTERED):
                trials.append(codecs.encode_png(arr, depth, ctype, palette, trns, self.embed_icc, strategy, 9, zs))
        return trials

    def _reduce_png(self, frame: Frame) -> Frame:
        """Lossless colour-type reduction (fewer channels / palette) for PNG."""
        img = frame.image
        mode = img.mode
        if mode not in ("RGB", "RGBA", "LA"):
            return frame
        arr = np.asarray(img)
        if mode in ("RGBA", "LA") and (arr[..., -1] == 255).all():
            arr = arr[..., :-1]
            mode = mode[:-1]
        if mode in ("RGB", "RGBA") and (arr[..., 0] == arr[..., 1]).all() and (arr[..., 1] == arr[..., 2]).all():
            arr = arr[..., [0, 3]] if mode == "RGBA" else arr[..., 0]
            mode = "LA" if mode == "RGBA" else "L"
        if mode in ("RGB", "RGBA", "LA") and self._few_colours(arr):
            chans = arr.reshape(-1, arr.shape[-1]).astype(np.uint32)
            if mode == "LA":
                keys = (chans[:, 0] << 24) | (chans[:, 0] << 16) | (chans[:, 0] << 8) | chans[:, 1]
            elif mode == "RGB":
                keys = (chans[:, 0] << 24) | (chans[:, 1] << 16) | (chans[:, 2] << 8) | 255
            else:
                keys = (chans[:, 0] << 24) | (chans[:, 1] << 16) | (chans[:, 2] << 8) | chans[:, 3]
            uniq, inverse = np.unique(keys, return_inverse=True)
            if len(uniq) <= 256:
                # Canonical palette: non-opaque entries first (short tRNS), then by colour.
                order = np.lexsort((uniq, (uniq & 0xFF) == 255))
                rank = np.empty_like(order)
                rank[order] = np.arange(len(order))
                index = rank[inverse.reshape(-1)].astype(np.uint8).reshape(arr.shape[:2])
                canon = uniq[order]
                rgb = [int(v) for c in canon for v in ((c >> 24) & 255, (c >> 16) & 255, (c >> 8) & 255)]
                p = Image.frombytes("P", (arr.shape[1], arr.shape[0]), index.tobytes())
                p.putpalette(rgb, "RGB")
                alphas = [int(c & 0xFF) for c in canon]
                return Frame(image=p, alpha=alphas if min(alphas) < 255 else None)
        if mode != img.mode:
            return Frame(image=_from_array(arr, mode))
        return frame

    def _tiff_page(self, frame: Frame) -> codecs.TiffPage:
        img = frame.image
        mode = img.mode
        icc = self.embed_icc
        if mode == "1":
            return codecs.TiffPage(np.asarray(img).astype(np.uint8), 1, bilevel=True, icc=icc)
        if mode == "P":
            pal = np.array(img.getpalette("RGB"), np.uint8).reshape(-1, 3)
            return codecs.TiffPage(np.asarray(img), 3, palette=pal, icc=icc)
        if mode == "I;16":
            return codecs.TiffPage(np.asarray(img).astype(np.uint16), 1, icc=icc)
        if mode == "I":
            return codecs.TiffPage(np.asarray(img).astype(np.int32), 1, icc=icc)
        if mode == "F":
            return codecs.TiffPage(np.asarray(img).astype(np.float32), 1, icc=icc)
        photometric, extra = {"L": (1, 0), "LA": (1, 1), "RGB": (2, 0), "RGBA": (2, 1), "CMYK": (5, 0)}[mode]
        return codecs.TiffPage(np.asarray(img), photometric, extra, icc=icc)

    @staticmethod
    def _few_colours(arr: np.ndarray) -> bool:
        """Cheap pre-check before an exact palette conversion (photos fail fast)."""
        flat = arr.reshape(-1, arr.shape[-1])
        step = max(1, len(flat) // 200_000)
        sample = flat[::step]
        packed = np.zeros(len(sample), np.uint32)
        for c in range(sample.shape[1]):
            packed = (packed << 8) | sample[:, c]
        return len(np.unique(packed)) <= 256

    def _gif_frame(self, frame: Frame) -> np.ndarray:
        rgba = _rgba_array(frame).copy()
        partial = (rgba[..., 3] > 0) & (rgba[..., 3] < 255)
        if partial.any():
            self.warnings.append("GIF supports only on/off transparency; semi-transparent pixels were thresholded")
            self.exact = False
            rgba[..., 3] = np.where(rgba[..., 3] >= 128, 255, 0)
        rgba[rgba[..., 3] == 0] = 0
        return rgba

    def _adapt(self, frame: Frame, out: str) -> Frame:
        """Convert a frame into a mode the output format can store."""
        img = frame.image
        mode = img.mode
        has_alpha = _has_alpha(frame)

        def to(mode_: str, why: str | None = None) -> Frame:
            if why:
                self.warnings.append(why)
                self.exact = False
            if mode == "P":
                rgba = _palette_to_rgba(frame)
                base = Image.frombytes("RGBA", img.size, rgba.tobytes())
            else:
                base = img
            if mode_ in ("RGB", "L") and base.mode in ("RGBA", "LA") and out in ("JPEG",):
                flat = _flatten_white(np.asarray(base.convert("RGBA")))
                conv = _from_array(flat, "RGB")
                return Frame(image=fresh(conv.convert(mode_)), duration=frame.duration)
            return Frame(image=fresh(base.convert(mode_)), duration=frame.duration)

        if mode in ("I", "F") and out != "TIFF":
            if mode == "I":
                arr = np.asarray(img)
                if arr.min() >= 0 and arr.max() <= 0xFFFF and out == "PNG":
                    return Frame(image=_from_array(arr.astype(np.uint16), "I;16"))
            raise StripError(f"{mode}-mode (32-bit) images can only be written as TIFF; use --format tiff")
        if out == "PNG":
            if mode == "CMYK":
                return to("RGB", "CMYK image converted to RGB for PNG output")
            return frame
        if out == "JPEG":
            if mode in ("L", "RGB", "CMYK"):
                return frame
            if has_alpha:
                return to("RGB", "transparency flattened onto white (JPEG has no alpha channel)")
            if mode == "I;16":
                return to("L", "16-bit grayscale reduced to 8 bits for JPEG")
            if mode == "P":
                return to("RGB")
            return to("L" if mode == "1" else "RGB")
        if out in ("WEBP", "AVIF", "HEIF"):
            if mode in ("RGB", "RGBA"):
                return frame
            if mode == "CMYK":
                return to("RGB", f"CMYK image converted to RGB for {out} output")
            if mode == "I;16":
                return to("RGB", f"16-bit grayscale reduced to 8 bits for {out}")
            return to("RGBA" if has_alpha else "RGB")
        if out == "GIF":
            return frame
        if out == "TIFF":
            if mode == "P" and frame.alpha:
                return to("RGBA")
            return frame
        if out == "BMP":
            if mode == "CMYK":
                return to("RGB", "CMYK image converted to RGB for BMP output")
            if mode == "I;16":
                return to("L", "16-bit grayscale reduced to 8 bits for BMP")
            if mode == "P" and frame.alpha:
                return to("RGBA")
            return frame
        return frame

    def _encode_16bit(self, frame: Frame, out: str):
        arr = frame.array
        if out == "PNG":
            ctype = {1: 0, 2: 4, 3: 2, 4: 6}[arr.shape[2]]
            data = codecs.encode_png(arr, 16, ctype, icc=self.embed_icc)
            if self.opts.optimize:
                trials = [
                    codecs.encode_png(arr, 16, ctype, icc=self.embed_icc, strategy=s) for s in ("none", "paeth", "up")
                ]
                data = min([data] + trials, key=len)
        else:
            data = codecs.encode_tiff16(arr, icc=self.embed_icc)
        return data, [frame], True

    # -- verification -----------------------------------------------------------

    def _verify(self, data: bytes, out: str, expected: list[Frame], exact: bool) -> None:
        report = inspect_bytes(data)
        if report.format != out:
            raise VerificationError(f"output is not a valid {out} file ({report.format})")
        if report.error:
            raise VerificationError(f"output failed structural verification: {report.error}")
        clap = report.info.get("clap")
        encoder_padding = (
            out == "HEIF"
            and clap is not None
            and clap[1]
            and clap[3]
            and (round(clap[0] / clap[1]), round(clap[2] / clap[3])) == expected[0].size
        )  # libheif pads to the codec block size; the padding holds no source data

        def allowed(f) -> bool:
            if self.embed_icc and f.label == "ICC color profile" and report.info.get("icc") == self.embed_icc:
                return True
            return encoder_padding and f.label.startswith("Pixels outside the visible crop")

        leftovers = [f for f in report.findings if not allowed(f)]
        if leftovers:
            raise VerificationError("output still contains: " + "; ".join(map(str, leftovers)))
        try:
            check = open_image(data)
        except StripError as exc:
            raise VerificationError(f"output cannot be decoded: {exc}") from None
        n = getattr(check, "n_frames", 1) or 1
        if n != len(expected):
            raise VerificationError(f"output has {n} frame(s), expected {len(expected)}")
        tolerance = 0.0 if exact else 4.0 + (100 - self._quality(out)) * 0.4
        for i, frame in enumerate(expected):
            if i:
                check.seek(i)
            kind = codecs.high_bit_depth_kind(check) if frame.array is not None else None
            check.load()
            if check.size != frame.size:
                raise VerificationError(f"frame {i}: output size {check.size} differs from {frame.size}")
            if frame.array is not None:
                if frame.array.shape[2] == 1 or kind is None:
                    got = np.asarray(check).astype(np.uint16)
                    got = got[..., None] if got.ndim == 2 else got
                else:
                    got = codecs.decode_16bit(lambda: open_image(data), i, kind)
                want, alpha = frame.array, frame.array.shape[2] in (2, 4)
            elif check.mode == frame.image.mode and frame.image.mode != "P":
                got, want = np.asarray(check), np.asarray(frame.image)
                alpha = frame.image.mode in ("LA", "RGBA")
            else:
                got, want, alpha = np.asarray(check.convert("RGBA")), _rgba_array(frame), True
            if got.shape != want.shape:
                raise VerificationError(f"frame {i}: decoded layout {got.shape} differs from {want.shape}")
            error = _mean_error(got, want, alpha, exact=tolerance == 0.0)
            if tolerance == 0.0 and error:
                raise VerificationError(f"frame {i}: pixels changed during lossless re-encoding")
            if tolerance == 0.0:
                self._verify_nothing_hidden_in_pixels(i, check, got, alpha, frame.array is not None)
            if error > tolerance:
                raise VerificationError(f"frame {i}: re-encoded image deviates too much (mean error {error:.1f})")

    @staticmethod
    def _verify_nothing_hidden_in_pixels(i: int, check: Image.Image, got: np.ndarray, alpha: bool, wide: bool) -> None:
        """Lossless output must not keep data the viewer never sees, whatever container it sits in.

        The container inspector cannot see these (they are values inside the pixel data or colour table), and the
        pixel comparison above ignores them (colour under a fully transparent pixel is invisible).
        """
        if check.mode == "P":
            unused = unused_palette_entries(check)
            if unused:
                raise VerificationError(f"frame {i}: {unused} unused palette entries hold colour data")
            clear = transparent_palette_colour(check)
            if clear:
                raise VerificationError(f"frame {i}: {clear} fully transparent palette entries hold colour data")
        elif alpha and (wide or check.mode in ("LA", "RGBA")):
            hidden = int(((got[..., -1] == 0) & (got[..., :-1] != 0).any(axis=-1)).sum())
            if hidden:
                raise VerificationError(f"frame {i}: colour data hidden under {hidden} fully transparent pixels")

    # -- lossless JPEG ----------------------------------------------------------

    def _dct_body(self, mode: str):
        """Canonical tables, frame header and scan for this JPEG (scrubbed first in "full" mode).

        Returns (body, frame as written, ScrubStats or None).  Fails closed: a JPEG that cannot be represented
        exactly (arithmetic coding, 12 bits, ...) is refused rather than copied with its coefficients intact.
        """
        from . import dct, jpeg

        try:
            frame = dct.read(self.data, self.opts.max_pixels)
            stats = None
            if mode == "full":
                stats = dct.scrub(frame, np.random.default_rng(int.from_bytes(os.urandom(16), "big")))
                floor = self.opts.dct_min_psnr
                if floor and stats.psnr is not None and stats.psnr < floor:
                    raise StripError(
                        f"scrubbing the DCT coefficients would cost about {stats.psnr} dB of picture quality "
                        f"(limit {floor:g} dB, see --dct-min-psnr); re-encoding without --scrub-dct is gentler "
                        "but does not guarantee that DCT steganography is gone"
                    )
            return dct.write(frame), frame, stats
        except jpeg.JpegError as exc:
            raise StripError(
                f"cannot rebuild this JPEG from its DCT coefficients ({exc}); nothing was written because "
                "--scrub-dct would otherwise have kept the hidden-data channel it was asked to remove"
            ) from None

    def _check_scan_slack(self) -> None:
        """Bit-for-bit mode copies the scan data, so refuse a file whose scans hold bytes no decoder reads."""
        from . import dct, jpeg

        try:
            frame = dct.read(self.data, self.opts.max_pixels)
        except jpeg.JpegError:
            self.warnings.append(
                "this JPEG's compressed data could not be checked for unused bytes (arithmetic coding, 12 bits or "
                "another form imgstrip's own decoder does not read)"
            )
            return
        if frame.slack:
            raise StripError(
                f"the compressed data holds {frame.slack} byte(s) that no decoder uses, which --jpeg-lossless would "
                "copy; use --jpeg-canonical (same picture, rebuilt) or the default mode instead"
            )

    def _jpeg_lossless(self, im: Image.Image) -> Result:
        from . import dct, jpeg

        mode = dct_mode(self.opts)
        orientation = _orientation(im, self.warnings)
        icc = im.info.get("icc_profile") or None
        icc_out = None
        if icc:
            name = iccmod.description(icc)
            try:
                equivalent = iccmod.is_srgb_equivalent(icc)
            except iccmod.ProfileError:
                equivalent = False
            if equivalent:
                self.actions.append(f"removed ICC profile '{name}' (sRGB-equivalent)")
            elif self.opts.icc == "drop":
                self.actions.append(f"removed ICC profile '{name}' without converting")
                self.warnings.append("colours may look different: the image was not in sRGB (--icc drop)")
            else:
                try:
                    icc_out = iccmod.build_sanitized(icc)
                    self.actions.append(f"replaced ICC profile '{name}' with a rebuilt anonymous profile")
                except iccmod.ProfileError as exc:
                    self.warnings.append(
                        f"ICC profile '{name}' removed ({exc}); colours may shift. "
                        "Re-encoding mode (without --jpeg-lossless) converts colours exactly."
                    )
        if orientation != 1 and not self.opts.auto_orient:
            self.warnings.append("orientation tag discarded without rotating (--no-auto-orient)")
            orientation = 1
        canonical = frame = stats = None
        if mode != "off":
            canonical, frame, stats = self._dct_body(mode)
        data, dropped = jpeg.rebuild(self.data, orientation, icc_out, canonical)
        if dropped:
            self.actions.append(f"removed {dropped} embedded image(s) after the main image")
        if orientation != 1:
            self.actions.append(
                f"kept orientation as a minimal 1-tag EXIF block ({_ORIENT_TEXT[orientation]}); "
                "lossless mode cannot rotate pixels"
            )
        if mode == "off":
            self.actions.append("kept the compressed image data bit-for-bit (no re-encoding)")
            self._check_scan_slack()
            self.warnings.append(
                "the compressed picture data was kept as it is, so data hidden in it survives (JPEG steganography "
                "stored in the coefficients, or in how the file is encoded); --jpeg-canonical removes the "
                "encoding-level hiding places and --scrub-dct destroys coefficient payloads"
            )
        elif mode == "canonical":
            self.actions.append(
                "rebuilt the compressed data from its DCT coefficients in one canonical form (pixels unchanged)"
            )
        else:
            self.actions.append(
                "randomised the parity of every quantised DCT coefficient and removed the smallest ones (-2 to 1) "
                "(anti-steganography; "
                + (f"estimated {stats.psnr} dB against the input" if stats.psnr is not None else "no change")
                + ")"
            )
        if self.opts.scrub_lsb:
            self.warnings.append(
                f"--scrub-lsb has no effect with {'--scrub-dct' if mode != 'off' else '--jpeg-lossless'}"
            )
        # Verify: structure must be clean apart from what we deliberately wrote.
        report = inspect_bytes(data)
        if report.format != "JPEG" or report.error:
            raise VerificationError(f"output failed structural verification: {report.error}")
        allowed_exif = jpeg.orientation_exif(orientation)[6:] if orientation != 1 else None
        leftovers = []
        for f in report.findings:
            if f.label == "ICC color profile" and icc_out and report.info.get("icc") == icc_out:
                continue
            if f.label == "EXIF" and allowed_exif and report.info.get("exif") == allowed_exif:
                continue
            leftovers.append(f)
        if leftovers:
            raise VerificationError("output still contains: " + "; ".join(map(str, leftovers)))
        original = open_image(self.data)
        original.load()
        rebuilt = open_image(data)
        rebuilt.load()
        if original.size != rebuilt.size or original.mode != rebuilt.mode:
            raise VerificationError("lossless JPEG rebuild changed the image geometry")
        measured = None
        if mode == "full":
            measured = self._verify_dct_scrub(data, original, rebuilt, frame, stats)
        else:
            if not np.array_equal(np.asarray(original), np.asarray(rebuilt)):
                raise VerificationError("lossless JPEG rebuild changed the decoded pixels")
            if mode == "canonical" and not dct.visible_equal(dct.read(data), frame):
                raise VerificationError("canonical JPEG rebuild changed the DCT coefficients")
        return Result(
            data=data,
            input_format="JPEG",
            output_format="JPEG",
            removed=list(self.before.findings),
            actions=self.actions,
            warnings=self.warnings,
            size=rebuilt.size,
            frames=1,
            pixels_exact=mode != "full",
            input_report=self.before,
            verified_exact=mode != "full",
            lsb_scrub=self.lsb_scrub,
            dct_scrub={"off": stego.DCT_OFF, "canonical": stego.DCT_CANONICAL, "full": stego.DCT_SCRUBBED}[mode],
            dct_psnr=measured,
        )

    def _verify_dct_scrub(self, data, original, rebuilt, frame, stats) -> float | None:
        """Check a scrubbed file against what the scrub promises, and against how it actually decodes."""
        from . import dct

        written = dct.read(data)
        if not dct.visible_equal(written, frame):
            raise VerificationError("the written file does not hold the coefficients that were scrubbed")
        problem = dct.check_scrub(written, stats.before)
        if problem:
            raise VerificationError(f"DCT scrub check failed: {problem}")
        measured = dct.psnr(np.asarray(original), np.asarray(rebuilt))
        floor = self.opts.dct_min_psnr
        if measured is not None:
            # The estimate averages the error over whole blocks, so it is loose for pictures smaller than a block and
            # for edge blocks; the coefficients were already compared exactly above.  This only catches an output
            # that decodes to something else entirely (a bug that would be symmetric in our reader and writer).
            if stats.psnr is not None and measured < stats.psnr - 10.0:
                raise VerificationError(
                    f"the scrubbed picture differs from the input by {measured} dB, not the {stats.psnr} dB expected"
                )
            if floor and measured < floor:
                raise StripError(
                    f"scrubbing the DCT coefficients would cost {measured} dB of picture quality "
                    f"(limit {floor:g} dB, see --dct-min-psnr)"
                )
        return measured


def strip_image(data: bytes, options: Options | None = None) -> Result:
    """Remove all metadata and hidden data from an image file held in memory."""
    if not data:
        raise StripError("file is empty")
    before = inspect_bytes(data)
    return _Job(data, options or Options(), before).run()


def _canonical_array(frame: Frame) -> np.ndarray:
    """A comparable view of one decoded frame: what a viewer would show, nothing hidden.

    Palette, grey and RGB images all become RGBA (so a PNG that was re-written as a palette
    image still compares equal); colour stored under fully transparent pixels is zeroed, as
    the engine deliberately drops it.  32-bit and float frames keep their native values.
    """
    if frame.array is not None:
        return frame.array
    mode = frame.image.mode
    if mode in ("I;16", "I", "F"):
        return np.asarray(frame.image)
    rgba = _rgba_array(frame).copy()
    rgba[rgba[..., 3] == 0] = 0
    return rgba


def canonical_frames(data: bytes, max_pixels: int = 300_000_000) -> list[np.ndarray]:
    """Decode every frame the engine would keep from ``data``, as comparable arrays.

    No orientation, colour management or other normalisation is applied, so the arrays of
    an original and of its cleaned copy are equal exactly when the cleaned file shows the
    very same pixel values (nothing is rotated, re-encoded, converted or scrubbed).
    """
    caught: list[str] = []
    with _pillow_guard(caught, max_pixels):
        try:
            job = _Job(data, Options(max_pixels=max_pixels), inspect_bytes(data))
            im = open_image(data)
            src = canonical_format(im.format)
            indices, _loop = job._select_frames(im, src, src)
            if len(indices) == 1:
                if indices[0]:
                    im.seek(indices[0])
                kind = codecs.high_bit_depth_kind(im)
                if kind:
                    try:
                        return [codecs.decode_16bit(lambda: open_image(data), indices[0], kind)]
                    except (ValueError, OSError):
                        pass  # the same fallback the engine takes
            frames = job._decode_frames(im, indices, animated=len(indices) > 1)
            job._apply_clap(frames)
            return [_canonical_array(f) for f in frames]
        except StripError:
            raise
        except Image.DecompressionBombError:
            raise StripError("image is too large to compare") from None
        except MemoryError:
            raise StripError("not enough memory to compare these images") from None
        except Exception as exc:  # noqa: BLE001 - decoders raise anything on corrupt data
            raise StripError(f"cannot decode image ({type(exc).__name__}: {exc})") from exc
