"""Test helper: surgically add a 'clap' (clean aperture) property to an AVIF."""

from __future__ import annotations

import struct


def _boxes(d: bytes, start: int, end: int):
    pos = start
    while pos < end:
        size, typ = struct.unpack_from(">I4s", d, pos)
        yield typ, pos, size
        pos += size


def add_clap(data: bytes, width: int, height: int, h_off: tuple[int, int], v_off: tuple[int, int]) -> bytes:
    """Return ``data`` with a clap box associated to the primary item.

    Rewrites the sizes of meta/iprp/ipco/ipma and shifts every absolute iloc
    offset that lies after the insertion point.
    """
    d = bytearray(data)
    clap = struct.pack(">I4s", 40, b"clap") + struct.pack(
        ">IIIIiIiI", width, 1, height, 1, h_off[0], h_off[1], v_off[0], v_off[1]
    )
    meta = next((p, s) for t, p, s in _boxes(d, 0, len(d)) if t == b"meta")
    mpos, msize = meta
    children = {t: (p, s) for t, p, s in _boxes(d, mpos + 12, mpos + msize)}
    iprp_pos, iprp_size = children[b"iprp"]
    sub = {t: (p, s) for t, p, s in _boxes(d, iprp_pos + 8, iprp_pos + iprp_size)}
    ipco_pos, ipco_size = sub[b"ipco"]
    ipma_pos, ipma_size = sub[b"ipma"]
    n_props = sum(1 for _ in _boxes(d, ipco_pos + 8, ipco_pos + ipco_size))
    pitm_pos, _ = children[b"pitm"]
    primary = (
        struct.unpack_from(">H", d, pitm_pos + 12)[0]
        if d[pitm_pos + 8] == 0
        else struct.unpack_from(">I", d, pitm_pos + 12)[0]
    )
    # 1. append an *essential* association to the primary item inside ipma
    #    (transformative properties must follow the descriptive ones).
    version, flags = d[ipma_pos + 8], int.from_bytes(d[ipma_pos + 9 : ipma_pos + 12], "big")
    count = struct.unpack_from(">I", d, ipma_pos + 12)[0]
    pos = ipma_pos + 16
    insert_at = None
    for _ in range(count):
        item = struct.unpack_from(">H" if version < 1 else ">I", d, pos)[0]
        pos += 2 if version < 1 else 4
        n = d[pos]
        if item == primary:
            d[pos] = n + 1
            insert_at = pos + 1 + n * (2 if flags & 1 else 1)
        pos += 1 + n * (2 if flags & 1 else 1)
    assert insert_at is not None
    assoc = struct.pack(">H", 0x8000 | (n_props + 1)) if flags & 1 else bytes([0x80 | (n_props + 1)])
    d[insert_at:insert_at] = assoc
    # 2. append the clap box at the end of ipco
    ipco_end = ipco_pos + ipco_size
    d[ipco_end:ipco_end] = clap
    delta = len(clap) + len(assoc)
    # 3. fix sizes (ipma moved by len(clap) since ipco precedes it)
    struct.pack_into(">I", d, ipco_pos, ipco_size + len(clap))
    struct.pack_into(">I", d, ipma_pos + len(clap), ipma_size + len(assoc))
    struct.pack_into(">I", d, iprp_pos, iprp_size + delta)
    struct.pack_into(">I", d, mpos, msize + delta)
    # 4. shift absolute iloc offsets
    iloc_pos, _ = children[b"iloc"]
    if iloc_pos > iprp_pos:
        iloc_pos += delta
    v = d[iloc_pos + 8]
    sizes, sizes2 = d[iloc_pos + 12], d[iloc_pos + 13]
    off_size, len_size, base_size = sizes >> 4, sizes & 15, sizes2 >> 4
    idx_size = sizes2 & 15 if v in (1, 2) else 0
    p = iloc_pos + 14
    count = struct.unpack_from(">H" if v < 2 else ">I", d, p)[0]
    p += 2 if v < 2 else 4
    for _ in range(count):
        p += 2 if v < 2 else 4
        method = 0
        if v in (1, 2):
            method = struct.unpack_from(">H", d, p)[0] & 15
            p += 2
        p += 2
        if base_size:
            base = int.from_bytes(d[p : p + base_size], "big")
            if method == 0 and base > mpos:
                d[p : p + base_size] = (base + delta).to_bytes(base_size, "big")
            p += base_size
        n_ext = struct.unpack_from(">H", d, p)[0]
        p += 2
        for _ in range(n_ext):
            p += idx_size
            off = int.from_bytes(d[p : p + off_size], "big")
            if method == 0 and off_size and off > mpos and not base_size:
                d[p : p + off_size] = (off + delta).to_bytes(off_size, "big")
            p += off_size + len_size
    return bytes(d)
