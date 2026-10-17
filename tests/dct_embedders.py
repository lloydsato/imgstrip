"""Reference JPEG steganography embedders and extractors, written for the tests.

Each one hides a known bit string in the quantised DCT coefficients of a ``dct.Frame`` the way a family of
real tools does, and reads it back from a frame.  They are deliberately small: what matters is which
coefficients carry the bits and how the bits are read (two's-complement parity, sign-magnitude parity, keyed
order, matrix/syndrome coding, changes to zeros), because that is what a defence has to destroy.

JSteg        sequential LSB replacement on AC coefficients other than 0 and 1.
Steghide     keyed random order, LSB replacement on every non-zero AC coefficient (never creates zeros).
OutGuess     keyed stride over coefficients other than 0 and 1 (without OutGuess's histogram correction).
F5           keyed order, matrix embedding (k=2), sign-magnitude parity, decrement only (with shrinkage).
Syndrome     J-UNIWARD-like: Hamming-coded +-1 changes over *all* AC coefficients, zeros included.

Every extractor returns the bits it reads; the tests compare them with what was embedded.
"""

from __future__ import annotations

import numpy as np

from imgstrip import dct

NAMES = ("jsteg", "steghide", "outguess", "f5", "syndrome")


def _luma_ac(frame: dct.Frame) -> np.ndarray:
    """Flat (blocks, 63) view of the visible luma AC coefficients, in raster block and zig-zag order."""
    c = frame.comps[0]
    vis = c.coef[: c.blocks_h, : c.blocks_w]
    return vis.reshape(-1, 64)[:, dct._ZIG][:, 1:]


def _store(frame: dct.Frame, ac: np.ndarray) -> None:
    c = frame.comps[0]
    shape = c.coef[: c.blocks_h, : c.blocks_w].shape
    full = np.zeros((ac.shape[0], 64), dtype=np.int16)
    vis = c.coef[: c.blocks_h, : c.blocks_w].reshape(-1, 64)
    full[:, 0] = vis[:, 0]
    full[:, 1:] = ac
    nat = np.empty_like(full)
    nat[:, dct._ZIG] = full
    c.coef[: c.blocks_h, : c.blocks_w] = nat.reshape(shape)


def _lsb_replace(values: np.ndarray, bits: np.ndarray) -> np.ndarray:
    return (values & np.int16(-2)) | bits.astype(np.int16)


# -- JSteg -----------------------------------------------------------------------------------------------


def capacity_jsteg(frame):
    ac = _luma_ac(frame)
    return int(((ac != 0) & (ac != 1)).sum())


def embed_jsteg(frame, bits, key=0):
    ac = _luma_ac(frame).copy()
    flat = ac.reshape(-1)
    carriers = np.nonzero((flat != 0) & (flat != 1))[0]
    assert len(bits) <= len(carriers)
    idx = carriers[: len(bits)]
    flat[idx] = _lsb_replace(flat[idx], np.asarray(bits))
    _store(frame, flat.reshape(ac.shape))


def extract_jsteg(frame, n, key=0):
    flat = _luma_ac(frame).reshape(-1)
    carriers = np.nonzero((flat != 0) & (flat != 1))[0]
    return (flat[carriers[:n]] & 1).astype(np.uint8)


# -- Steghide-like ---------------------------------------------------------------------------------------


def capacity_steghide(frame):
    return int((_luma_ac(frame) != 0).sum())


def embed_steghide(frame, bits, key=0):
    ac = _luma_ac(frame).copy()
    flat = ac.reshape(-1)
    carriers = np.nonzero(flat != 0)[0]
    perm = carriers[np.random.default_rng(key).permutation(len(carriers))][: len(bits)]
    assert len(bits) <= len(carriers)
    for pos, bit in zip(perm, bits, strict=True):
        c = int(flat[pos])
        if (c & 1) != bit:
            if abs(c) == 1:
                c += 1 if c > 0 else -1  # never create a zero
            else:
                c ^= 1
            flat[pos] = c
    _store(frame, flat.reshape(ac.shape))


def extract_steghide(frame, n, key=0):
    flat = _luma_ac(frame).reshape(-1)
    carriers = np.nonzero(flat != 0)[0]
    perm = carriers[np.random.default_rng(key).permutation(len(carriers))][:n]
    return (flat[perm] & 1).astype(np.uint8)


# -- OutGuess-like ---------------------------------------------------------------------------------------


def capacity_outguess(frame):
    return capacity_jsteg(frame) // 2


def _stride_order(count, n, key):
    rng = np.random.default_rng(key)
    steps = rng.integers(1, max(2, count // max(1, n)) + 1, size=n)  # n <= count / 2 keeps the walk inside
    pos = np.cumsum(steps) - 1
    return pos[pos < count]


def embed_outguess(frame, bits, key=0):
    ac = _luma_ac(frame).copy()
    flat = ac.reshape(-1)
    carriers = np.nonzero((flat != 0) & (flat != 1))[0]
    order = carriers[_stride_order(len(carriers), len(bits), key)]
    assert len(order) == len(bits), "payload too large for the stride walk"
    flat[order] = _lsb_replace(flat[order], np.asarray(bits))
    _store(frame, flat.reshape(ac.shape))


def extract_outguess(frame, n, key=0):
    flat = _luma_ac(frame).reshape(-1)
    carriers = np.nonzero((flat != 0) & (flat != 1))[0]
    order = carriers[_stride_order(len(carriers), n, key)]
    return (flat[order] & 1).astype(np.uint8)


# -- F5 (matrix embedding k=2, shrinkage) ------------------------------------------------------------------


def capacity_f5(frame):
    return 2 * (int((_luma_ac(frame) != 0).sum()) // 3) // 2


def _f5_bit(c):
    return (c & 1) if c > 0 else 1 - (c & 1)


def _f5_hash(group):
    h = 0
    for i, c in enumerate(group, 1):
        if _f5_bit(c):
            h ^= i
    return h


def embed_f5(frame, bits, key=0):
    ac = _luma_ac(frame).copy()
    flat = ac.reshape(-1)
    perm = np.random.default_rng(key).permutation(flat.size)
    walk = [int(p) for p in perm if flat[p] != 0]
    bits = [int(b) for b in bits] + [0] * (-len(bits) % 2)
    i = 0
    for m in range(0, len(bits), 2):
        want = bits[m] * 2 + bits[m + 1]
        while True:
            group = walk[i : i + 3]
            if len(group) < 3:
                raise ValueError("payload too large")
            h = _f5_hash([int(flat[p]) for p in group])
            s = want ^ h
            if s == 0:
                i += 3
                break
            pos = group[s - 1]
            flat[pos] += -1 if flat[pos] > 0 else 1  # decrement the magnitude
            if flat[pos] == 0:  # shrinkage: the group is invalid, retry without this coefficient
                walk.pop(i + s - 1)
                continue
            i += 3
            break
    _store(frame, flat.reshape(ac.shape))


def extract_f5(frame, n, key=0):
    flat = _luma_ac(frame).reshape(-1)
    perm = np.random.default_rng(key).permutation(flat.size)
    walk = [int(p) for p in perm if flat[p] != 0]
    out = []
    for i in range(0, 3 * ((n + 1) // 2), 3):
        h = _f5_hash([int(flat[p]) for p in walk[i : i + 3]])
        out += [h >> 1, h & 1]
    return np.array(out[:n], dtype=np.uint8)


# -- J-UNIWARD-like syndrome coding over every AC coefficient ----------------------------------------------

_H = np.array([[0, 0, 0, 1, 1, 1, 1], [0, 1, 1, 0, 0, 1, 1], [1, 0, 1, 0, 1, 0, 1]], dtype=np.uint8)  # Hamming(7,4)


def capacity_syndrome(frame):
    return 3 * (_luma_ac(frame).size // 7) // 4


def embed_syndrome(frame, bits, key=0):
    ac = _luma_ac(frame).copy()
    flat = ac.reshape(-1)
    rng = np.random.default_rng(key)
    perm = rng.permutation(flat.size)
    bits = np.asarray(list(bits) + [0] * (-len(bits) % 3), dtype=np.uint8)
    signs = rng.choice((-1, 1), size=len(bits) // 3)
    for g, m in enumerate(range(0, len(bits), 3)):
        pos = perm[7 * g : 7 * g + 7]
        x = (flat[pos] & 1).astype(np.uint8)
        syndrome = (_H @ x) % 2
        diff = syndrome ^ bits[m : m + 3]
        if diff.any():
            col = int(np.nonzero((_H.T == diff).all(axis=1))[0][0])
            flat[pos[col]] += signs[g]  # +-1: zeros become +-1, the support changes
    _store(frame, flat.reshape(ac.shape))


def extract_syndrome(frame, n, key=0):
    flat = _luma_ac(frame).reshape(-1)
    rng = np.random.default_rng(key)
    perm = rng.permutation(flat.size)
    out = []
    for g in range(0, -(-n // 3)):
        pos = perm[7 * g : 7 * g + 7]
        out += list((_H @ (flat[pos] & 1).astype(np.uint8)) % 2)
    return np.array(out[:n], dtype=np.uint8)


def carrier_positions(frame, name, n, key=0):
    """Flat indices (into the visible luma AC array) that carry the first ``n`` bits, for the direct embedders.

    This is what an attacker who knows the tool, the key and the cover's carrier set would read; the supervised
    attacker in the tests gets it for free to make the test as strong as it can be.
    """
    flat = _luma_ac(frame).reshape(-1)
    if name == "jsteg":
        return np.nonzero((flat != 0) & (flat != 1))[0][:n]
    if name == "steghide":
        carriers = np.nonzero(flat != 0)[0]
        return carriers[np.random.default_rng(key).permutation(len(carriers))][:n]
    if name == "outguess":
        carriers = np.nonzero((flat != 0) & (flat != 1))[0]
        return carriers[_stride_order(len(carriers), n, key)]
    raise ValueError(name)


def values_at(frame, positions):
    return _luma_ac(frame).reshape(-1)[positions]


EMBEDDERS = {
    "jsteg": (embed_jsteg, extract_jsteg, capacity_jsteg),
    "steghide": (embed_steghide, extract_steghide, capacity_steghide),
    "outguess": (embed_outguess, extract_outguess, capacity_outguess),
    "f5": (embed_f5, extract_f5, capacity_f5),
    "syndrome": (embed_syndrome, extract_syndrome, capacity_syndrome),
}


def make_stego(jpeg_bytes: bytes, name: str, n_bits: int, seed: int = 7):
    """(stego JPEG bytes, payload bits) for the named embedder, with ``n_bits`` random payload bits."""
    embed, _extract, capacity = EMBEDDERS[name]
    frame = dct.read(jpeg_bytes)
    n = min(n_bits, capacity(frame))
    bits = np.random.default_rng(seed).integers(0, 2, size=n, dtype=np.uint8)
    embed(frame, bits, key=seed)
    return b"\xff\xd8" + dct.write(frame) + b"\xff\xd9", bits


def read_back(jpeg_bytes: bytes, name: str, bits: np.ndarray, seed: int = 7) -> float:
    """Bit error rate of the named extractor on this JPEG against the embedded bits (0.0 = payload intact)."""
    _embed, extract, _cap = EMBEDDERS[name]
    got = extract(dct.read(jpeg_bytes), len(bits), key=seed)
    if len(got) < len(bits):
        got = np.concatenate([got, np.zeros(len(bits) - len(got), dtype=np.uint8)])
    return float(np.mean(got != bits))
