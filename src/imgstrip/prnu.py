"""Disrupting the camera sensor fingerprint (PRNU).

Every image sensor has a tiny, fixed, multiplicative pattern of pixel-to-pixel
sensitivity differences, the photo-response non-uniformity (PRNU).  It is
close to white noise, it survives JPEG compression and colour conversion, and
anyone holding other photos from the same camera (or the camera itself) can
estimate it and test whether a given image came from that sensor, with the
same confidence as a fingerprint match.  No metadata remover touches it:
it is not metadata, it is in the pixels.

What does *not* work: adding noise.  The pattern is detected by correlating
over millions of pixels, so imperceptible noise (a few grey levels) leaves the
correlation essentially intact, and even clearly visible noise only halves it.

What does work is *desynchronisation*: the detector is a matched filter that
needs each pixel's noise at the pixel position the fingerprint expects.  A
smooth, random, non-rigid warp moves every part of the picture by a different
fraction of a pixel to a few pixels, so the noise no longer lines up, and no
single global shift or rotation can bring it back.  A rigid shift is useless
(the detector searches over shifts), and a warp that is too small leaves too
many pixels in place; the amplitude here was calibrated against simulated
attackers (see ``tests/test_prnu.py`` and the README).

To keep the picture looking the same, the random motion is made
*edge-parallel*: near strong, coherent edges (straight lines, building
corners, horizons) the component of the motion perpendicular to the edge is
removed, so an edge slides along itself instead of bending.  In flat and
textured regions, where any displacement is invisible and where the sensor
pattern is easiest to read, the motion is left free.

The module is deliberately independent of the rest of the engine: it works on
Pillow images and numpy arrays and touches no files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np
from PIL import Image

# RMS displacement per axis (pixels; the vector RMS is sqrt(2) larger) and
# correlation length (pixels) of the random field for a 1 megapixel image.
# Both grow with the fourth root of the pixel count:
# a bigger image has more pixels that happen to stay aligned, and a detector
# gains sqrt(N), so the warp has to be larger in absolute pixels to leave the
# same (undetectable) amount of coherent signal.  Relative to the picture the
# movement is, if anything, smaller on large images.
BASE_RMS = 3.5
BASE_CORR = 28.0
MAX_SCALE = 3.0
MIN_SIDE = 48  # below this the image is too small to hold a usable fingerprint (or a warp)

# Edge-normal motion is suppressed where the block-scale gradient (grey levels
# per block of ``cell`` pixels) rises from EDGE_LO to EDGE_HI.  Block averaging
# makes this insensitive to sensor noise and to the image resolution.
EDGE_LO = 1.0
EDGE_HI = 4.0


def new_rng() -> np.random.Generator:
    """A generator seeded from the operating system.  The warp must be
    unpredictable: anyone who could reproduce it could undo it.  (Tests replace
    this function to get reproducible output.)"""
    return np.random.default_rng(int.from_bytes(os.urandom(16), "big"))


@dataclass
class Warp:
    """A planned warp: the mesh for Pillow's transform plus statistics for the report."""

    size: tuple[int, int]
    mesh: list
    rms: float  # root-mean-square displacement over the image, in pixels
    max_shift: float  # largest displacement, in pixels
    edge_shift: float  # mean displacement *across* strong edges, in pixels (0 if there are none)
    corr: float  # correlation length of the field, in pixels


def parameters(width: int, height: int) -> tuple[float, float, int]:
    """(rms displacement, correlation length, vertex spacing) in pixels for an image of this size."""
    scale = min(max(width * height / 1e6, 1.0) ** 0.25, MAX_SCALE)
    corr = BASE_CORR * scale
    cell = max(8, 2 * round(corr / 4))  # even, so block statistics stay aligned
    return BASE_RMS * scale, corr, cell


def supported(width: int, height: int) -> bool:
    return min(width, height) >= MIN_SIDE


def _gauss(a: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian blur of a 2-D grid with reflected borders (FFT; sigma in grid cells)."""
    pad = int(3 * sigma) + 1
    p = np.pad(a, pad, mode="reflect")
    ky = np.fft.fftfreq(p.shape[0])[:, None]
    kx = np.fft.rfftfreq(p.shape[1])[None, :]
    g = np.exp(-2 * (np.pi * sigma) ** 2 * (kx**2 + ky**2))
    out = np.fft.irfft2(np.fft.rfft2(p) * g, s=p.shape)
    return out[pad:-pad, pad:-pad]


def _smoothstep(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    t = np.clip((x - lo) / (hi - lo), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _structure(luma: Image.Image, cell: int, corr: float, grid: tuple[int, int]):
    """Edge normal, coherence and strength on the vertex grid.

    The luminance is box-averaged to one value per ``cell`` x ``cell`` block,
    which removes sensor noise, and the gradient structure tensor of that small
    image is smoothed at the scale of the displacement field.  Returns
    ``(nx, ny, coherence, strength)``; ``strength`` is the block-scale gradient
    magnitude in grey levels.
    """
    gh, gw = grid
    small = np.asarray(luma.reduce(cell), dtype=np.float64)
    # Pad to the vertex grid by replicating the last block row/column.
    small = np.pad(small, ((0, max(gh - small.shape[0], 0)), (0, max(gw - small.shape[1], 0))), mode="edge")
    small = small[:gh, :gw]
    gy, gx = np.gradient(small)
    sigma = corr / cell
    jxx, jyy, jxy = _gauss(gx * gx, sigma), _gauss(gy * gy, sigma), _gauss(gx * gy, sigma)
    trace = jxx + jyy
    coherence = np.sqrt((jxx - jyy) ** 2 + 4 * jxy**2) / (trace + 1e-9)
    theta = 0.5 * np.arctan2(2 * jxy, jxx - jyy)  # direction of the steepest change: the edge normal
    return np.cos(theta), np.sin(theta), coherence, np.sqrt(np.maximum(trace, 0.0))


def plan(width: int, height: int, luma: Image.Image | None, rng: np.random.Generator, project: bool = True) -> Warp:
    """Plan a random smooth warp for an image of this size.

    ``luma`` (an ``L`` image of the same size) steers the edge-parallel
    projection; with ``luma=None`` or ``project=False`` the motion is free (the
    edge statistic is still measured when ``luma`` is given, which lets tests
    compare the two).
    """
    rms, corr, cell = parameters(width, height)
    gw, gh = -(-width // cell) + 1, -(-height // cell) + 1  # vertex grid
    sigma = corr / cell

    def field() -> np.ndarray:
        # A smooth Gaussian field, then flattened to a *uniform* distribution of the same RMS
        # (an exact rank transform, which keeps it smooth).  What decides whether the detector
        # still sees the pattern is how many pixels stay close to their original position, and
        # a uniform field has about half the density near zero of a Gaussian one, so the same
        # protection needs a smaller motion.  It also bounds the displacement (at sqrt(3) RMS).
        f = _gauss(rng.normal(0.0, 1.0, (gh, gw)), sigma).ravel()
        order = np.argsort(f, kind="stable")
        u = np.empty(f.size)
        u[order] = (np.arange(f.size) + 0.5) / f.size
        return ((u - 0.5) * (2.0 * np.sqrt(3.0) * rms)).reshape(gh, gw)

    dx, dy = field(), field()

    vx = np.minimum(np.arange(gw) * cell, width).astype(np.float64)
    vy = np.minimum(np.arange(gh) * cell, height).astype(np.float64)

    strength = None
    if luma is not None:
        nx, ny, coherence, strength = _structure(luma, cell, corr, (gh, gw))
        if project:
            kappa = coherence * _smoothstep(strength, EDGE_LO, EDGE_HI)
            normal = dx * nx + dy * ny
            dx, dy = dx - kappa * normal * nx, dy - kappa * normal * ny

    # Pin the border: displacements fade to zero over 3 RMS, and a displacement vector is at
    # most sqrt(6) RMS long, so no sample ever lands outside the image (which would leave a
    # black rim).
    fade = np.clip(
        np.minimum.reduce(
            [
                np.broadcast_to(vx[None, :], (gh, gw)),
                np.broadcast_to(width - vx[None, :], (gh, gw)),
                np.broadcast_to(vy[:, None], (gh, gw)),
                np.broadcast_to(height - vy[:, None], (gh, gw)),
            ]
        )
        / (3.0 * rms),
        0.0,
        1.0,
    )
    dx, dy = dx * fade, dy * fade

    # Pillow's MESH transform maps each target rectangle to a source quadrilateral
    # (corner order NW, SW, SE, NE); shared vertices keep the warp continuous.
    x0, x1 = vx[:-1][None, :], vx[1:][None, :]
    y0, y1 = vy[:-1][:, None], vy[1:][:, None]
    quads = np.stack(
        np.broadcast_arrays(
            x0 + dx[:-1, :-1], y0 + dy[:-1, :-1],
            x0 + dx[1:, :-1], y1 + dy[1:, :-1],
            x1 + dx[1:, 1:], y1 + dy[1:, 1:],
            x1 + dx[:-1, 1:], y0 + dy[:-1, 1:],
        ),
        axis=-1,
    ).reshape(-1, 8)  # fmt: skip
    boxes = np.stack(np.broadcast_arrays(x0, y0, x1, y1), axis=-1).reshape(-1, 4).astype(int)
    mesh = list(zip(map(tuple, boxes.tolist()), quads.tolist(), strict=True))

    magnitude = np.hypot(dx, dy)
    edge_shift = 0.0
    if strength is not None:
        strong = strength > EDGE_HI
        if strong.any():
            edge_shift = float(np.abs(dx * nx + dy * ny)[strong].mean())
    return Warp(
        size=(width, height),
        mesh=mesh,
        rms=float(np.sqrt((magnitude**2).mean())),
        max_shift=float(magnitude.max()),
        edge_shift=edge_shift,
        corr=corr,
    )


def _transform(im: Image.Image, warp: Warp) -> Image.Image:
    return im.transform(warp.size, Image.Transform.MESH, warp.mesh, Image.Resampling.BICUBIC)


def warp_image(im: Image.Image, warp: Warp) -> Image.Image | None:
    """Warp an 8-bit L / LA / RGB / RGBA / CMYK image; None for modes that cannot carry a fingerprint
    (palette, bilevel, 32-bit)."""
    if im.size != warp.size:
        raise ValueError("warp planned for a different image size")
    if im.mode in ("L", "RGB", "CMYK"):
        return _transform(im, warp)
    if im.mode in ("LA", "RGBA"):
        alpha = im.getchannel("A")
        if alpha.getextrema() == (255, 255):
            # Opaque (the usual case for photos): warp the colour and put the alpha back untouched.
            out = _transform(im.convert(im.mode[:-1]), warp)
            out.putalpha(alpha)
            return out
        premultiplied = "La" if im.mode == "LA" else "RGBa"
        return _transform(im.convert(premultiplied), warp).convert(im.mode)
    return None


def warp_array16(arr: np.ndarray, warp: Warp) -> np.ndarray:
    """Warp a uint16 (H, W, C) array at full precision (colour, grey, grey+alpha or colour+alpha)."""
    h, w, c = arr.shape
    if (w, h) != warp.size:
        raise ValueError("warp planned for a different image size")
    has_alpha = c in (2, 4)
    colour = arr[..., :-1] if has_alpha else arr
    planes = [colour[..., i].astype(np.float32) for i in range(colour.shape[2])]
    alpha = arr[..., -1].astype(np.float32) / 65535.0 if has_alpha else None
    premultiply = alpha is not None and not (arr[..., -1] == 65535).all()
    if premultiply:
        planes = [p * alpha for p in planes]
    out = [np.asarray(_transform(Image.fromarray(p, "F"), warp)) for p in planes]
    result = np.empty_like(arr)
    if premultiply:
        a_out = np.asarray(_transform(Image.fromarray(alpha, "F"), warp)).clip(0.0, 1.0)
        out = [np.where(a_out > 0, p / np.maximum(a_out, 1e-6), 0.0) for p in out]
        result[..., -1] = np.rint(a_out * 65535.0).astype(np.uint16)
    elif has_alpha:
        result[..., -1] = arr[..., -1]
    for i, p in enumerate(out):
        result[..., i] = np.rint(np.clip(p, 0.0, 65535.0)).astype(np.uint16)
    return result
