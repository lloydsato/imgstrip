#!/usr/bin/env python3
"""Calibrate and check the localised stage-2 bound for lossy outputs (finding F2 of the review).

The verifier refuses a lossy output whose worst 8 x 8 block is further from the pixels that were written than a
re-encode moves one (``core.block_tolerance``).  This script measures how far a re-encode moves a block: it cleans a
corpus through the real engine, for every lossy output format, both chroma layouts and qualities 1 to 100, with the
bound switched off, and records the worst block of every output.  Then it checks the shipped bound against what it
measured, and runs the review's fault injections against it.

    python scripts/calibrate_lossy.py            # everything (several minutes)
    python scripts/calibrate_lossy.py --quick    # a smaller corpus

The corpus is meant to contain the worst cases for a block bound: natural photographs (scikit-image's sample
pictures, which this script needs), grey textures, photographs with heavy Gaussian noise (sigma 20 to 60), uniform
white noise, saturated colour graphics with one-pixel lines (the worst case for chroma subsampling), gradients and a
photograph with an alpha gradient.
"""

from __future__ import annotations

import argparse
import collections
import io
import math

import numpy as np
from PIL import Image, ImageDraw

from imgstrip import core, fixtures
from imgstrip.core import Options, StripError, strip_image

QUALITIES = (1, 5, 10, 25, 50, 75, 85, 90, 95, 100)


def corpus(quick: bool) -> dict[str, np.ndarray]:
    import skimage.data
    from skimage.transform import resize

    out: dict[str, np.ndarray] = {}
    rng = np.random.default_rng(7)
    names = ("astronaut", "coffee", "chelsea", "rocket", "hubble_deep_field", "immunohistochemistry", "retina")
    for name in names[: 3 if quick else None]:
        img = getattr(skimage.data, name)()
        if max(img.shape[:2]) > 512:
            img = (resize(img, (384, 384), anti_aliasing=True) * 255).astype(np.uint8)
        out[name] = img[:384, :384]
    for name in ("camera", "brick", "grass", "gravel", "text")[: 2 if quick else None]:
        grey = getattr(skimage.data, name)()[:384, :384]
        out[f"{name} (grey)"] = np.repeat(grey[..., None], 3, axis=2)
    base = out["astronaut"].astype(np.float64)
    for sigma in (20, 40, 60):
        out[f"astronaut + noise {sigma}"] = np.clip(base + rng.normal(0, sigma, base.shape), 0, 255).astype(np.uint8)
    out["white noise"] = rng.integers(0, 256, (256, 256, 3), dtype=np.uint8)
    graphics = Image.new("RGB", (256, 256), (20, 40, 200))
    draw = ImageDraw.Draw(graphics)
    for k in range(40):
        colour = tuple(int(c) for c in rng.integers(0, 2, 3) * 255)
        x, y = rng.integers(0, 240, 2)
        if k % 2:
            draw.rectangle((int(x), int(y), int(x) + 1 + k % 9, int(y) + 1 + k % 7), fill=colour)
        else:
            draw.line((int(x), int(y), int(rng.integers(0, 256)), int(rng.integers(0, 256))), fill=colour, width=1)
    for y in range(0, 256, 2):
        draw.line((0, y, 40, y), fill=(255, 0, 0) if y % 4 else (0, 255, 0))
    draw.text((60, 120), "imgstrip 1.2.5", fill=(255, 255, 0))
    out["colour graphics"] = np.asarray(graphics)
    ramp = np.linspace(0, 255, 256)
    out["gradients"] = np.stack(np.broadcast_arrays(ramp[None, :], ramp[:, None], 255 - ramp[None, :]), -1).astype(
        np.uint8
    )
    out["fixtures.photo"] = fixtures.photo(256, 192)
    return out


def encoded(arr: np.ndarray | Image.Image, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    (arr if isinstance(arr, Image.Image) else Image.fromarray(arr)).save(buf, fmt, **kw)
    return buf.getvalue()


def sources(img: np.ndarray, with_alpha: bool):
    """(source label, bytes, output formats) for the ways a picture reaches each lossy encoder."""
    if (img[..., 0] == img[..., 1]).all() and (img[..., 1] == img[..., 2]).all():
        yield "grey png", encoded(img[..., 0], "PNG"), ("JPEG",)
    yield "png", encoded(img, "PNG"), ("JPEG", "AVIF", "HEIF", "GIF")
    yield "cmyk jpeg", encoded(Image.fromarray(img).convert("CMYK"), "JPEG", quality=100), ("JPEG",)
    yield "jpeg 4:2:0", encoded(img, "JPEG", quality=100, subsampling=2), ("JPEG", "WEBP", "AVIF", "HEIF")
    yield "jpeg 4:4:4", encoded(img, "JPEG", quality=100, subsampling=0), ("JPEG", "AVIF", "HEIF")
    if with_alpha:
        h, w = img.shape[:2]
        yy, xx = np.mgrid[0:h, 0:w]
        alpha = np.clip(255 * (1.2 - np.hypot(xx - w / 2, yy - h / 2) / (0.5 * min(h, w))), 0, 255).astype(np.uint8)
        rgba = np.dstack([img, alpha])
        yield "webp+alpha", encoded(rgba, "WEBP", quality=100, alpha_quality=100), ("WEBP", "AVIF")


def measure(quick: bool):
    """{(format, subsampled, quality): [(luma, chroma, alpha, image, source)]} with the bound switched off."""
    seen: dict[tuple, list] = collections.defaultdict(list)
    refused: list[str] = []
    record: list = []
    real = core._Job._check_blocks

    def capture(self, i, blocks, out):
        record.append((out, self.chroma_subsampled, self._quality(out), blocks))

    core._Job._check_blocks = capture
    try:
        for name, img in corpus(quick).items():
            for label, data, formats in sources(img, with_alpha=name in ("astronaut", "coffee")):
                for fmt in formats:
                    for q in QUALITIES if fmt != "GIF" else (95,):
                        record.clear()
                        try:
                            strip_image(data, Options(format=fmt, quality=q))
                        except StripError as exc:
                            refused.append(f"{name} from {label} to {fmt} q{q}: {exc}")
                            continue
                        for out, subsampled, quality, b in record:
                            seen[(out, subsampled, quality)].append((b.luma, b.chroma, b.alpha, name, label))
            print(f"  measured {name}", flush=True)
    finally:
        core._Job._check_blocks = real
    return seen, refused


def _rectangle(share: float):
    def fault(arr, rng):
        h, w = arr.shape[:2]
        side_h, side_w = max(1, round(h * math.sqrt(share))), max(1, round(w * math.sqrt(share)))
        y, x = (h - side_h) // 2, (w - side_w) // 2
        out = arr.copy()
        out[y : y + side_h, x : x + side_w, :3] = 0
        return out

    return fault


def _random_pixels(arr, rng):
    out = arr.copy()
    flat = out.reshape(-1, out.shape[-1])
    pick = rng.choice(len(flat), max(1, len(flat) // 100), replace=False)
    flat[pick, :3] = rng.integers(0, 256, (len(pick), 3))
    return out


def _noise(level: int):
    def fault(arr, rng):
        out = arr.astype(np.int16)
        out[..., :3] += rng.integers(-level, level + 1, out[..., :3].shape).astype(np.int16)
        return np.clip(out, 0, 255).astype(np.uint8)

    return fault


# The review's pixel faults (its Table 6), applied to the pixels the encoder was given and encoded again with the
# same settings, so that the container stays valid and only the pixel comparison can notice.
FAULTS = {
    "1% of the area blacked out": _rectangle(0.01),
    "2% of the area": _rectangle(0.02),
    "5% of the area": _rectangle(0.05),
    "10% of the area": _rectangle(0.10),
    "1% of pixels random": _random_pixels,
    "uniform noise +-2": _noise(2),
    "uniform noise +-8": _noise(8),
    "uniform noise +-16": _noise(16),
    "uniform noise +-32": _noise(32),
}


def fault_detection(quick: bool, localised: bool) -> dict[str, dict[str, tuple[int, int]]]:
    """{fault: {format: (caught, runs)}} at each format's default quality, as in the review (Section 5.4)."""
    import skimage.data

    pictures = [getattr(skimage.data, n)()[:201, :301] for n in ("astronaut", "coffee", "chelsea")[: 2 if quick else 3]]
    out: dict[str, dict[str, list[int]]] = {name: collections.defaultdict(lambda: [0, 0]) for name in FAULTS}
    real_encode, real_blocks = core._Job._encode, core._Job._check_blocks
    for name, fault in FAULTS.items():
        def encode(self, frames, fmt, loop, src, _fault=fault):
            data, expected, exact = real_encode(self, frames, fmt, loop, src)
            arr = np.asarray(expected[0].image)
            bad = core.Frame(image=Image.fromarray(_fault(arr, np.random.default_rng(1))), duration=expected[0].duration)
            return real_encode(self, [bad], fmt, loop, src)[0], expected, exact

        core._Job._encode = encode
        if not localised:
            core._Job._check_blocks = lambda *a: None
        try:
            for picture in pictures:
                for fmt, data in (
                    ("JPEG", encoded(picture, "JPEG", quality=90)),
                    ("WEBP", encoded(picture, "WEBP", quality=90)),
                    ("HEIF", encoded(picture, "HEIF", quality=90)),
                    ("AVIF", encoded(picture, "AVIF", quality=90)),
                ):
                    cell = out[name][fmt]
                    cell[1] += 1
                    try:
                        strip_image(data, Options())
                    except core.VerificationError:
                        cell[0] += 1
        finally:
            core._Job._encode, core._Job._check_blocks = real_encode, real_blocks
    return {name: {fmt: tuple(v) for fmt, v in cells.items()} for name, cells in out.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--quick", action="store_true", help="a smaller corpus")
    args = parser.parse_args()
    seen, refused = measure(args.quick)
    print("\nworst block a re-encode left (RMS, 8-bit levels), and the shipped bound")
    print(f"{'format':6s} {'chroma':10s} {'q':>4s}   {'luma':>6s} {'bound':>6s}   {'chroma':>6s} {'bound':>6s}   "
          f"{'alpha':>6s} {'bound':>6s}   worst luma")
    tight = []
    for (fmt, subsampled, q), rows in sorted(seen.items()):
        luma = max(rows, key=lambda r: r[0])
        chroma = max(r[1] for r in rows)
        alpha = max(r[2] for r in rows)
        bound = core.block_tolerance(fmt, q, subsampled)
        print(
            f"{fmt:6s} {'subsampled' if subsampled else 'full':10s} {q:4d}   {luma[0]:6.1f} {bound.luma:6.1f}   "
            f"{chroma:6.1f} {bound.chroma:6.1f}   {alpha:6.1f} {bound.alpha:6.1f}   {luma[3]} ({luma[4]})"
        )
        for part, value in (("luma", luma[0]), ("chroma", chroma), ("alpha", alpha)):
            if value > getattr(bound, part):
                tight.append(f"{fmt} {'subsampled' if subsampled else 'full'} q{q} {part}: {value:.1f}")
    print("\nrefused by the mean-error bound (unchanged since 1.2.4):", len(refused))
    for line in refused:
        print("  " + line)
    print("\nmeasured above the shipped bound (would be false refusals):", tight or "none")
    print("\nthe review's pixel faults at the default quality: caught / runs (mean test only -> with the block test)")
    before, after = fault_detection(args.quick, False), fault_detection(args.quick, True)
    formats = list(next(iter(after.values())))
    print(f"{'fault':28s} " + "  ".join(f"{f:>13s}" for f in formats))
    for name in FAULTS:
        cells = [f"{before[name][f][0]}/{before[name][f][1]} -> {after[name][f][0]}/{after[name][f][1]}" for f in formats]
        print(f"{name:28s} " + "  ".join(f"{c:>13s}" for c in cells))
    print("\nas a table for core._BLOCK_MEASURED (luma, chroma, alpha at each anchor quality):")
    table = collections.defaultdict(dict)
    for (fmt, subsampled, q), rows in sorted(seen.items()):
        table[(fmt, subsampled)][q] = tuple(math.ceil(max(r[k] for r in rows) * 10) / 10 for k in range(3))
    for key, by_q in table.items():
        print(f"    {key!r}: {{{', '.join(f'{q}: {v}' for q, v in by_q.items())}}},")
    return 1 if tight else 0


if __name__ == "__main__":
    raise SystemExit(main())
