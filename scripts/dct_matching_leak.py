#!/usr/bin/env python3
"""Finding F5: why a +-1 (LSB-matching) payload keeps a bit error rate above 0.5 after --scrub-dct.

The review read a keyed +-1 payload back at its true carrier positions after --scrub-dct and measured a bit
error rate of 0.562 (0.5 is chance).  This script reproduces that experiment, splits the leak by where it
comes from, and measures candidate fixes, so the decision recorded in docs/REFERENCE.md can be checked:

    python scripts/dct_matching_leak.py            # the full table (about a minute)
    python scripts/dct_matching_leak.py --quick    # fewer covers

The embedder is the review's: keyed +-1 LSB matching over luma AC coefficients with |c| >= 1, a value moved by
+-1 at random, away from zero when |c| = 1, the bit read as the two's-complement low bit.  Covers are 256 x 256
crops of the scikit-image sample photographs at JPEG quality 75 and 90, 4:2:0, with payloads of 25 % and 100 % of
the usable coefficients (the review's set-up).  Needs scikit-image.

What it shows: the scrub maps the pairs {-2, -1} and {0, 1} to 0 and keeps every other value non-zero.  A +-1
change at the edge of that zero zone (1 <-> 2, -2 <-> -3) therefore decides whether the carrier is zero after the
scrub, and that depends on the bit: a carrier at +1 that must hold 1 stays at 1 and is zeroed, one that must hold
0 moves to 2 and survives.  A parity reader sees the deterministic 0 of a zeroed carrier, so its errors are
correlated with the payload.  Carriers away from the edge read as noise.
"""

from __future__ import annotations

import argparse
import io
import math
import zlib

import numpy as np
from PIL import Image

from imgstrip import dct

PHOTOS = ("astronaut", "coffee", "chelsea", "rocket", "hubble_deep_field", "immunohistochemistry", "retina")


def covers(quick: bool):
    import skimage.data

    rng = np.random.default_rng(31337)
    for name in PHOTOS[: 3 if quick else None]:
        img = getattr(skimage.data, name)()
        for quality in (75, 90):
            y = int(rng.integers(0, img.shape[0] - 256 + 1))
            x = int(rng.integers(0, img.shape[1] - 256 + 1))
            buf = io.BytesIO()
            Image.fromarray(img[y : y + 256, x : x + 256]).save(buf, "JPEG", quality=quality, subsampling=2)
            yield f"{name} q{quality}", buf.getvalue()


def luma_ac(frame: dct.Frame) -> np.ndarray:
    """Flat view of the visible luma AC coefficients, block by block in zig-zag order (a copy)."""
    c = frame.comps[0]
    return c.coef[: c.blocks_h, : c.blocks_w].reshape(-1, 64)[:, dct._ZIG][:, 1:].reshape(-1).copy()


def embed_matching(frame: dct.Frame, fraction: float, rng: np.random.Generator):
    """Keyed +-1 matching into the luma AC coefficients of ``frame`` (in place): (positions, bits)."""
    c = frame.comps[0]
    vis = c.coef[: c.blocks_h, : c.blocks_w].reshape(-1, 64)
    zz = vis[:, dct._ZIG]
    ac = zz[:, 1:].reshape(-1)
    usable = np.flatnonzero(ac != 0)
    pos = rng.permutation(usable)[: int(len(usable) * fraction)]
    bits = rng.integers(0, 2, len(pos)).astype(np.int16)
    vals = ac[pos].astype(np.int16)
    wrong = (vals & 1) != bits
    step = np.where(np.abs(vals) == 1, np.sign(vals), rng.choice(np.array([-1, 1], dtype=np.int16), len(pos)))
    ac[pos] = np.where(wrong, vals + step, vals)
    zz[:, 1:] = ac.reshape(zz.shape[0], 63)
    vis[:, dct._ZIG] = zz
    c.coef[: c.blocks_h, : c.blocks_w] = vis.reshape(c.coef[: c.blocks_h, : c.blocks_w].shape)
    return pos, bits


def scrub_variant(frame: dct.Frame, rng: np.random.Generator, edge_zero: float) -> None:
    """dct.scrub's rule, plus: values in the pairs next to the zero zone ({2, 3} and {-4, -3}) are also sent to 0
    with probability ``edge_zero`` (0 is the shipped scrub, 1 widens the zero zone to {-4 .. 3})."""
    for c in frame.comps:
        o = c.coef.astype(np.int32)
        pair = o >> 1
        n = np.where((pair == 0) | (pair == -1), 0, (o & -2) | rng.integers(0, 2, o.shape))
        if edge_zero:
            n = np.where(((pair == 1) | (pair == -2)) & (rng.random(o.shape) < edge_zero), 0, n)
        n[..., 1:] = np.maximum(n[..., 1:], -1023)
        c.coef = n.astype(np.int16)


def jpeg_bytes(frame: dct.Frame) -> bytes:
    return b"\xff\xd8" + dct.write(frame) + b"\xff\xd9"


def decoded(data: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=np.float64)


def capacity(ber: float) -> float:
    """Capacity in bits per carrier of a binary symmetric channel with this error rate (the review's measure)."""
    p = min(max(ber, 1e-12), 1 - 1e-12)
    return 1 + p * math.log2(p) + (1 - p) * math.log2(1 - p)


def mutual_information(bits: np.ndarray, values: np.ndarray) -> float:
    """I(bit; scrubbed value) in bits per carrier, plug-in estimate with the Miller-Madow correction.

    This is what a receiver who knows every carrier position can learn at best from the value it reads, whatever
    rule it reads it with; the parity reader of the BER column is one such rule.  Values beyond 12 in magnitude
    are binned with their parity kept, as the test suite's supervised attacker does.
    """
    v = values.astype(np.int64)
    v = np.where(np.abs(v) > 12, np.sign(v) * (13 + (np.abs(v) & 1)), v)
    joint = np.zeros((2, 29))
    np.add.at(joint, (bits.astype(np.int64), v + 14), 1)
    n = joint.sum()

    def entropy(counts):
        p = counts[counts > 0] / n
        return float(-(p * np.log2(p)).sum()) + (np.count_nonzero(counts) - 1) / (2 * n * math.log(2))

    return max(0.0, entropy(joint.sum(1)) + entropy(joint.sum(0)) - entropy(joint))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--quick", action="store_true", help="three photographs instead of seven")
    args = parser.parse_args()
    variants = {"shipped scrub": 0.0, "edge zeroed p=0.5": 0.5, "edge zeroed p=1": 1.0}
    rows = {v: {"ber": [], "zeroed": [], "ber_zeroed": [], "ber_kept": [], "psnr": [], "extra": []} for v in variants}
    pooled = {v: ([], []) for v in variants}
    for label, cover in covers(args.quick):
        for fraction in (0.25, 1.0):
            rng = np.random.default_rng(zlib.crc32(f"{label} {fraction}".encode()))
            frame = dct.read(cover)
            pos, bits = embed_matching(frame, fraction, rng)
            stego = jpeg_bytes(frame)
            reference = decoded(stego)
            shipped_psnr = None
            for name, edge_zero in variants.items():
                out = dct.read(stego)
                scrub_variant(out, np.random.default_rng(rng.integers(1 << 32)), edge_zero)
                data = jpeg_bytes(out)
                got = luma_ac(dct.read(data))[pos]
                wrong = (got & 1) != bits
                zeroed = got == 0
                mse = float(np.mean((decoded(data) - reference) ** 2))
                psnr = 10 * math.log10(255.0**2 / mse) if mse else float("inf")
                shipped_psnr = psnr if shipped_psnr is None else shipped_psnr
                r = rows[name]
                r["ber"].append(float(wrong.mean()))
                r["zeroed"].append(float(zeroed.mean()))
                r["ber_zeroed"].append(float(wrong[zeroed].mean()) if zeroed.any() else float("nan"))
                r["ber_kept"].append(float(wrong[~zeroed].mean()) if (~zeroed).any() else float("nan"))
                r["psnr"].append(psnr)
                r["extra"].append(shipped_psnr - psnr)
                pooled[name][0].append(bits)
                pooled[name][1].append(got)
            print(f"  {label}, payload {fraction:.0%}: {len(bits)} carriers", flush=True)
    print()
    print(
        "variant              parity BER [min, max]  BSC capacity  zeroed  BER zeroed  BER kept  "
        "I(bit; value)  PSNR (dB)  extra cost (dB)"
    )
    for name, r in rows.items():
        ber = np.array(r["ber"])
        mi = mutual_information(np.concatenate(pooled[name][0]), np.concatenate(pooled[name][1]))
        print(
            f"{name:20s} {ber.mean():.3f} [{ber.min():.3f}, {ber.max():.3f}]  {capacity(ber.mean()):.4f}        "
            f"{np.mean(r['zeroed']):.2f}    {np.nanmean(r['ber_zeroed']):.3f}       {np.nanmean(r['ber_kept']):.3f}     "
            f"{mi:.4f}         {np.median(r['psnr']):5.1f}      {np.median(r['extra']):.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
