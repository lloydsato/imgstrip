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
from dataclasses import dataclass, field, replace

import numpy as np
from PIL import Image, ImageCms, UnidentifiedImageError

from . import codecs
from . import icc as iccmod
from . import prnu, refdecode, stego
from .inspect import (
    Finding,
    Report,
    canonical_form_problems,
    inspect_bytes,
    transparent_palette_colour,
    unused_palette_entries,
)

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
# The longest side the encoders accept, measured against Pillow's own (an image past it makes the encoder fail).
OUTPUT_MAX_SIDE = {"JPEG": 65500, "WEBP": 16383, "GIF": 65535, "AVIF": 32768}
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


def dct_mode(options: Options) -> str:
    """The normalised --scrub-dct setting: "off", "canonical" or "full"."""
    value = options.scrub_dct
    if value is True:
        return "full"
    if value in (False, None, "off"):
        return "off"
    if value in ("canonical", "full"):
        return value
    raise StripError(f"unknown DCT scrub mode {value!r} (use off, canonical or full)")


# The command-line switch behind each DCT mode, so messages name the one the user gave (--jpeg-canonical sets
# "canonical", --scrub-dct sets "full").
_DCT_FLAG = {"canonical": "--jpeg-canonical", "full": "--scrub-dct"}
_DCT_NAME = {"canonical": "canonical JPEG rebuilding", "full": "the DCT scrub"}


class StripError(Exception):
    """The file could not be cleaned (the reason is user-facing)."""


class VerificationError(StripError):
    """The produced file failed the post-write safety checks."""


# The largest picture accepted unless --max-pixels says otherwise.  At about 16 bytes per pixel at the peak (more for
# 16-bit pictures; docs/REFERENCE.md has the measured table) 150 megapixels already need over 2 GB; 1.2.4 allowed 300.
DEFAULT_MAX_PIXELS = 150_000_000


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
    max_pixels: int = DEFAULT_MAX_PIXELS
    # Also decode default-mode JPEG output with imgstrip's own, independent decoder (pure Python, about 0.3 s per
    # megapixel).  PNG and BMP output always get an independent reader; see refdecode.
    independent_jpeg: bool = False
    # Decode the input in a separate, freshly forked process that hands back only pixels and a few typed values (see
    # sandbox.py).  None follows the process-wide setting (on unless sandbox.set_default(False) or --no-sandbox).
    sandbox: bool | None = None


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
    # Which checks the output passed: the structural walk, Pillow's decode, and any independent reader (refdecode).
    verified_by: list[str] = field(default_factory=list)
    # What the output carries over from the input besides its pixel values: the choices that are part of the mode's
    # equivalence relation (docs/REFERENCE.md, "What the output keeps").  Only values taken from the input appear.
    kept: dict = field(default_factory=dict)


@dataclass
class Decoded:
    """What the decode stage hands to the encoder.  Nothing else crosses from the input to the output: when the
    decode stage runs in the sandbox, these fields are all that comes back, as raw pixels or coefficients plus a
    short list of typed values (sandbox.py validates every one)."""

    kind: str  # "pixels" (decoded and normalised frames) or "jpeg" (the JPEG modes that keep compressed data)
    report: Report  # the input's inspection: what will be reported as removed
    src: str  # the input format
    out: str  # the output format
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    decoder_warnings: list[str] = field(default_factory=list)  # what the decoders themselves warned about
    exact: bool = True  # nothing so far changed a pixel value beyond the documented normalisation
    # kind "pixels"
    frames: list[Frame] = field(default_factory=list)
    loop: int | None = None
    embed_icc: bytes | None = None  # the rebuilt anonymous profile (--icc sanitize)
    source_subsampling: int | None = None  # a JPEG input's chroma subsampling (0, 1 or 2), kept on re-encoding
    # kind "jpeg"
    orientation: int = 1
    icc_out: bytes | None = None
    original: np.ndarray | None = None  # the input's decoded pixels, for the comparison
    original_mode: str = ""
    rebuilt: bytes | None = None  # --jpeg-lossless: the input's compressed data inside a rebuilt file
    coefficients: object = None  # the input's quantised DCT coefficients (a dct.Frame), when imgstrip can read them
    had_jfif: bool = False
    adobe_transform: int | None = None
    dropped: int = 0  # embedded images found after the main image


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
def _pillow_guard(collected: list[str], max_pixels: int = DEFAULT_MAX_PIXELS):
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
    # Pillow reads the array's buffer directly; the array is only copied when it is not already contiguous.
    arr = np.ascontiguousarray(arr, dtype="<u2" if mode == "I;16" else np.uint8)
    h, w = arr.shape[:2]
    return Image.frombytes(mode, (w, h), arr)


def fresh(img: Image.Image) -> Image.Image:
    """A new image holding only the pixel data (and palette) of ``img``."""
    out = Image.new(img.mode, img.size)
    out.paste(img, (0, 0))  # copies the pixel values straight across, with no intermediate bytes copy
    if img.mode in ("P", "PA"):
        out.putpalette(img.getpalette("RGB") or [0, 0, 0], "RGB")
    return out


# Whole-image comparisons and checks walk the picture in strips of rows of about this many bytes, so they need a
# few megabytes beside the images themselves instead of several full-size copies.
_STRIP_BYTES = 1 << 22


def _strips(height: int, row_bytes: int, align: int = 1):
    """(top, bottom) row ranges of about ``_STRIP_BYTES`` each, every one starting at a multiple of ``align``."""
    step = max(1, _STRIP_BYTES // max(1, row_bytes))
    step = max(align, step - step % align)
    for top in range(0, height, step):
        yield top, min(height, top + step)


# --------------------------------------------------------------------------
# Stage 2 for lossy formats: where the error is, not only how large it is on average
# --------------------------------------------------------------------------

# A mean over the whole picture lets a change to a small area through (finding F2 of the review: a black rectangle
# over 2 % of a JPEG, or 1 % of its pixels replaced, passed).  Lossy encoders spread their error over every block,
# so the worst 8 x 8 block is the sharper test: a re-encode moves no block far, a local change moves a few blocks a
# lot.  The difference is split the way the lossy codecs see it: luma (BT.601 Y), which every codec stores at full
# resolution; chroma, which most subsample, so it gets a looser bound; and alpha, which WebP and AVIF store apart.
_BLOCK = 8
_LUMA = np.array([0.299, 0.587, 0.114], np.float32)
_CB = np.array([-0.168736, -0.331264, 0.5], np.float32)
_CR = np.array([0.5, -0.418688, -0.081312], np.float32)


@dataclass
class BlockError:
    """The worst 8 x 8 block of a decoded lossy frame against the frame that was written: RMS differences in 8-bit
    levels for luma, chroma and alpha, with the top-left corner of the block that set each one."""

    luma: float = 0.0
    chroma: float = 0.0
    alpha: float = 0.0
    # A subsampled four-component (CMYK) JPEG keeps its first channel at full resolution and the other three at a
    # lower one, like luma and chroma.
    split_cmyk: bool = False
    luma_at: tuple[int, int] = (0, 0)
    chroma_at: tuple[int, int] = (0, 0)
    alpha_at: tuple[int, int] = (0, 0)

    def add(self, got: np.ndarray, want: np.ndarray, alpha: bool, top: int) -> None:
        """Account for rows [top, top + len(got)) of the frame (``top`` a multiple of the block size)."""
        for start in range(0, got.shape[0], 8 * _BLOCK):  # a few block rows at a time keeps the float copies small
            self._add(got[start : start + 8 * _BLOCK], want[start : start + 8 * _BLOCK], alpha, top + start)

    def _add(self, got: np.ndarray, want: np.ndarray, alpha: bool, top: int) -> None:
        if got.ndim == 2:
            got, want = got[..., None], want[..., None]
        full = _full_scale(got, want)
        if alpha:
            got, want = _premultiplied(got, full), _premultiplied(want, full)
        diff = (got.astype(np.float32) - want.astype(np.float32)) * np.float32(255.0 / full)
        colour = diff[..., :-1] if alpha else diff
        if colour.shape[-1] == 3:
            parts = {"luma": colour @ _LUMA, "chroma": np.hypot(colour @ _CB, colour @ _CR)}
        elif colour.shape[-1] == 4 and self.split_cmyk:
            parts = {"luma": colour[..., 0], "chroma": np.abs(colour[..., 1:]).max(axis=-1)}
        else:  # grey, or CMYK at full resolution (no colour transform): every channel is stored like luma
            parts = {"luma": np.abs(colour).max(axis=-1)}
        if alpha:
            parts["alpha"] = diff[..., -1]
        for name, values in parts.items():
            rms = _block_rms(values)
            k = int(np.argmax(rms))
            if rms.flat[k] > getattr(self, name):
                setattr(self, name, float(rms.flat[k]))
                by, bx = divmod(k, rms.shape[1])
                setattr(self, f"{name}_at", (bx * _BLOCK, top + by * _BLOCK))


@dataclass(frozen=True)
class BlockLimits:
    luma: float
    chroma: float
    alpha: float


# The worst 8 x 8 block (RMS of luma, chroma and alpha, in 8-bit levels) that a re-encode left in an output the mean
# test accepted, measured by scripts/calibrate_lossy.py at each anchor quality, per output format and chroma layout
# (subsampled or not).  Its corpus is meant to hold the worst cases: natural photographs, grey textures, photographs
# with Gaussian noise up to sigma 60, uniform white noise, saturated one-pixel colour graphics, gradients, CMYK, and
# alpha gradients.  Re-run the script after changing an encoder setting or a codec library; it fails if a measured
# block exceeds the bound below.
_BLOCK_MEASURED: dict[tuple[str, bool], dict[int, tuple[float, float, float]]] = {
    ("AVIF", False): {1: (57.2, 66.6, 0.0), 5: (56.6, 72.0, 0.0), 10: (47.3, 69.1, 0.0), 25: (40.8, 52.3, 0.0),
                      50: (16.6, 25.7, 0.0), 75: (6.2, 19.9, 0.0), 85: (4.1, 13.1, 0.0), 90: (2.6, 8.6, 0.0),
                      95: (2.0, 3.6, 0.0), 100: (0.7, 1.7, 0.0)},
    ("AVIF", True): {1: (60.8, 54.8, 12.6), 5: (56.3, 54.0, 9.4), 10: (57.2, 42.0, 8.2), 25: (49.0, 32.6, 4.0),
                     50: (21.3, 22.4, 2.5), 75: (5.9, 17.5, 1.4), 85: (4.3, 17.0, 1.1), 90: (3.7, 17.1, 0.9),
                     95: (3.7, 17.0, 0.8), 100: (2.3, 6.6, 0.0)},
    ("GIF", False): {1: (20.7, 25.2, 0.0)},  # colour quantisation: the quality setting plays no part
    ("HEIF", False): {1: (63.8, 98.6, 0.0), 5: (68.2, 97.9, 0.0), 10: (55.8, 95.4, 0.0), 25: (41.2, 63.5, 0.0),
                      50: (6.8, 19.4, 0.0), 75: (1.7, 4.2, 0.0), 85: (1.1, 2.6, 0.0), 90: (0.9, 1.9, 0.0),
                      95: (0.8, 1.5, 0.0), 100: (0.8, 1.5, 0.0)},
    ("HEIF", True): {1: (64.6, 45.5, 0.0), 5: (57.9, 36.8, 0.0), 10: (54.5, 37.1, 0.0), 25: (25.6, 26.0, 0.0),
                     50: (6.1, 23.4, 0.0), 75: (3.0, 23.1, 0.0), 85: (2.9, 23.1, 0.0), 90: (2.8, 23.1, 0.0),
                     95: (2.7, 23.1, 0.0), 100: (2.7, 23.1, 0.0)},
    ("JPEG", False): {1: (103.9, 74.6, 0.0), 5: (94.7, 74.4, 0.0), 10: (87.9, 72.6, 0.0), 25: (59.2, 55.2, 0.0),
                      50: (31.6, 33.9, 0.0), 75: (16.1, 19.0, 0.0), 85: (10.0, 12.7, 0.0), 90: (6.7, 8.5, 0.0),
                      95: (3.5, 4.4, 0.0), 100: (0.7, 0.9, 0.0)},
    ("JPEG", True): {1: (59.4, 111.0, 0.0), 5: (57.4, 109.1, 0.0), 10: (53.9, 106.6, 0.0), 25: (40.1, 106.8, 0.0),
                     50: (23.5, 105.3, 0.0), 75: (12.3, 28.8, 0.0), 85: (7.5, 26.2, 0.0), 90: (5.6, 18.0, 0.0),
                     95: (3.9, 17.2, 0.0), 100: (3.6, 16.9, 0.0)},
    ("WEBP", True): {1: (39.4, 40.3, 0.0), 5: (32.5, 31.8, 0.0), 10: (26.6, 30.9, 0.0), 25: (18.8, 23.2, 0.0),
                     50: (14.3, 18.9, 0.0), 75: (9.0, 16.8, 0.0), 85: (5.5, 17.1, 0.0), 90: (4.8, 16.8, 0.0),
                     95: (4.1, 17.0, 0.0), 100: (3.9, 16.8, 0.0)},
}  # fmt: skip
# The bound is this multiple of the worst block measured at the nearest anchor at or below the quality, plus a floor
# (luma, chroma, alpha) that covers rounding and decoder differences on near-lossless settings.
_BLOCK_MARGIN = 2.0
_BLOCK_FLOOR = (3.0, 6.0, 3.0)


def block_tolerance(fmt: str, quality: int, chroma_subsampled: bool) -> BlockLimits:
    """How far (RMS, 8-bit levels) a re-encode may move one 8 x 8 block of a lossy output."""
    table = _BLOCK_MEASURED.get((fmt, chroma_subsampled)) or _BLOCK_MEASURED.get((fmt, not chroma_subsampled))
    if table is None:
        inf = float("inf")
        return BlockLimits(inf, inf, inf)
    # The nearest anchor at or below the quality (a lower quality moves blocks further), and every anchor above it,
    # so that a measurement that happened to come out larger at a higher quality still counts.
    nearest = max([q for q in table if q <= quality] or [min(table)])
    worst = [max(table[q][k] for q in table if q >= nearest) for k in range(3)]
    return BlockLimits(*(_BLOCK_MARGIN * w + f for w, f in zip(worst, _BLOCK_FLOOR, strict=True)))


def _cmyk_subsampling(im: Image.Image) -> int:
    """Pillow's subsampling setting (0, 1 or 2) that keeps a four-component JPEG's own layout, 0 when it has none.

    Pillow can only write the first component at a higher resolution than the other three; any other layout is
    kept at full resolution, which loses nothing.
    """
    layer = getattr(im, "layer", None) or []
    factors = [(h, v) for _id, h, v, _q in layer]
    if len(factors) == 4 and all(f == (1, 1) for f in factors[1:]):
        return {(1, 1): 0, (2, 1): 1, (2, 2): 2}.get(factors[0], 0)
    return 0


def _expected_rows16(frame: Frame, top: int, bottom: int) -> np.ndarray:
    """Rows [top, bottom) of a frame as written, as uint16 RGBA with 8-bit samples scaled by 257 and colour under
    fully transparent pixels zeroed: the form refdecode compares with."""
    from .oracles import expand

    if frame.array is not None:
        return expand(frame.array[top:bottom], 16)
    img = frame.image
    part = img.crop((0, top, img.width, bottom))
    if img.mode == "I;16":
        grey = np.asarray(part).astype(np.uint16)
        out = expand(grey[..., None], 16)
        if frame.key is not None:
            out[grey == frame.key] = 0
        return out
    if img.mode == "1":
        return expand(np.asarray(part).astype(np.uint8)[..., None] * 255, 8)
    return expand(_rgba_array(replace(frame, image=part)), 8)


def _block_rms(values: np.ndarray) -> np.ndarray:
    """Root mean square of a (rows, cols) array over 8 x 8 blocks; edge blocks average the pixels they hold."""
    h, w = values.shape
    squared = values.astype(np.float64) ** 2
    rows, cols = np.arange(0, h, _BLOCK), np.arange(0, w, _BLOCK)
    sums = np.add.reduceat(np.add.reduceat(squared, rows, axis=0), cols, axis=1)
    counts = np.outer(np.diff(np.append(rows, h)), np.diff(np.append(cols, w)))
    return np.sqrt(sums / counts)


def _rows(img: Image.Image, top: int, bottom: int) -> np.ndarray:
    """Rows [top, bottom) of ``img`` as an array (only those rows are copied)."""
    return np.asarray(img.crop((0, top, img.width, bottom)))


def _hidden_colour(px: np.ndarray) -> int:
    """How many pixels hold colour under full transparency (the last channel is alpha)."""
    return int(np.count_nonzero((px[..., -1] == 0) & (px[..., :-1] != 0).any(axis=-1)))


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


def _full_scale(got: np.ndarray, want: np.ndarray) -> int:
    return 65535 if got.dtype == np.uint16 or want.dtype == np.uint16 else 255


def _mean_error(got: np.ndarray, want: np.ndarray, alpha: bool, exact: bool) -> float:
    """Mean absolute difference, computed in row blocks to bound memory.

    With ``exact`` it returns as soon as any difference is found.
    """
    if got.ndim == 2:
        got, want = got[..., None], want[..., None]
    full = _full_scale(got, want)
    if exact and not alpha and got.dtype == want.dtype:
        return 0.0 if np.array_equal(got, want) else 1.0
    if exact:
        rows = max(1, (1 << 22) // max(1, got.shape[1] * got.shape[2]))
        for top in range(0, got.shape[0], rows):
            # Bit-exact, except colour under fully transparent pixels (zeroed on purpose).
            a, b = got[top : top + rows].astype(np.int64), want[top : top + rows].astype(np.int64)
            a[a[..., -1] == 0] = 0
            b[b[..., -1] == 0] = 0
            if not np.array_equal(a, b):
                return float(np.abs(a - b).max())
        return 0.0
    return _error_total(got, want, alpha, full) / max(1, got.size) * (255.0 / full)


def _error_total(got: np.ndarray, want: np.ndarray, alpha: bool, full: int) -> float:
    """Sum of absolute differences (of the premultiplied colour with ``alpha``), in row blocks to bound memory.

    For integer pictures the sum is a whole number held exactly, so the totals of the strips of a picture add up to
    the total of the whole picture, bit for bit.
    """
    if got.ndim == 2:
        got, want = got[..., None], want[..., None]
    rows = max(1, (1 << 22) // max(1, got.shape[1] * got.shape[2]))
    total = 0.0
    for top in range(0, got.shape[0], rows):
        a, b = got[top : top + rows], want[top : top + rows]
        if alpha:
            a, b = _premultiplied(a, full), _premultiplied(b, full)
        elif a.dtype.kind == "u" and b.dtype.kind == "u" and max(a.itemsize, b.itemsize) <= 2:
            # Integer arithmetic is exact here (a block sums to far less than 2**53), so this equals the float
            # computation below bit for bit while touching a quarter of the memory.
            wide = np.int16 if max(a.itemsize, b.itemsize) == 1 else np.int32
            diff = np.subtract(a, b, dtype=wide)
            total += float(np.abs(diff, out=diff).sum(dtype=np.int64))
            continue
        else:
            a, b = a.astype(np.float64), b.astype(np.float64)
        total += float(np.abs(a - b).sum())
    return total


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
    def __init__(self, data: bytes, options: Options, before: Report | None, sandboxed: bool = False):
        self.data = data  # the input; only the decode stage reads it
        self.opts = options
        self.before = before
        self.sandboxed = sandboxed  # decode what still holds the input's compressed data in the sandbox too
        self.source_subsampling: int | None = None
        self.actions: list[str] = []
        self.warnings: list[str] = []
        self.embed_icc: bytes | None = None
        self.exact = True
        self.prnu_applied = False
        self.lsb_scrub = stego.SCRUB_OFF if not options.scrub_lsb else stego.SCRUB_NOT_APPLIED
        self.chroma_subsampled = False  # the lossy output stores colour at a lower resolution than luma
        self.kept: dict = {}  # see Result.kept
        self.verified_by = ["structure", "Pillow"]

    # -- entry points -----------------------------------------------------
    #
    # decode() reads the input and returns a Decoded; finish() scrubs, encodes and verifies.  strip_image runs the
    # first in the sandbox (sandbox.py) and the second here; with the sandbox off, both run here.

    def _guarded(self, step):
        """Run ``step`` under Pillow's guard; return (its result, the decoders' own warnings)."""
        caught: list[str] = []
        with _pillow_guard(caught, self.opts.max_pixels):
            try:
                result = step()
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
        return result, [msg for msg in caught if not any(n in msg for n in noisy)]

    def decode(self) -> Decoded:
        """The decode stage: everything that reads the input."""
        decoded, caught = self._guarded(self._decode)
        decoded.decoder_warnings = caught
        return decoded

    def finish(self, decoded: Decoded) -> Result:
        """The encode stage: from what the decode stage handed over to a verified file."""
        result, caught = self._guarded(lambda: self._finish(decoded))
        for msg in dict.fromkeys(decoded.decoder_warnings + caught):
            self.warnings.append(f"decoder: {msg}")
        return result

    def run(self) -> Result:
        """Both stages in this process."""
        return self.finish(self.decode())

    def _decode(self) -> Decoded:
        if self.before is None:
            self.before = inspect_bytes(self.data)
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
                raise StripError(f"{_DCT_FLAG[mode]} works on JPEG files and keeps the output a JPEG")
            if self.opts.scrub_prnu:
                raise StripError(
                    f"the sensor-fingerprint scrub changes pixels, so it cannot be combined with {_DCT_NAME[mode]} "
                    f"({_DCT_FLAG[mode]})"
                )
            return self._decode_jpeg(im, mode)
        if self.opts.jpeg_lossless and out == "JPEG":
            if src != "JPEG":
                raise StripError("--jpeg-lossless requires a JPEG input")
            if self.opts.scrub_prnu:
                # Fail closed: silently skipping would leave the user believing the fingerprint is gone.
                raise StripError(
                    "the sensor-fingerprint scrub changes pixels, so it cannot be combined with bit-exact JPEG "
                    "mode (--jpeg-lossless)"
                )
            return self._decode_jpeg(im, "off")

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
        # Every frame is now a copy of its own, so the decoded source (and any decoder state behind it) can go.
        im.close()
        del im

        self._apply_clap(frames)
        if orientation != 1:
            self._apply_orientation(frames, orientation)
        elif not self.opts.auto_orient:
            self.warnings.append("orientation tag discarded without rotating (--no-auto-orient)")
        if icc:
            self._color_manage(frames, icc, out)
        self._scrub_transparent(frames)
        return Decoded(
            kind="pixels",
            report=self.before,
            src=src,
            out=out,
            actions=self.actions,
            warnings=self.warnings,
            exact=self.exact,
            frames=frames,
            loop=loop,
            embed_icc=self.embed_icc,
            source_subsampling=self._source_subsampling() if src == "JPEG" else None,
        )

    def _adopt(self, decoded: Decoded) -> None:
        """Take over what the decode stage found (it may have run in another process)."""
        self.before = decoded.report
        self.actions, self.warnings = decoded.actions, decoded.warnings
        self.exact = decoded.exact
        self.embed_icc = decoded.embed_icc
        self.source_subsampling = decoded.source_subsampling

    def _finish(self, decoded: Decoded) -> Result:
        # With the decode stage in the sandbox this process may never have opened an image, and the HEIF writer is
        # only registered by the plugin set-up that opening does.
        _register_plugins()
        self._adopt(decoded)
        if decoded.kind == "jpeg":
            return self._finish_jpeg(decoded)
        src, out, loop = decoded.src, decoded.out, decoded.loop
        frames, decoded.frames = decoded.frames, []  # this stage owns the pixels now
        self._scrub_transparent(frames)  # again, because the decode stage may have run in another process
        if self.opts.scrub_prnu:
            self._scrub_prnu(frames)
        if self.opts.scrub_lsb:  # last, so the final low bits are random whatever the warp did
            self._scrub_lsb(frames)
        data, expected, exact = self._encode(frames, out, loop, src)
        kept = self._kept(out, expected)
        # The encoder returns the frames as written (new ones whenever it converted them), so the ones it was given
        # need not stay in memory while the output is verified.
        size, count = frames[0].size, len(frames)
        del frames
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
            size=size,
            frames=count,
            pixels_exact=exact and self.exact,
            input_report=self.before,
            verified_exact=exact,
            prnu_applied=self.prnu_applied,
            lsb_scrub=self.lsb_scrub,
            verified_by=self.verified_by,
            kept=kept,
        )

    def _kept(self, out: str, written: list[Frame]) -> dict:
        kept = {"format": out} if not self.opts.format else {}
        first = written[0]
        mode = first.image.mode if first.image is not None else None
        if mode == "CMYK":
            kept["colour"] = "CMYK"
        if first.array is not None or mode == "I;16":
            kept["depth"] = 16
        if self.embed_icc:
            kept["profile"] = "rebuilt from the input's"
        return {**kept, **self.kept}

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
        if loop is not None:  # one representation: a whole number in the range every animated format can store
            loop = max(0, min(0xFFFF, int(loop)))
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
                turned = f.image.transpose(_TRANSPOSE[orientation])
                f.image = None  # let the unrotated pixels go before the rotated ones are copied
                f.image = fresh(turned)
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
            if img.mode not in ("LA", "RGBA") or img.getextrema()[-1][0] > 0:
                continue  # no fully transparent pixel at all
            for top, bottom in _strips(img.height, img.width * 4):
                arr = _rows(img, top, bottom)
                mask = arr[..., -1] == 0
                hidden = int(np.count_nonzero(mask & (arr[..., :-1] != 0).any(axis=-1)))
                if hidden:
                    cleared += hidden
                    arr = arr.copy()
                    arr[mask] = 0
                    img.paste(_from_array(arr, img.mode), (0, top))  # the frame owns its image: fix it in place
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
        # Resampling alpha can round a faint pixel to fully transparent while it keeps colour borrowed from its
        # neighbours; that colour would be hidden under it, so clear it as the first pass does.
        self._scrub_transparent(frames)
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
            if mode in ("L", "LA", "RGB", "RGBA", "CMYK", "I;16"):
                img = f.image
                full = 0xFFFF if mode == "I;16" else 0xFF
                for top, bottom in _strips(img.height, img.width * 4):
                    arr = _rows(img, top, bottom)
                    arr = arr[..., None] if arr.ndim == 2 else arr
                    out = self._randomize_lsb(arr, rng, mode in ("LA", "RGBA"), full)
                    # The frame owns its image, so each strip is written back in place.
                    img.paste(_from_array(out[..., 0] if out.shape[2] == 1 else out, mode), (0, top))
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
            # A barely visible pixel (alpha 1) must not be randomised to 0: that would hide its colour under a
            # fully transparent pixel, which the verifier refuses (and which is a hiding place of its own).
            keep_alpha |= (alpha != 0) & (out[..., -1] == 0)
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
        frames = self._canonical_model(frames, out)
        frames = [self._adapt(f, out) for f in frames]
        animated = len(frames) > 1
        # One representation of timing: whole, non-negative milliseconds (GIF then stores tens of them).
        durations = [max(0, int(round(f.duration or 0))) for f in frames]
        if animated and out != "TIFF":  # pages of a TIFF have no timing
            self.kept.update(frames=len(frames), durations=durations, loop=loop)
        elif len(frames) > 1:
            self.kept["frames"] = len(frames)
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
                self.chroma_subsampled = kw["subsampling"] != 0
                if src == "JPEG":
                    self.kept["chroma"] = {0: "4:4:4", 1: "4:2:2", 2: "4:2:0"}[kw["subsampling"]]
            try:
                img.save(buf, "JPEG", **kw, **icc_kw)
                self.actions.append(f"re-encoded JPEG at quality {kw['quality']}")
            except OSError:
                # To optimise the Huffman tables Pillow gives libjpeg an output buffer of one or two bytes per pixel,
                # and libjpeg cannot suspend there ("broken data stream"), so a very noisy picture does not fit.
                # Standard tables need no such buffer.
                buf = io.BytesIO()
                img.save(buf, "JPEG", **{**kw, "optimize": False}, **icc_kw)
                self.actions.append(f"re-encoded JPEG at quality {kw['quality']} (standard Huffman tables)")
        elif out == "WEBP":
            lossless = bool(self.before.info.get("lossless")) if src == "WEBP" else src in ("PNG", "GIF", "BMP", "TIFF")
            self.kept["lossless"] = lossless
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
                self.chroma_subsampled = True  # lossy WebP is always 4:2:0
                self.actions.append(f"re-encoded lossy WebP at quality {kw['quality']}")
        elif out == "GIF":
            arrays = [self._gif_frame(f) for f in frames]
            data, quantized = codecs.encode_gif(arrays, durations, loop)
            if quantized:
                lossless = False
                self.warnings.append(f"{len(quantized)} frame(s) had more than 256 colours and were quantised")
            exp = []
            for k, f in enumerate(frames):
                exp.append(Frame(image=_from_array(arrays[k], "RGBA"), duration=f.duration))
                arrays[k] = None  # one copy of each frame at a time
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
            self.chroma_subsampled = kw["subsampling"] != "4:4:4"
            if src in ("AVIF", "HEIF", "JPEG", "WEBP"):
                self.kept["chroma"] = kw["subsampling"]
            if animated:
                kw.update(save_all=True, append_images=imgs[1:], duration=durations)
            imgs[0].save(buf, "AVIF", **kw, **icc_kw)
            self.actions.append(f"re-encoded AVIF at quality {kw['quality']}")
            data = buf.getvalue()
            return (_zero_isobmff_timestamps(data) if animated else data), frames, lossless
        elif out == "HEIF":
            lossless = False
            kw = {"quality": self._quality("HEIF"), "chroma": int(self._chroma(src).replace(":", ""))}
            self.chroma_subsampled = kw["chroma"] != 444
            if src in ("AVIF", "HEIF", "JPEG", "WEBP"):
                self.kept["chroma"] = self._chroma(src)
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
            return self.source_subsampling if self.source_subsampling is not None else 2
        return 0 if self._quality("JPEG") >= 90 else 2

    def _source_subsampling(self) -> int:
        """The chroma subsampling of a JPEG input (decode stage: it reads the input's frame header)."""
        try:
            from PIL import JpegImagePlugin

            im = open_image(self.data)
            if getattr(im, "layers", None) == 4:
                # Pillow reports no sampling for four components (and would then default to 4:2:0, which keeps
                # M, Y and K at a quarter of their resolution): read the factors from the frame header instead.
                return _cmyk_subsampling(im)
            sampling = JpegImagePlugin.get_sampling(im)
            if sampling in (0, 1, 2):
                return sampling
        except Exception:  # noqa: BLE001
            pass
        return 2

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
        if img.mode not in ("RGB", "RGBA", "LA"):
            return frame
        # Each test reads the picture strip by strip and stops at the first strip that rules it out, so a photo
        # costs almost nothing here.
        reduced = img
        if reduced.mode in ("RGBA", "LA") and reduced.getextrema()[-1] == (255, 255):
            reduced = reduced.convert(reduced.mode[:-1])  # drops the alpha channel, nothing else
        if reduced.mode in ("RGB", "RGBA") and self._grey(reduced):
            red = reduced.getchannel("R")
            reduced = red if reduced.mode == "RGB" else Image.merge("LA", (red, reduced.getchannel("A")))
        mode = reduced.mode
        if mode in ("RGB", "RGBA", "LA") and self._few_colours(reduced):
            arr = np.asarray(reduced)
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
            return Frame(image=fresh(reduced))
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
    def _grey(img: Image.Image) -> bool:
        """Whether every pixel of an RGB(A) image has R == G == B."""
        for top, bottom in _strips(img.height, img.width * 4):
            a = _rows(img, top, bottom)
            if not ((a[..., 0] == a[..., 1]).all() and (a[..., 1] == a[..., 2]).all()):
                return False
        return True

    @staticmethod
    def _few_colours(img: Image.Image) -> bool:
        """Cheap pre-check before an exact palette conversion (photos fail fast).

        Looks at every ``step``-th pixel in raster order (about 200,000 of them), strip by strip.
        """
        width = img.width
        step = max(1, width * img.height // 200_000)
        seen = np.zeros(0, np.uint32)
        for top, bottom in _strips(img.height, width * 4):
            flat = _rows(img, top, bottom).reshape(-1, len(img.mode))
            sample = flat[(-top * width) % step :: step]
            packed = np.zeros(len(sample), np.uint32)
            for c in range(sample.shape[1]):
                packed = (packed << 8) | sample[:, c]
            seen = np.union1d(seen, packed)
            if len(seen) > 256:
                return False
        return True

    def _gif_frame(self, frame: Frame) -> np.ndarray:
        rgba = _rgba_array(frame).copy()
        partial = (rgba[..., 3] > 0) & (rgba[..., 3] < 255)
        if partial.any():
            self.warnings.append("GIF supports only on/off transparency; semi-transparent pixels were thresholded")
            self.exact = False
            rgba[..., 3] = np.where(rgba[..., 3] >= 128, 255, 0)
        rgba[rgba[..., 3] == 0] = 0
        return rgba

    def _canonical_model(self, frames: list[Frame], out: str) -> list[Frame]:
        """Store the pixels in the colour model they need, not the one the input happened to use: otherwise an opaque
        alpha channel, grey stored as colour, or a few colours stored without a palette would each carry a choice of
        the input's into the output (review item 6).  PNG gets the same reduction when it is written."""
        if out in ("TIFF", "BMP"):
            # One page only: the pages of a multi-page TIFF must share one model (an animation's frames already do).
            if len(frames) == 1 and frames[0].image is not None:
                return [self._reduce_png(frames[0])]
            return frames
        if len(frames) == 1 and frames[0].image is not None and out in ("JPEG", "WEBP", "AVIF", "HEIF"):
            img = frames[0].image
            if img.mode in ("RGBA", "LA") and img.getextrema()[-1] == (255, 255):
                img = img.convert(img.mode[:-1])
            if out == "JPEG" and img.mode == "RGB" and self._grey(img):
                img = img.getchannel("R")
            if img is not frames[0].image:
                return [Frame(image=fresh(img), duration=frames[0].duration)]
        return frames

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
            if mode == "P" and (img.getpalette("RGB") or [])[:6] == [0, 0, 0, 255, 255, 255] and len(img.getpalette("RGB")) == 6:
                # An 8-bit BMP whose two-entry palette is black, white is read by Pillow as a 1-bit image although its
                # bytes are 8-bit indices, so the picture would be mis-decoded (and the verifier would rightly refuse
                # it).  Write it as a 1-bit BMP instead: the same pixels, and a layout every reader agrees on.
                return to("1")
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
        departures = canonical_form_problems(data, out)
        if departures:
            raise VerificationError("output departs from the writer's layout: " + "; ".join(departures))
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
            alpha, pairs = self._decoded_against_written(data, i, check, frame, kind)
            total, samples, hidden, full, top = 0.0, 0, 0, 255, 0
            blocks = BlockError(split_cmyk=self.chroma_subsampled)
            rows = frame.size[1]
            for got, want in pairs:
                if got.shape != want.shape:
                    raise VerificationError(
                        f"frame {i}: decoded layout {(rows,) + got.shape[1:]} differs from {(rows,) + want.shape[1:]}"
                    )
                if tolerance == 0.0:
                    if _mean_error(got, want, alpha, exact=True):
                        raise VerificationError(f"frame {i}: pixels changed during lossless re-encoding")
                    if alpha:
                        hidden += _hidden_colour(got)
                    continue
                full = _full_scale(got, want)
                total += _error_total(got, want, alpha, full)
                samples += got.size
                blocks.add(got, want, alpha, top)
                top += got.shape[0]
            if tolerance == 0.0:
                self._verify_nothing_hidden_in_pixels(i, check, hidden, alpha, frame.array is not None)
                continue
            error = total / max(1, samples) * (255.0 / full)
            if error > tolerance:
                raise VerificationError(f"frame {i}: re-encoded image deviates too much (mean error {error:.1f})")
            self._check_blocks(i, blocks, out)
        check.close()  # Pillow's decode is no longer needed while the independent reader works
        del check
        if out in ("PNG", "BMP") or (out == "JPEG" and self.opts.independent_jpeg):
            self._independent_check(data, out, expected, tolerance)

    def _independent_check(self, data: bytes, out: str, expected: list[Frame], tolerance: float) -> None:
        """Stage 2 again, with a reader that shares no code with Pillow or with imgstrip's encoders (refdecode)."""

        def rows(k: int, top: int, bottom: int) -> np.ndarray:
            return _expected_rows16(expected[k], top, bottom)

        try:
            if out == "PNG":
                name = refdecode.check_png(data, len(expected), rows)
            elif out == "BMP":
                name = refdecode.check_bmp(data, rows)
            else:
                name = self._independent_jpeg(data, expected[0], tolerance)
        except refdecode.Disagreement as exc:
            raise VerificationError(f"an independent reader disagrees with the output: {exc}") from None
        self.verified_by.append(name)

    def _independent_jpeg(self, data: bytes, frame: Frame, tolerance: float) -> str:
        """Decode a default-mode JPEG with imgstrip's own decoder: it must show the written pixels within the same
        bounds as Pillow's decode, and agree with Pillow's decode within what two decoders may differ by."""
        mine = refdecode.decode_jpeg(data, self.opts.max_pixels)
        want = np.asarray(frame.image)
        theirs = np.asarray(open_image(data))
        mine, want, theirs = (a if a.ndim == 3 else a[..., None] for a in (mine, want, theirs))
        if mine.shape != want.shape or theirs.shape != want.shape:
            raise refdecode.Disagreement(f"decoded layout {mine.shape} differs from {want.shape}")
        written = block_tolerance("JPEG", self._quality("JPEG"), self.chroma_subsampled)
        for other, label, mean_limit, limits in (
            (want, "the written pixels", tolerance, written),
            (theirs, "Pillow's decode", refdecode.JPEG_DECODERS.mean, refdecode.JPEG_DECODERS),
        ):
            blocks = BlockError(split_cmyk=self.chroma_subsampled)
            total = 0.0
            for top, bottom in _strips(want.shape[0], want.shape[1] * 4, _BLOCK):
                blocks.add(mine[top:bottom], other[top:bottom], False, top)
                total += _error_total(mine[top:bottom], other[top:bottom], False, 255)
            mean = total / max(1, mine.size)
            worst = max((("luma", blocks.luma, limits.luma), ("chroma", blocks.chroma, limits.chroma)), key=lambda r: r[1] - r[2])
            if mean > mean_limit or worst[1] > worst[2]:
                raise refdecode.Disagreement(
                    f"imgstrip's JPEG decoder and {label} differ by {mean:.2f} levels on average and {worst[1]:.1f} "
                    f"in the worst block ({worst[0]}; allowed {mean_limit:.2f} and {worst[2]:.1f})"
                )
        return "imgstrip JPEG decoder"

    def _check_blocks(self, i: int, blocks: BlockError, out: str) -> None:
        """Refuse a lossy output whose worst 8 x 8 block is further from what was written than a re-encode moves one."""
        limits = block_tolerance(out, self._quality(out), self.chroma_subsampled)
        for name in ("luma", "chroma", "alpha"):
            value, limit = getattr(blocks, name), getattr(limits, name)
            if value > limit:
                x, y = getattr(blocks, f"{name}_at")
                raise VerificationError(
                    f"frame {i}: the 8x8 block at x={x}, y={y} differs from the written pixels by {value:.1f} levels "
                    f"(RMS, {name}; a re-encode at this quality moves no block more than {limit:.1f}), so part of the "
                    "picture changed"
                )

    @staticmethod
    def _verify_nothing_hidden_in_pixels(i: int, check: Image.Image, hidden: int, alpha: bool, wide: bool) -> None:
        """Lossless output must not keep data the viewer never sees, whatever container it sits in.

        ``hidden`` is how many of the decoded pixels hold colour under full transparency (``_hidden_colour``).  The
        container inspector cannot see these (they are values inside the pixel data or colour table), and the pixel
        comparison above ignores them (colour under a fully transparent pixel is invisible).
        """
        if check.mode == "P":
            unused = unused_palette_entries(check)
            if unused:
                raise VerificationError(f"frame {i}: {unused} unused palette entries hold colour data")
            clear = transparent_palette_colour(check)
            if clear:
                raise VerificationError(f"frame {i}: {clear} fully transparent palette entries hold colour data")
        elif alpha and (wide or check.mode in ("LA", "RGBA")) and hidden:
            raise VerificationError(f"frame {i}: colour data hidden under {hidden} fully transparent pixels")

    @staticmethod
    def _decoded_against_written(data: bytes, i: int, check: Image.Image, frame: Frame, kind: str | None):
        """(alpha, pairs): frame ``i`` of the output as decoded beside the frame that was written, as pairs of
        arrays strip by strip, so neither picture is ever copied whole."""
        if frame.array is not None:
            want = frame.array
            alpha = want.shape[2] in (2, 4)
            if want.shape[2] != 1 and kind is not None:
                return alpha, [(codecs.decode_16bit(lambda: open_image(data), i, kind), want)]

            def wide():
                for top, bottom in _strips(check.height, want[0].nbytes, _BLOCK):
                    got = _rows(check, top, bottom).astype(np.uint16)
                    yield (got[..., None] if got.ndim == 2 else got), want[top:bottom]

            return alpha, wide()
        img = frame.image
        strips = _strips(img.height, img.width * 4, _BLOCK)
        if check.mode == img.mode and img.mode != "P":
            return img.mode in ("LA", "RGBA"), ((_rows(check, t, b), _rows(img, t, b)) for t, b in strips)

        def rgba():
            for top, bottom in strips:
                box = (0, top, img.width, bottom)
                yield np.asarray(check.crop(box).convert("RGBA")), _rgba_array(replace(frame, image=img.crop(box)))

        return True, rgba()

    # -- the JPEG modes that keep compressed data --------------------------------

    def _check_scan_slack(self):
        """Bit-for-bit mode copies the scan data, so refuse a file whose scans hold bytes no decoder reads.

        Returns the input's coefficients (None when imgstrip's own decoder cannot read the file).
        """
        from . import dct, jpeg

        try:
            frame = dct.read(self.data, self.opts.max_pixels)
        except jpeg.JpegError:
            self.warnings.append(
                "this JPEG's compressed data could not be checked for unused bytes (arithmetic coding, 12 bits or "
                "another form imgstrip's own decoder does not read)"
            )
            return None
        if frame.slack:
            raise StripError(
                f"the compressed data holds {frame.slack} byte(s) that no decoder uses, which --jpeg-lossless would "
                "copy; use --jpeg-canonical (same picture, rebuilt) or the default mode instead"
            )
        return frame

    def _decode_jpeg(self, im: Image.Image, mode: str) -> Decoded:
        """Decode stage of --jpeg-lossless (mode "off"), --jpeg-canonical and --scrub-dct: the input's coefficients,
        its decoded pixels, and (bit-exact mode only) the rebuilt file that keeps its compressed data."""
        from . import dct, jpeg

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
        decoded = Decoded(
            kind="jpeg",
            report=self.before,
            src="JPEG",
            out="JPEG",
            actions=self.actions,
            warnings=self.warnings,
            orientation=orientation,
            icc_out=icc_out,
        )
        if mode == "off":
            decoded.rebuilt, decoded.dropped = jpeg.rebuild(self.data, orientation, icc_out, None)
            decoded.coefficients = self._check_scan_slack()
        else:
            try:
                decoded.coefficients = dct.read(self.data, self.opts.max_pixels)
            except jpeg.JpegError as exc:
                raise StripError(
                    f"cannot rebuild this JPEG from its DCT coefficients ({exc}); nothing was written because "
                    "--scrub-dct would otherwise have kept the hidden-data channel it was asked to remove"
                ) from None
            decoded.had_jfif, decoded.adobe_transform, decoded.dropped = jpeg.wrapper_facts(self.data)
        im.load()
        decoded.original, decoded.original_mode = np.asarray(im), im.mode
        return decoded

    def _decode_output(self, data: bytes) -> tuple[np.ndarray, str]:
        """Decoded pixels of an output that still holds the input's compressed data (bit-exact mode): in the sandbox
        when the input was decoded there, since that data came from the input."""
        if self.sandboxed:
            from . import sandbox

            return sandbox.pixels(data, self.opts.max_pixels)
        im = open_image(data)
        im.load()
        return np.asarray(im), im.mode

    def _finish_jpeg(self, decoded: Decoded) -> Result:
        from . import dct, jpeg

        mode = dct_mode(self.opts)
        orientation, icc_out, frame = decoded.orientation, decoded.icc_out, decoded.coefficients
        stats = None
        if mode == "off":
            data = decoded.rebuilt
        else:
            try:
                if mode == "full":
                    stats = dct.scrub(frame, np.random.default_rng(int.from_bytes(os.urandom(16), "big")))
                    floor = self.opts.dct_min_psnr
                    if floor and stats.psnr is not None and stats.psnr < floor:
                        raise StripError(
                            f"scrubbing the DCT coefficients would cost about {stats.psnr} dB of picture quality "
                            f"(limit {floor:g} dB, see --dct-min-psnr); re-encoding without --scrub-dct is gentler "
                            "but does not guarantee that DCT steganography is gone"
                        )
                body = dct.write(frame)
            except jpeg.JpegError as exc:
                raise StripError(
                    f"cannot rebuild this JPEG from its DCT coefficients ({exc}); nothing was written because "
                    "--scrub-dct would otherwise have kept the hidden-data channel it was asked to remove"
                ) from None
            data = jpeg.assemble(body, orientation, icc_out, decoded.had_jfif, decoded.adobe_transform)
        if decoded.dropped:
            self.actions.append(f"removed {decoded.dropped} embedded image(s) after the main image")
        if orientation != 1:
            self.actions.append(
                f"kept orientation as a minimal 1-tag EXIF block ({_ORIENT_TEXT[orientation]}); "
                "lossless mode cannot rotate pixels"
            )
        if mode == "off":
            self.actions.append("kept the compressed image data bit-for-bit (no re-encoding)")
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
            self.warnings.append(f"--scrub-lsb has no effect with {_DCT_FLAG.get(mode, '--jpeg-lossless')}")
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
        original = decoded.original
        decoded.original = None
        rebuilt, rebuilt_mode = self._decode_output(data) if mode == "off" else self._decode_here(data)
        if original.shape != rebuilt.shape or decoded.original_mode != rebuilt_mode:
            raise VerificationError("lossless JPEG rebuild changed the image geometry")
        measured = None
        if mode == "full":
            measured = self._verify_dct_scrub(data, original, rebuilt, frame, stats)
        else:
            row_bytes = int(np.prod(original.shape[1:])) * original.itemsize
            strips = _strips(original.shape[0], row_bytes)
            if not all(np.array_equal(original[t:b], rebuilt[t:b]) for t, b in strips):
                raise VerificationError("lossless JPEG rebuild changed the decoded pixels")
            if frame is not None:
                # A second, independent decoder: imgstrip's own entropy decoder must read the output's coefficients
                # as the input's (lossless) or as the ones that were written (canonical).
                written = dct.read(data)
                if not dct.visible_equal(written, frame):
                    raise VerificationError(
                        "canonical JPEG rebuild changed the DCT coefficients"
                        if mode == "canonical"
                        else "imgstrip's coefficient reader sees other coefficients in the output than in the input"
                    )
                if mode == "canonical":
                    self._verify_canonical_form(data, written)
                self.verified_by.append("imgstrip JPEG coefficient reader")
        return Result(
            data=data,
            input_format="JPEG",
            output_format="JPEG",
            removed=list(self.before.findings),
            actions=self.actions,
            warnings=self.warnings,
            size=(rebuilt.shape[1], rebuilt.shape[0]),
            frames=1,
            pixels_exact=mode != "full",
            input_report=self.before,
            verified_exact=mode != "full",
            lsb_scrub=self.lsb_scrub,
            dct_scrub={"off": stego.DCT_OFF, "canonical": stego.DCT_CANONICAL, "full": stego.DCT_SCRUBBED}[mode],
            dct_psnr=measured,
            verified_by=self.verified_by,
            kept={
                **({"format": "JPEG"} if not self.opts.format else {}),
                "dct": {"off": "compressed data bit-exact", "canonical": "coefficients", "full": "scrubbed coefficients"}[
                    mode
                ],
                **({"orientation": orientation} if orientation != 1 else {}),
                **({"profile": "rebuilt from the input's"} if icc_out else {}),
            },
        )

    @staticmethod
    def _decode_here(data: bytes) -> tuple[np.ndarray, str]:
        """Decoded pixels of an output this process built from coefficients (nothing in it came from the input)."""
        im = open_image(data)
        im.load()
        return np.asarray(im), im.mode

    @staticmethod
    def _verify_canonical_form(data: bytes, written) -> None:
        """The canonical form is a pure function of the coefficients: writing what was read back must give the very
        same bytes, so nothing else (an encoding choice, a hidden bit) can be in the compressed data."""
        from . import dct

        if not data.endswith(dct.write(written) + b"\xff\xd9"):
            raise VerificationError("the rebuilt JPEG data is not in canonical form")

    def _verify_dct_scrub(self, data, original: np.ndarray, rebuilt: np.ndarray, frame, stats) -> float | None:
        """Check a scrubbed file against what the scrub promises, and against how it actually decodes."""
        from . import dct

        written = dct.read(data)
        if not dct.visible_equal(written, frame):
            raise VerificationError("the written file does not hold the coefficients that were scrubbed")
        problem = dct.check_scrub(written, stats.before)
        if problem:
            raise VerificationError(f"DCT scrub check failed: {problem}")
        self._verify_canonical_form(data, written)
        self.verified_by.append("imgstrip JPEG coefficient reader")
        sse = samples = 0
        row_bytes = int(np.prod(original.shape[1:])) * original.itemsize
        for top, bottom in _strips(original.shape[0], row_bytes):
            a, b = original[top:bottom], rebuilt[top:bottom]
            sse += dct.squared_error(a, b)
            samples += a.size
        measured = dct.psnr_of(sse, samples)
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
    """Remove all metadata and hidden data from an image file held in memory.

    The input is inspected and decoded in the sandbox (a separate, freshly forked process: see sandbox.py) unless
    ``options.sandbox`` is False or the sandbox was switched off for the process; the output is encoded and verified
    here, from what the sandbox handed back.
    """
    if not data:
        raise StripError("file is empty")
    options = options or Options()
    from . import sandbox

    if sandbox.enabled(options.sandbox):
        decoded = sandbox.decode(data, options)
        return _Job(data, options, decoded.report, sandboxed=True).finish(decoded)
    return _Job(data, options, inspect_bytes(data)).run()


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


def canonical_frames(data: bytes, max_pixels: int = DEFAULT_MAX_PIXELS, sandboxed: bool | None = None) -> list[np.ndarray]:
    """Decode every frame the engine would keep from ``data``, as comparable arrays.

    No orientation, colour management or other normalisation is applied, so the arrays of
    an original and of its cleaned copy are equal exactly when the cleaned file shows the
    very same pixel values (nothing is rotated, re-encoded, converted or scrubbed).  The
    decoding runs in the sandbox unless it is switched off (``sandboxed``, as Options.sandbox).
    """
    from . import sandbox

    if sandbox.enabled(sandboxed):
        return sandbox.frames(data, max_pixels)
    return canonical_frames_here(data, max_pixels)


def canonical_frames_here(data: bytes, max_pixels: int = DEFAULT_MAX_PIXELS) -> list[np.ndarray]:
    """``canonical_frames`` in this process."""
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
