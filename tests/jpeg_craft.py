"""A tiny reference JPEG encoder with knobs for every place a payload can hide that is not a coefficient.

It encodes the same quantised coefficients of a grayscale picture in many different valid ways: another Huffman
table (any prefix code, any symbol order, unused symbols), an alias of the end-of-block symbol, zero runs spelled
out as ZRL tokens, restart markers, either padding bit value, extra unused tables, a 16-bit quantisation table,
another frame type and component id.  Every variant decodes to the same picture, which is exactly why the
canonical writer in ``imgstrip.dct`` has to make them byte-identical.
"""

from __future__ import annotations

import numpy as np

from imgstrip.dct import ZIGZAG


class _Bits:
    def __init__(self, pad_bit: int):
        self.out = bytearray()
        self.acc = 0
        self.n = 0
        self.pad_bit = pad_bit

    def put(self, value: int, length: int) -> None:
        for i in range(length - 1, -1, -1):
            self.acc = (self.acc << 1) | ((value >> i) & 1)
            self.n += 1
            if self.n == 8:
                self._byte(self.acc)
                self.acc = self.n = 0

    def _byte(self, b: int) -> None:
        self.out.append(b)
        if b == 0xFF:
            self.out.append(0)

    def align(self) -> None:
        while self.n:
            self.put(self.pad_bit, 1)


def _category(v: int) -> int:
    return abs(v).bit_length()


def _bits_of(v: int, size: int) -> int:
    return v if v >= 0 else v + (1 << size) - 1


def _segment(marker: int, payload: bytes) -> bytes:
    return bytes((0xFF, marker)) + (len(payload) + 2).to_bytes(2, "big") + payload


def _prefix_code(symbols: list[int], order: list[int] | None, rng: np.random.Generator, limit: int = 256):
    """A valid Huffman table over ``symbols`` plus unused ones: (counts per length, huffval, code map)."""
    extra = [s for s in range(limit) if s not in symbols]
    pool = list(symbols) + [int(x) for x in rng.choice(extra, size=min(len(extra), int(rng.integers(0, 12))), replace=False)]
    n = len(pool)
    length = max(1, int(np.ceil(np.log2(n + 1))))  # fixed length, never the all-ones code
    if order is not None:
        pool = [pool[i] for i in order if i < n] + [p for i, p in enumerate(pool) if i not in order]
    counts = [0] * 16
    counts[length - 1] = n
    codes = {sym: i for i, sym in enumerate(pool)}
    return counts, pool, {sym: (code, length) for sym, code in codes.items()}


def _optimal_like(symbols: list[int], rng: np.random.Generator):
    """A skewed prefix code (a few short codes, the rest longer) with the symbols in a shuffled order."""
    pool = list(symbols)
    rng.shuffle(pool)
    n = len(pool)
    counts = [0] * 16
    singles = min(3, n - 1)
    for length in range(1, singles + 1):
        counts[length - 1] = 1
    rest = n - singles
    if rest:
        counts[singles + max(1, int(np.ceil(np.log2(rest + 1)))) - 1] = rest
    codes, code, k = {}, 0, 0
    for length in range(1, 17):
        for _ in range(counts[length - 1]):
            codes[pool[k]] = (code, length)
            k += 1
            code += 1
        code <<= 1
    return counts, pool, codes


def craft(
    blocks: np.ndarray,
    bw: int,
    bh: int,
    qt: np.ndarray,
    *,
    seed: int = 0,
    table: str = "fixed",  # fixed | skewed
    alias_eob: bool = False,
    zrl_tail: bool = False,
    restart: int = 0,
    pad_bit: int = 1,
    extra_dqt: bool = False,
    extra_dht: bool = False,
    wide_dqt: bool = False,
    ext_sequential: bool = False,
    comp_id: int = 1,
    com: bytes | None = None,
    jfif: bool = True,
) -> bytes:
    """A grayscale baseline JPEG of ``bh`` x ``bw`` blocks holding exactly ``blocks`` (natural order, N x 64)."""
    rng = np.random.default_rng(seed)
    zz = np.asarray(blocks)[:, list(ZIGZAG)].astype(int)
    n = len(zz)
    # tokens
    dc_syms, ac_syms = set(), set()
    plan = []
    prev_dc = 0
    for i in range(n):
        if restart and i % restart == 0:
            prev_dc = 0
        diff = int(zz[i, 0]) - prev_dc
        prev_dc = int(zz[i, 0])
        toks = [("dc", _category(diff), diff)]
        dc_syms.add(_category(diff))
        run = 0
        last = max([k for k in range(1, 64) if zz[i, k]], default=0)
        for k in range(1, last + 1):
            v = int(zz[i, k])
            if v == 0:
                run += 1
                continue
            while run > 15:
                toks.append(("ac", 0xF0, None))
                ac_syms.add(0xF0)
                run -= 16
            sym = (run << 4) | _category(v)
            toks.append(("ac", sym, v))
            ac_syms.add(sym)
            run = 0
        tail = 63 - last
        if tail:
            if zrl_tail and tail >= 16:
                for _ in range(tail // 16):
                    toks.append(("ac", 0xF0, None))
                    ac_syms.add(0xF0)
                if tail % 16:
                    toks.append(("eob", 0x10 if alias_eob else 0, None))
                    ac_syms.add(0x10 if alias_eob else 0)
            else:
                toks.append(("eob", 0x10 if alias_eob else 0, None))
                ac_syms.add(0x10 if alias_eob else 0)
        plan.append(toks)
    if table == "skewed":
        dc_counts, dc_vals, dc_codes = _optimal_like(sorted(dc_syms), rng)
        ac_counts, ac_vals, ac_codes = _optimal_like(sorted(ac_syms), rng)
    else:
        dc_counts, dc_vals, dc_codes = _prefix_code(sorted(dc_syms), None, rng, limit=12)  # DC symbols are sizes
        ac_counts, ac_vals, ac_codes = _prefix_code(sorted(ac_syms), None, rng)

    bits = _Bits(pad_bit)
    data = bytearray()
    rst = 0
    for i, toks in enumerate(plan):
        if restart and i and i % restart == 0:
            bits.align()
            data += bits.out
            bits.out = bytearray()
            data += bytes((0xFF, 0xD0 + rst))
            rst = (rst + 1) % 8
        for kind, sym, v in toks:
            code, length = (dc_codes if kind == "dc" else ac_codes)[sym]
            bits.put(code, length)
            if kind == "dc" and sym:
                bits.put(_bits_of(v, sym), sym)
            elif kind == "ac" and v is not None:
                bits.put(_bits_of(v, sym & 15), sym & 15)
    bits.align()
    data += bits.out

    out = bytearray(b"\xff\xd8")
    if jfif:
        out += _segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    if com is not None:
        out += _segment(0xFE, com)
    zzq = np.asarray(qt)[list(ZIGZAG)].astype(int)
    if wide_dqt:
        out += _segment(0xDB, bytes((0x10,)) + b"".join(int(x).to_bytes(2, "big") for x in zzq))
    else:
        out += _segment(0xDB, bytes((0,)) + bytes(int(x) for x in zzq))
    if extra_dqt:
        out += _segment(0xDB, bytes((2,)) + bytes(int(x) for x in rng.integers(1, 255, 64)))
    frame = bytes((8,)) + (bh * 8).to_bytes(2, "big") + (bw * 8).to_bytes(2, "big") + bytes((1, comp_id, 0x11, 0))
    out += _segment(0xC1 if (wide_dqt or ext_sequential) else 0xC0, frame)
    out += _segment(0xC4, bytes((0x00,)) + bytes(dc_counts) + bytes(dc_vals))
    out += _segment(0xC4, bytes((0x10,)) + bytes(ac_counts) + bytes(ac_vals))
    if extra_dht:
        counts, vals, _ = _prefix_code(sorted(rng.choice(256, size=9, replace=False).tolist()), None, rng)
        out += _segment(0xC4, bytes((0x12,)) + bytes(counts) + bytes(vals))
    if restart:
        out += _segment(0xDD, restart.to_bytes(2, "big"))
    out += _segment(0xDA, bytes((1, comp_id, 0x00, 0, 63, 0)))
    out += data
    out += b"\xff\xd9"
    return bytes(out)


def random_blocks(n: int, seed: int = 0, density: float = 0.12) -> np.ndarray:
    """Natural-looking quantised blocks: a spread of DC values, sparse Laplacian AC coefficients."""
    rng = np.random.default_rng(seed)
    blocks = np.zeros((n, 64), dtype=np.int16)
    blocks[:, 0] = np.cumsum(rng.integers(-6, 7, n)) + 20
    decay = np.exp(-np.arange(64) / 14.0)
    mask = rng.random((n, 64)) < density * (0.2 + 4 * decay)
    mask[:, 0] = False
    mag = np.rint(rng.laplace(0, 2.2, (n, 64))).astype(np.int16)
    blocks[mask] = mag[mask]
    return blocks
