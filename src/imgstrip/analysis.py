"""Pixel-level analysis for ``imgstrip --check --deep``.

The container inspector finds metadata stored *beside* the pixels.  This
module decodes the image and looks for data hidden *in* the pixels:

* colour values stored under fully transparent pixels (invisible but kept),
* unused, duplicate or invisible palette entries,
* LSB steganography, estimated with Sample Pair Analysis (Dumitrescu, Wu &
  Wang, 2003), which estimates the fraction of least-significant bits that
  were overwritten by a hidden message.

The LSB test is statistical and covers one family of methods.  ``scan_pixels``
therefore returns, besides the findings, a ``stego.Verdict`` that says what was
and was not tested; "not detected" is never "nothing is hidden".
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from . import stego
from .core import StripError, _pillow_guard, canonical_format, open_image
from .inspect import HIDDEN, Finding, sniff_format, unused_palette_entries

# Thresholds for the joint estimate min(SPA, WS), calibrated on 75 real
# camera photos (worst clean image: 0.117) with simulated LSB-replacement
# payloads (every image with >= 20% payload scored >= 0.195).
SPA_LIKELY = 0.18
SPA_POSSIBLE = 0.12
_MIN_PAIRS = 4096
# Pixels examined across all frames of an animation.  Frames are tested one at a time and each in row strips, so
# this bounds time, not memory.
_FRAME_PIXEL_BUDGET = 100_000_000
# Both tests are sums over neighbouring pixels, so they run over strips of rows that overlap by one row: the
# totals are the same as for the whole picture, and the working memory is a few megabytes whatever its size.
_STRIP_PIXELS = 1 << 18


def _strips(height: int, width: int):
    """(first, top, bottom, last): strip [top, bottom) of the rows, read as [first, last) with one row of context."""
    rows = max(1, _STRIP_PIXELS // max(1, width))
    for top in range(0, height, rows):
        bottom = min(height, top + rows)
        yield max(0, top - 1), top, bottom, min(height, bottom + 1)


class _LsbStats:
    """SPA counts and WS sums of one 8-bit channel, gathered strip by strip (see ``_strips``)."""

    def __init__(self, height: int, width: int) -> None:
        self.height, self.width = height, width
        self.pairs = self.x = self.z = self.w = 0
        self.ws_num = self.ws_den = 0.0

    def add(self, block: np.ndarray, first: int, top: int, bottom: int) -> None:
        """``block`` holds image rows [first, first + len(block)), and [top, bottom) are this strip's own rows."""
        if block.dtype != np.uint8:
            block = block.astype(np.int64)
        own = block[top - first : bottom - first]
        # SPA: horizontal pairs inside this strip's rows, vertical pairs ending in them.
        self._pairs(own[:, :-1], own[:, 1:])
        self._pairs(block[: bottom - 1 - first], block[1 : bottom - first])
        # WS: the 3x3 neighbourhood of every interior pixel in this strip's rows.
        lo, hi = max(1, top), min(self.height - 1, bottom)
        if hi > lo and self.width >= 3:
            num, den = _ws_sums(block[lo - 1 - first : hi + 1 - first])
            self.ws_num += num
            self.ws_den += den

    def _pairs(self, u: np.ndarray, v: np.ndarray) -> None:
        if not u.size:
            return
        v_even = (v & 1) == 0
        less, more = u < v, u > v
        self.x += int(np.count_nonzero(less & v_even)) + int(np.count_nonzero(more & ~v_even))
        same = u == v
        self.z += int(np.count_nonzero(same))
        self.w += int(np.count_nonzero((u >> 1) == (v >> 1))) - int(np.count_nonzero(same))
        self.pairs += u.size

    def spa(self) -> float | None:
        total, x, z, w = self.pairs, self.x, self.z, self.w
        if total < _MIN_PAIRS:
            return None
        y = total - x - z  # every pair with u != v is counted by exactly one of x and y
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

    def ws(self) -> float | None:
        if self.height < 3 or self.width < 3:
            return None
        if (self.height - 2) * (self.width - 2) < _MIN_PAIRS or self.ws_den == 0:
            return None
        return float(2.0 * self.ws_num / self.ws_den)


def _ws_sums(s: np.ndarray) -> tuple[float, float]:
    """Weighted residual and total weight over the interior pixels of ``s`` (integers, at least 3x3).

    The predictor and the local variance are small integers scaled by 1/4 and 1/16, so they are computed exactly
    in integer arithmetic; the only roundings are the ones the floating-point formula makes (the weight's
    division and the final products), so the result is the same to the last bit.
    """
    s = s.astype(np.int32 if s.dtype == np.uint8 else np.int64)
    p = s[1:-1, 1:-1]
    up, down, left, right = s[:-2, 1:-1], s[2:, 1:-1], s[1:-1, :-2], s[1:-1, 2:]
    cross = up + down + left + right
    pred4 = 2 * cross - (s[:-2, :-2] + s[:-2, 2:] + s[2:, :-2] + s[2:, 2:])  # 4 x the Ker-Boehme prediction
    var16 = 4 * (up * up + down * down + left * left + right * right) - cross * cross  # 16 x their variance
    weights = 16.0 / (80 + var16)  # 1 / (5 + variance)
    weights *= (p > 0) & (p < 255)
    residual4 = 4 * p - pred4
    signed = np.where(p & 1, residual4, -residual4)  # (s - s_flipped) x 4 (p - pred)
    return float(np.sum(weights * signed)) / 4.0, float(weights.sum())


def _channel_stats(channel: np.ndarray) -> _LsbStats:
    height, width = channel.shape
    stats = _LsbStats(height, width)
    for first, top, bottom, last in _strips(height, width):
        stats.add(channel[first:last], first, top, bottom)
    return stats


def spa_estimate(channel: np.ndarray) -> float | None:
    """Estimated LSB embedding rate (0..1) of one 8-bit channel, or None if undecidable."""
    return _channel_stats(channel).spa()


def ws_estimate(channel: np.ndarray) -> float | None:
    """Weighted-stego estimate of the LSB embedding rate (Fridrich & Goljan 2004).

    Uses the Ker-Boehme 3x3 predictor and inverse-local-variance weights;
    saturated pixels are excluded.
    """
    return _channel_stats(channel).ws()


def _joint(stats: _LsbStats) -> float | None:
    a, b = stats.spa(), stats.ws()
    if a is None or b is None:
        return None
    return max(0.0, min(a, b, 1.0))


def lsb_payload_estimate(channel: np.ndarray) -> float | None:
    """Conservative embedding-rate estimate: both detectors must agree."""
    return _joint(_channel_stats(channel))


def _lossless(im: Image.Image, data: bytes, fmt: str) -> bool:
    """SPA is only meaningful on losslessly stored pixels."""
    if fmt == "TIFF":
        return im.info.get("compression") not in ("jpeg", "tiff_jpeg")
    if fmt == "WEBP":
        return b"VP8L" in data[:4096] and b"VP8 " not in data[:4096]
    return fmt in ("PNG", "BMP", "PPM", "QOI", "TGA", "PCX", "SGI")


@dataclass
class PixelScan:
    findings: list[Finding] = field(default_factory=list)
    verdict: stego.Verdict = field(default_factory=lambda: stego.not_scanned("the pixels were not scanned"))


def analyze_pixels(data: bytes) -> list[Finding]:
    return scan_pixels(data).findings


def scan(data: bytes, sandboxed: bool | None = None) -> PixelScan:
    """``scan_pixels``, run in the decoder sandbox unless it is switched off (``sandboxed`` as Options.sandbox)."""
    from . import sandbox

    if sandbox.enabled(sandboxed):
        return sandbox.scan(data)
    return scan_pixels(data)


_LOSSY = {"JPEG", "MPO", "AVIF", "HEIF", "HEIC", "WEBP"}


def _untestable_reason(fmt: str, lossless: bool, modes: set[str], too_small: bool) -> str:
    if "P" in modes or fmt == "GIF":
        return "palette image: only unused and duplicate palette entries were checked"
    if not lossless:
        if fmt in _LOSSY:
            return f"{fmt} is lossy: it stores compressed picture data, not the sample values the LSB tests read"
        return f"{fmt} images are not covered by the LSB tests (they need losslessly stored 8-bit samples)"
    other = modes - {"L", "LA", "RGB", "RGBA"}
    if other:
        return (
            f"{', '.join(sorted(other))} images are not covered by the LSB tests (they need 8-bit grey or RGB samples)"
        )
    if too_small:
        return "too few pixels for a reliable estimate"
    return "no image data the LSB tests can read"


def scan_pixels(data: bytes) -> PixelScan:
    """Look inside the pixels of ``data``: findings, plus an honest verdict on what was covered."""
    findings: list[Finding] = []
    caught: list[str] = []
    with _pillow_guard(caught):  # the default pixel limit
        im = open_image(data)
        fmt = canonical_format(im.format)
        sniffed = sniff_format(data) or fmt
        n_frames = getattr(im, "n_frames", 1) or 1
        estimates: dict[str, float] = {}
        tested = 0
        modes: set[str] = set()
        too_small = False
        lossless = True
        hidden_alpha = 0
        palette_labels: set[str] = set()
        budget = _FRAME_PIXEL_BUDGET
        for index in range(n_frames):
            if index:
                if budget <= 0:
                    break
                try:
                    im.seek(index)
                except Exception as exc:  # decoders raise arbitrary errors on corrupt data
                    raise StripError(f"cannot decode frame {index + 1}: {exc}") from None
            try:
                im.load()
            except Exception as exc:  # decoders raise arbitrary errors on corrupt data
                raise StripError(f"cannot decode image: {exc}") from None
            width, height = im.size
            budget -= width * height
            modes.add(im.mode)
            if im.mode == "P":
                for f in _palette_findings(im):
                    if f.label not in palette_labels:
                        palette_labels.add(f.label)
                        findings.append(f)
            alpha = im.mode in ("P", "RGBA", "LA", "PA")
            frame_lossless = _lossless(im, data, sniffed)
            lossless = lossless and frame_lossless
            names = {"L": "L", "LA": "L", "RGB": "RGB", "RGBA": "RGB"}.get(im.mode, "") if frame_lossless else ""
            stats = [_LsbStats(height, width) for _ in names]
            if alpha or stats:
                for first, top, bottom, last in _strips(height, width):
                    if alpha:
                        rgba = np.asarray(im.crop((0, top, width, bottom)).convert("RGBA"))
                        hidden_alpha += int(((rgba[..., 3] == 0) & (rgba[..., :3] != 0).any(axis=-1)).sum())
                    if stats:
                        block = np.asarray(im.crop((0, first, width, last)))
                        if block.ndim == 2:
                            block = block[..., None]
                        for i, channel in enumerate(stats):
                            channel.add(np.ascontiguousarray(block[..., i]), first, top, bottom)
            if not stats:
                continue
            got = False
            for name, channel in zip(names, stats, strict=True):
                est = _joint(channel)
                if est is not None:
                    estimates[name] = max(estimates.get(name, 0.0), est)
                    got = True
            if got:
                tested += 1
            else:
                too_small = True
        if hidden_alpha:
            findings.append(Finding(HIDDEN, "Colour data under fully transparent pixels", f"{hidden_alpha} pixels"))
    if not tested:
        return PixelScan(findings, stego.not_scanned(_untestable_reason(fmt, lossless, modes, too_small)))
    coverage = "".join(estimates) + (" channels" if len(estimates) > 1 else " channel")
    complete = tested == n_frames
    if n_frames > 1:
        coverage += f", {tested} of {n_frames} frames"
    worst = max(estimates.values())
    detail = "estimated payload " + ", ".join(f"{k}={v:.0%}" for k, v in estimates.items()) + " of capacity"
    if worst >= SPA_LIKELY:
        findings.append(Finding(HIDDEN, "Likely LSB steganography", detail))
        return PixelScan(findings, stego.Verdict(stego.DETECTED, stego.LIKELY, detail, complete))
    if worst >= SPA_POSSIBLE:
        findings.append(Finding(HIDDEN, "Possible LSB steganography", detail))
        return PixelScan(findings, stego.Verdict(stego.DETECTED, stego.POSSIBLE, detail, complete))
    return PixelScan(findings, stego.Verdict(stego.NOT_DETECTED, detail=coverage, complete=complete))


def _palette_findings(im: Image.Image) -> list[Finding]:
    out = []
    idx = np.asarray(im)
    pal = im.getpalette("RGB") or []
    n = len(pal) // 3
    used = np.flatnonzero(np.bincount(idx.ravel(), minlength=256))
    unused = unused_palette_entries(im)
    if unused:
        out.append(Finding(HIDDEN, "Unused palette entries with colour data", f"{unused} of {n} entries"))
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
