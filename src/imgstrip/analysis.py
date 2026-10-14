"""Pixel-level analysis for ``imgstrip --check --deep``.

The container inspector finds metadata stored *beside* the pixels.  This
module decodes the image and looks for data hidden *in* the pixels:

* colour values stored under fully transparent pixels (invisible but kept),
* unused, duplicate or invisible palette entries,
* LSB steganography, estimated with Sample Pair Analysis (Dumitrescu, Wu &
  Wang, 2003), which estimates the fraction of least-significant bits that
  were overwritten by a hidden message.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from .core import StripError, _pillow_guard, canonical_format, open_image
from .inspect import HIDDEN, Finding, sniff_format

# Thresholds for the joint estimate min(SPA, WS), calibrated on 75 real
# camera photos (worst clean image: 0.117) with simulated LSB-replacement
# payloads (every image with >= 20% payload scored >= 0.195).
SPA_LIKELY = 0.18
SPA_POSSIBLE = 0.12
_MIN_PAIRS = 4096


def spa_estimate(channel: np.ndarray) -> float | None:
    """Estimated LSB embedding rate (0..1) of one 8-bit channel, or None if undecidable."""
    c = channel.astype(np.int32)
    u = np.concatenate([c[:, :-1].ravel(), c[:-1, :].ravel()])
    v = np.concatenate([c[:, 1:].ravel(), c[1:, :].ravel()])
    total = u.size
    if total < _MIN_PAIRS:
        return None
    v_even = (v & 1) == 0
    x = int(np.count_nonzero((v_even & (u < v)) | (~v_even & (u > v))))
    y = int(np.count_nonzero((v_even & (u > v)) | (~v_even & (u < v))))
    z = int(np.count_nonzero(u == v))
    w = int(np.count_nonzero(((u >> 1) == (v >> 1)) & (u != v)))
    a = 0.5 * (w + z)
    b = 2.0 * x - total
    c_ = float(y - x)
    if a == 0:
        return None if b == 0 else max(0.0, min(1.0, -c_ / b))
    disc = b * b - 4 * a * c_
    if disc < 0:
        p = -b / (2 * a)  # no real root: take the closest point of the parabola
    else:
        roots = [(-b + np.sqrt(disc)) / (2 * a), (-b - np.sqrt(disc)) / (2 * a)]
        p = min(roots, key=abs)
    return float(max(0.0, min(1.0, p)))


def ws_estimate(channel: np.ndarray) -> float | None:
    """Weighted-stego estimate of the LSB embedding rate (Fridrich & Goljan 2004).

    Uses the Ker-Boehme 3x3 predictor and inverse-local-variance weights;
    saturated pixels are excluded.
    """
    s = channel.astype(np.float64)
    if s.shape[0] < 3 or s.shape[1] < 3:
        return None
    p = s[1:-1, 1:-1]
    pred = (
        -s[:-2, :-2]
        + 2 * s[:-2, 1:-1]
        - s[:-2, 2:]
        + 2 * s[1:-1, :-2]
        + 2 * s[1:-1, 2:]
        - s[2:, :-2]
        + 2 * s[2:, 1:-1]
        - s[2:, 2:]
    ) / 4.0
    neighbours = np.stack([s[:-2, 1:-1], s[2:, 1:-1], s[1:-1, :-2], s[1:-1, 2:]])
    weights = (1.0 / (5.0 + neighbours.var(axis=0))) * ((p > 0) & (p < 255))
    total = weights.sum()
    if p.size < _MIN_PAIRS or total == 0:
        return None
    sign = np.where(p % 2 == 0, -1.0, 1.0)  # s - s_flipped
    return float(2.0 * np.sum(weights * sign * (p - pred)) / total)


def lsb_payload_estimate(channel: np.ndarray) -> float | None:
    """Conservative embedding-rate estimate: both detectors must agree."""
    a, b = spa_estimate(channel), ws_estimate(channel)
    if a is None or b is None:
        return None
    return max(0.0, min(a, b, 1.0))


def _lossless(im: Image.Image, data: bytes, fmt: str) -> bool:
    """SPA is only meaningful on losslessly stored pixels."""
    if fmt == "TIFF":
        return im.info.get("compression") not in ("jpeg", "tiff_jpeg")
    if fmt == "WEBP":
        return b"VP8L" in data[:4096] and b"VP8 " not in data[:4096]
    return fmt in ("PNG", "BMP", "PPM", "QOI", "TGA", "PCX", "SGI")


def analyze_pixels(data: bytes) -> list[Finding]:
    findings: list[Finding] = []
    caught: list[str] = []
    with _pillow_guard(caught):  # default 300 MP limit
        im = open_image(data)
        fmt = canonical_format(im.format)
        try:
            im.load()
        except Exception as exc:  # noqa: BLE001 - decoders raise arbitrary errors on corrupt data
            raise StripError(f"cannot decode image: {exc}") from None
        if im.mode == "P":
            findings += _palette_findings(im)
            rgba = np.asarray(im.convert("RGBA"))
        elif im.mode in ("RGBA", "LA", "PA"):
            rgba = np.asarray(im.convert("RGBA"))
        else:
            rgba = None
        if rgba is not None:
            hidden = (rgba[..., 3] == 0) & (rgba[..., :3] != 0).any(axis=-1)
            if hidden.any():
                findings.append(
                    Finding(HIDDEN, "Colour data under fully transparent pixels", f"{int(hidden.sum())} pixels")
                )
        if _lossless(im, data, sniff_format(data) or fmt) and im.mode in ("L", "LA", "RGB", "RGBA"):
            arr = np.asarray(im)
            if arr.ndim == 2:
                arr = arr[..., None]
            names = {"L": "L", "LA": "L", "RGB": "RGB", "RGBA": "RGB"}[im.mode]
            estimates = {}
            for i, name in enumerate(names):
                est = lsb_payload_estimate(arr[..., i])
                if est is not None:
                    estimates[name] = est
            if estimates:
                worst = max(estimates.values())
                detail = "estimated payload " + ", ".join(f"{k}={v:.0%}" for k, v in estimates.items())
                if worst >= SPA_LIKELY:
                    findings.append(Finding(HIDDEN, "Likely LSB steganography", detail + " of capacity"))
                elif worst >= SPA_POSSIBLE:
                    findings.append(Finding(HIDDEN, "Possible LSB steganography", detail + " of capacity"))
    return findings


def _palette_findings(im: Image.Image) -> list[Finding]:
    out = []
    idx = np.asarray(im)
    pal = im.getpalette("RGB") or []
    n = len(pal) // 3
    used = np.flatnonzero(np.bincount(idx.ravel(), minlength=256))
    unused = [i for i in range(n) if i not in set(used.tolist())]
    nonzero_unused = [i for i in unused if any(pal[3 * i : 3 * i + 3])]
    if nonzero_unused:
        out.append(Finding(HIDDEN, "Unused palette entries with colour data", f"{len(nonzero_unused)} of {n} entries"))
    colors = [tuple(pal[3 * i : 3 * i + 3]) for i in used.tolist() if i < n]
    if len(colors) != len(set(colors)):
        out.append(
            Finding(
                HIDDEN,
                "Duplicate palette colours in use",
                f"{len(colors) - len(set(colors))} duplicates (can encode hidden bits)",
            )
        )
    return out
