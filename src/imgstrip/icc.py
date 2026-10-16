"""Colour-profile handling.

Embedded ICC profiles are metadata (they routinely carry device names,
creator strings, dates and calibration data), but they also define what the
pixel values *mean*.  Dropping a non-sRGB profile silently changes how the
picture looks, so imgstrip offers three policies:

``convert``  (default) transform the pixels to sRGB and embed nothing.
``sanitize`` keep the colour space but replace the profile with a minimal one
             rebuilt from scratch (colorimetric tags only, no text, no dates).
``drop``     discard the profile without touching the pixels.

8-bit images are converted with LittleCMS (via Pillow).  For 16-bit images,
which Pillow's CMS bindings cannot handle, this module contains a small
matrix/TRC colour engine implemented with numpy.
"""

from __future__ import annotations

import functools
import io
import struct

import numpy as np
from PIL import Image, ImageCms


class ProfileError(Exception):
    """The embedded profile cannot be used (corrupt or incompatible)."""


@functools.lru_cache(maxsize=1)
def srgb_profile() -> ImageCms.ImageCmsProfile:
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))


def colorspace(icc: bytes) -> str:
    """ICC data colour space signature, e.g. 'RGB', 'GRAY', 'CMYK', 'LAB'."""
    if len(icc) < 128 or icc[36:40] != b"acsp":
        raise ProfileError("not an ICC profile")
    return icc[16:20].decode("latin-1").strip().upper()


def load(icc: bytes) -> ImageCms.ImageCmsProfile:
    try:
        return ImageCms.ImageCmsProfile(io.BytesIO(icc))
    except (OSError, ImageCms.PyCMSError, ValueError, TypeError) as exc:
        raise ProfileError(f"unreadable ICC profile ({exc})") from None


def description(icc: bytes) -> str:
    try:
        return (ImageCms.getProfileDescription(load(icc)) or "").strip() or "unnamed"
    except (ProfileError, ImageCms.PyCMSError):
        return "unnamed"


def _intent(profile: ImageCms.ImageCmsProfile) -> int:
    try:
        intent = ImageCms.getDefaultIntent(profile)
        if ImageCms.isIntentSupported(profile, intent, ImageCms.Direction.INPUT):
            return intent
    except ImageCms.PyCMSError:
        pass
    return ImageCms.Intent.RELATIVE_COLORIMETRIC


def _transform(img: Image.Image, src: ImageCms.ImageCmsProfile, out_mode: str) -> Image.Image:
    try:
        return ImageCms.profileToProfile(
            img,
            src,
            srgb_profile(),
            renderingIntent=_intent(src),
            outputMode=out_mode,
            flags=ImageCms.Flags.BLACKPOINTCOMPENSATION,
        )
    except ImageCms.PyCMSError as exc:
        raise ProfileError(f"colour conversion failed ({exc})") from None


# A 17x17x17 lattice of RGB colours (plus a fine gray ramp) used to compare
# profiles numerically instead of trusting their (free-text) names.
@functools.lru_cache(maxsize=1)
def _probe_rgb() -> Image.Image:
    steps = np.linspace(0, 255, 17).round().astype(np.uint8)
    grid = np.stack(np.meshgrid(steps, steps, steps, indexing="ij"), axis=-1).reshape(-1, 3)
    ramp = np.repeat(np.arange(256, dtype=np.uint8)[:, None], 3, axis=1)
    return Image.fromarray(np.concatenate([grid, ramp])[None, :, :], "RGB")


@functools.lru_cache(maxsize=64)
def is_srgb_equivalent(icc: bytes) -> bool:
    """True when converting through this profile to sRGB changes no pixel by more than 1/255."""
    space = colorspace(icc)
    src = load(icc)
    probe = _probe_rgb()
    if space == "RGB":
        out = _transform(probe, src, "RGB")
        diff = np.abs(np.asarray(out, np.int16) - np.asarray(probe, np.int16))
        return int(diff.max()) <= 1
    if space == "GRAY":
        ramp = Image.fromarray(np.arange(256, dtype=np.uint8)[None, :], "L")
        out = np.asarray(_transform(ramp, src, "RGB"), np.int16)
        return int(np.abs(out - np.arange(256)[None, :, None]).max()) <= 1
    return False


def convert_image_8bit(img: Image.Image, icc: bytes) -> Image.Image:
    """Convert an 8-bit image tagged with ``icc`` to untagged sRGB, preserving alpha."""
    space = colorspace(icc)
    src = load(icc)
    mode = img.mode
    if space == "RGB" and mode in ("RGB", "RGBA"):
        return _transform(img, src, mode)
    if space == "CMYK" and mode == "CMYK":
        return _transform(img, src, "RGB")
    if space == "GRAY" and mode in ("L", "LA"):
        gray = img.getchannel("L") if mode == "LA" else img
        converted = _transform(gray, src, "RGB").getchannel("G")
        if mode == "LA":
            return Image.merge("LA", (converted, img.getchannel("A")))
        return converted
    if space == "LAB" and mode == "LAB":
        return _transform(img, src, "RGB")
    raise ProfileError(f"profile colour space {space} does not match image mode {mode}")


def convert_palette_8bit(palette_rgb: list[tuple[int, int, int]], icc: bytes) -> list[tuple[int, int, int]]:
    """Convert palette entries (instead of pixels) of a paletted image to sRGB."""
    if colorspace(icc) != "RGB":
        raise ProfileError("palette images need an RGB profile")
    strip = Image.new("RGB", (len(palette_rgb), 1))
    strip.putdata(palette_rgb)
    out = _transform(strip, load(icc), "RGB")
    return list(out.getdata())


# --------------------------------------------------------------------------
# Matrix/TRC engine (used for 16-bit images and for profile sanitising)
# --------------------------------------------------------------------------


def _s15(raw: bytes, count: int) -> list[float]:
    return [v / 65536.0 for v in struct.unpack(f">{count}i", raw[: 4 * count])]


def _tag_table(icc: bytes) -> dict[bytes, bytes]:
    (count,) = struct.unpack_from(">I", icc, 128)
    if count > 512:
        raise ProfileError("implausible tag count")
    tags = {}
    for i in range(count):
        sig, off, size = struct.unpack_from(">4sII", icc, 132 + 12 * i)
        if off + size > len(icc):
            raise ProfileError("tag overruns profile")
        tags[sig] = icc[off : off + size]
    return tags


class _Curve:
    """An ICC 'curv' or 'para' tone curve: device value -> linear light."""

    def __init__(self, raw: bytes):
        self.raw = raw
        kind = raw[:4]
        if kind == b"curv":
            (n,) = struct.unpack_from(">I", raw, 8)
            if n == 0:
                self.kind, self.params = "identity", ()
            elif n == 1:
                (g,) = struct.unpack_from(">H", raw, 12)
                self.kind, self.params = "gamma", (g / 256.0,)
            else:
                table = np.frombuffer(raw, ">u2", count=n, offset=12).astype(np.float64) / 65535.0
                self.kind, self.params = "table", (table,)
        elif kind == b"para":
            (ftype,) = struct.unpack_from(">H", raw, 8)
            nparams = {0: 1, 1: 3, 2: 4, 3: 5, 4: 7}.get(ftype)
            if nparams is None:
                raise ProfileError("unknown parametric curve type")
            self.kind, self.params = f"para{ftype}", tuple(_s15(raw[12:], nparams))
        else:
            raise ProfileError("unsupported tone curve type")

    def evaluate(self, x: np.ndarray) -> np.ndarray:
        k, p = self.kind, self.params
        if k == "identity":
            return x.copy()
        if k == "gamma":
            return np.power(x, p[0])
        if k == "table":
            table = p[0]
            return np.interp(x, np.linspace(0.0, 1.0, len(table)), table)
        g = p[0]
        with np.errstate(invalid="ignore", divide="ignore"):
            if k == "para0":
                return np.power(x, g)
            a, b = p[1], p[2]
            base = np.maximum(a * x + b, 0.0)
            if k == "para1":
                return np.where(x >= -b / a, np.power(base, g), 0.0)
            if k == "para2":
                c = p[3]
                return np.where(x >= -b / a, np.power(base, g) + c, c)
            if k == "para3":
                c, d = p[3], p[4]
                return np.where(x >= d, np.power(base, g), c * x)
            c, d, e, f = p[3], p[4], p[5], p[6]
            return np.where(x >= d, np.power(base, g) + e, c * x + f)


class MatrixShaper:
    """Colorimetric model of an RGB matrix/TRC or gray TRC ICC profile."""

    def __init__(self, icc: bytes):
        try:
            self._parse(icc)
        except (struct.error, ValueError, IndexError) as exc:  # a tag shorter than its own header says
            raise ProfileError(f"malformed profile ({exc})") from None

    def _parse(self, icc: bytes) -> None:
        space = colorspace(icc)
        if icc[20:24] != b"XYZ ":
            raise ProfileError("profile connection space is not XYZ")
        tags = _tag_table(icc)
        if any(sig in tags for sig in (b"A2B0", b"A2B1", b"A2B2", b"D2B0")):
            # LUT-based profiles take precedence over the matrix in LittleCMS;
            # a matrix-only evaluation would not match.
            raise ProfileError("LUT-based profile")
        self.space = space
        if space == "RGB":
            try:
                cols = [_s15(tags[sig][8:], 3) for sig in (b"rXYZ", b"gXYZ", b"bXYZ")]
                self.curves = [_Curve(tags[sig]) for sig in (b"rTRC", b"gTRC", b"bTRC")]
            except KeyError as exc:
                raise ProfileError(f"missing tag {exc}") from None
            self.matrix = np.array(cols, dtype=np.float64).T  # columns are the primaries
            self.raw_tags = {sig: tags[sig] for sig in (b"rXYZ", b"gXYZ", b"bXYZ", b"rTRC", b"gTRC", b"bTRC")}
            if abs(np.linalg.det(self.matrix)) < 1e-9:
                raise ProfileError("singular colour matrix")
        elif space == "GRAY":
            if b"kTRC" not in tags:
                raise ProfileError("missing gray tone curve")
            self.curves = [_Curve(tags[b"kTRC"])]
            self.matrix = None
            self.raw_tags = {b"kTRC": tags[b"kTRC"]}
        else:
            raise ProfileError(f"{space} profiles are not matrix/TRC profiles")
        for sig, body in self.raw_tags.items():
            if body[:4] not in (b"XYZ ", b"curv", b"para"):
                raise ProfileError(f"unexpected type for tag {sig!r}")

    # -- conversion ---------------------------------------------------------

    @staticmethod
    @functools.lru_cache(maxsize=1)
    def _srgb_inverse() -> np.ndarray:
        model = MatrixShaper(srgb_profile().tobytes())
        return np.linalg.inv(model.matrix)

    @staticmethod
    def _encode_srgb(linear: np.ndarray) -> np.ndarray:
        linear = np.clip(linear, 0.0, 1.0)
        return np.where(linear <= 0.0031308, linear * 12.92, 1.055 * np.power(linear, 1 / 2.4) - 0.055)

    def to_srgb_16bit(self, arr: np.ndarray) -> np.ndarray:
        """Convert a uint16 array of shape (H, W, C) to sRGB; extra channels (alpha) pass through."""
        out = arr.copy()
        n_color = 3 if self.space == "RGB" else 1
        x = np.linspace(0.0, 1.0, 65536)
        luts = [c.evaluate(x) for c in self.curves]
        if self.space == "RGB":
            to_srgb = self._srgb_inverse() @ self.matrix
        rows = max(1, (1 << 22) // max(1, arr.shape[1] * n_color))
        for top in range(0, arr.shape[0], rows):
            block = arr[top : top + rows, :, :n_color]
            linear = np.stack([luts[i][block[..., i]] for i in range(n_color)], axis=-1)
            if self.space == "RGB":
                linear = linear @ to_srgb.T
            encoded = self._encode_srgb(linear)
            out[top : top + rows, :, :n_color] = np.rint(encoded * 65535.0).astype(np.uint16)
        return out


def convert_array_16bit(arr: np.ndarray, icc: bytes) -> np.ndarray:
    model = MatrixShaper(icc)
    n_color = 3 if model.space == "RGB" else 1
    if arr.shape[2] not in (n_color, n_color + 1):
        raise ProfileError(f"{model.space} profile does not match a {arr.shape[2]}-channel image")
    return model.to_srgb_16bit(arr)


# --------------------------------------------------------------------------
# Profile sanitising
# --------------------------------------------------------------------------

_D50 = struct.pack(">3i", 63190, 65536, 54061)  # 0.9642, 1.0, 0.8249 as s15Fixed16


def _desc_tag(text: str) -> bytes:
    ascii_ = text.encode("ascii") + b"\x00"
    body = b"desc" + b"\x00" * 4 + struct.pack(">I", len(ascii_)) + ascii_
    body += struct.pack(">II", 0, 0)  # unicode language code + count
    body += struct.pack(">HB", 0, 0) + b"\x00" * 67  # scriptcode
    return body


def _text_tag(text: str) -> bytes:
    return b"text" + b"\x00" * 4 + text.encode("ascii") + b"\x00"


def build_sanitized(icc: bytes) -> bytes:
    """Rebuild a profile from its colorimetric tags only.

    The result has a fixed header (no creator, device, date or profile ID), a
    generic description and only the matrix/curve tags, copied verbatim after
    validation.  Raises ProfileError if the profile cannot be represented this
    way or if the rebuilt profile is not colorimetrically identical.
    """
    model = MatrixShaper(icc)
    tags: list[tuple[bytes, bytes]] = [
        (b"desc", _desc_tag("RGB" if model.space == "RGB" else "Gray")),
        (b"cprt", _text_tag("No copyright, use freely")),
        (b"wtpt", b"XYZ " + b"\x00" * 4 + _D50),
    ]
    tags += sorted(model.raw_tags.items())
    # Fixed header: version 2.1 display profile, no CMM/platform/device/creator,
    # constant date, zero profile ID.
    rebuilt = _assemble(tags, model.space == "GRAY")
    # Fail closed: the new profile must describe exactly the same colours.
    original, clean = load(icc), load(rebuilt)
    probe = _probe_rgb() if model.space == "RGB" else Image.fromarray(np.arange(256, dtype=np.uint8)[None, :], "L")
    a = np.asarray(_transform(probe, original, "RGB"), np.int16)
    b = np.asarray(_transform(probe, clean, "RGB"), np.int16)
    if int(np.abs(a - b).max()) > 1:
        raise ProfileError("sanitised profile is not colorimetrically equivalent")
    return rebuilt


# --------------------------------------------------------------------------
# Profiles synthesised from PNG gAMA / cHRM chunks
# --------------------------------------------------------------------------

_SRGB_CHROMATICITY = (0.3127, 0.3290, 0.64, 0.33, 0.30, 0.60, 0.15, 0.06)
_BRADFORD = np.array([[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367], [0.0389, -0.0685, 1.0296]])
_D50_XYZ = np.array([0.9642, 1.0, 0.8249])


def _xyz(x: float, y: float) -> np.ndarray:
    return np.array([x / y, 1.0, (1.0 - x - y) / y])


def chromaticity_matrix(chromaticity) -> np.ndarray:
    """RGB->XYZ(D50) matrix from (white x, y, red x, y, green x, y, blue x, y)."""
    if len(chromaticity) != 8:
        raise ProfileError("malformed cHRM chunk")
    wx, wy, rx, ry, gx, gy, bx, by = (float(v) for v in chromaticity)
    if min(wy, ry, gy, by) <= 0:
        raise ProfileError("invalid chromaticities")
    primaries = np.stack([_xyz(rx, ry), _xyz(gx, gy), _xyz(bx, by)], axis=1)
    white = _xyz(wx, wy)
    try:
        scale = np.linalg.solve(primaries, white)
    except np.linalg.LinAlgError:
        raise ProfileError("degenerate chromaticities") from None
    rgb_to_xyz = primaries * scale
    cone_src, cone_dst = _BRADFORD @ white, _BRADFORD @ _D50_XYZ
    adapt = np.linalg.inv(_BRADFORD) @ np.diag(cone_dst / cone_src) @ _BRADFORD
    return adapt @ rgb_to_xyz


def is_srgb_like_png_gamma(gamma: float, chromaticity=None) -> bool:
    """gAMA 1/2.2 with sRGB (or no) chromaticities is what PNG encoders write for sRGB."""
    if abs(gamma - 1 / 2.2) > 0.005:
        return False
    if chromaticity is None:
        return True
    if len(chromaticity) != 8:
        raise ProfileError("malformed cHRM chunk")
    return max(abs(a - b) for a, b in zip(chromaticity, _SRGB_CHROMATICITY, strict=True)) < 0.005


def synthesize_png_profile(gamma: float, chromaticity=None, gray: bool = False) -> bytes:
    """Build an ICC profile equivalent to a PNG's gAMA (+ cHRM) chunks.

    PNG gAMA stores the encoding exponent: linear = sample ** (1 / gamma).
    """
    if not 0.01 < gamma < 100:
        raise ProfileError("implausible gAMA value")
    para = b"para\x00\x00\x00\x00" + struct.pack(">HH", 0, 0) + struct.pack(">i", int(round(65536 / gamma)))

    def xyz_tag(v) -> bytes:
        return b"XYZ \x00\x00\x00\x00" + b"".join(struct.pack(">i", int(round(c * 65536))) for c in v)

    tags = [
        (b"desc", _desc_tag("PNG gAMA/cHRM")),
        (b"cprt", _text_tag("No copyright, use freely")),
        (b"wtpt", b"XYZ " + b"\x00" * 4 + _D50),
    ]
    if gray:
        tags.append((b"kTRC", para))
    else:
        m = chromaticity_matrix(chromaticity or _SRGB_CHROMATICITY)
        tags += [
            (b"bTRC", para),
            (b"bXYZ", xyz_tag(m[:, 2])),
            (b"gTRC", para),
            (b"gXYZ", xyz_tag(m[:, 1])),
            (b"rTRC", para),
            (b"rXYZ", xyz_tag(m[:, 0])),
        ]
    return _assemble(tags, gray)


def _assemble(tags: list[tuple[bytes, bytes]], gray: bool) -> bytes:
    header_and_table = 128 + 4 + 12 * len(tags)
    table = struct.pack(">I", len(tags))
    data = b""
    for sig, body in tags:
        data += b"\x00" * ((-len(data) - header_and_table) % 4)
        table += struct.pack(">4sII", sig, header_and_table + len(data), len(body))
        data += body
    header = struct.pack(
        ">I4sI4s4s4s12s4s4sII4sQI12s4s16s28s",
        header_and_table + len(data),
        b"\x00" * 4,
        0x02100000,
        b"mntr",
        b"GRAY" if gray else b"RGB ",
        b"XYZ ",
        struct.pack(">6H", 2000, 1, 1, 0, 0, 0),
        b"acsp",
        b"\x00" * 4,
        0,
        0,
        b"\x00" * 4,
        0,
        0,
        _D50,
        b"\x00" * 4,
        b"\x00" * 16,
        b"\x00" * 28,
    )
    return header + table + data
