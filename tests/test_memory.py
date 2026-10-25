"""Memory: big pictures are worked through in strips of rows and bands of blocks, never as many whole copies.

Two kinds of test.  The bounds catch a return to whole-image working arrays; each notes what the earlier
whole-image code needed, and they leave a wide margin above what the strip code uses.  The equivalence tests
show that the strips change nothing: whatever their size, the outputs and statistics are the same to the bit.
"""

import io
import os
import subprocess
import sys
import tracemalloc
import zlib

import numpy as np
import pytest
from PIL import Image

from imgstrip import analysis, core, dct, fixtures, inspect
from imgstrip.core import Options, strip_image


def jpeg(arr, **kw) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG", **kw)
    return buf.getvalue()


def traced_peak(fn, *args) -> int:
    tracemalloc.start()
    try:
        fn(*args)
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


# --------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------

_CLEAN_ONE_PHOTO = """
import io, sys
import numpy as np
from PIL import Image
from imgstrip import sandbox
from imgstrip.core import Options, strip_image
w, h, fmt = 3000, 2000, sys.argv[1]
small = np.random.default_rng(0).integers(0, 256, (h // 50 + 2, w // 50 + 2, 3), dtype=np.uint8)
exif = Image.Exif()
exif[0x0112] = 6  # rotated, as phone photos usually are
buf = io.BytesIO()
Image.fromarray(small).resize((w, h), Image.BICUBIC).save(buf, fmt, **({"quality": 90, "exif": exif} if fmt == "JPEG" else {}))
data = buf.getvalue()
sandbox.probe_limits("/nonexistent", "/nonexistent")  # the zygote is started before the measurement
with open("/proc/self/clear_refs", "w") as fh:
    fh.write("5")  # resets the peak resident size to the current one
status = lambda key: int(open("/proc/self/status").read().split(key + ":")[1].split()[0])
base = status("VmRSS")
strip_image(data, Options(sandbox=sys.argv[2] == "on"))
print((status("VmHWM") - base) * 1024 / (w * h * 3), (sandbox.last_peak() or 0) / (w * h * 3))
"""


@pytest.mark.skipif(not os.access("/proc/self/clear_refs", os.W_OK), reason="needs Linux peak-RSS accounting")
@pytest.mark.parametrize("sandboxed", ["on", "off"])
@pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
def test_cleaning_a_photo_needs_a_few_copies_of_it(fmt, sandboxed):
    """Decode, rotate, encode and verify a 6 MP photo: the whole-image code peaked at about 8.5 times its pixels.

    With the sandbox the decode stage's peak is in its worker, which has exited by the time this process encodes, so
    the two peaks do not add up; each is bounded on its own."""
    out = subprocess.run(
        [sys.executable, "-c", _CLEAN_ONE_PHOTO, fmt, sandboxed], capture_output=True, text=True, check=True
    )
    here, worker = map(float, out.stdout.split())
    assert here < 6.0, out.stdout
    assert worker < 3.0 if sandboxed == "on" else worker == 0, out.stdout


def test_the_canonical_jpeg_writer_works_in_bands():
    """Pure noise is the writer's worst case; the whole-picture writer needed about 340 bytes per pixel."""
    frame = dct.read(jpeg(np.random.default_rng(0).integers(0, 256, (600, 800, 3), dtype=np.uint8), quality=95))
    assert traced_peak(dct.write, frame) < 100 * 600 * 800


def test_the_deep_scan_works_in_strips():
    """Both LSB tests over a 3 MP channel; the whole-image code needed about 95 bytes per pixel."""
    channel = np.random.default_rng(0).integers(0, 256, (1500, 2000), dtype=np.uint8)
    assert traced_peak(analysis.lsb_payload_estimate, channel) < 12 * channel.size


# --------------------------------------------------------------------------
# The DCT writer: bands, and the bit packer
# --------------------------------------------------------------------------


def _jpegs() -> dict[str, bytes]:
    rng = np.random.default_rng(3)
    photo = fixtures.photo(161, 97, 2)
    out = {f"photo s{s}": jpeg(photo, quality=90, subsampling=s) for s in (0, 1, 2)}
    out["noise"] = jpeg(rng.integers(0, 256, (33, 47, 3), dtype=np.uint8), quality=98, subsampling=0)
    out["grey"] = jpeg(photo[..., 0], quality=85)
    out["progressive"] = jpeg(photo, quality=88, progressive=True)
    out["tiny"] = jpeg(photo[:5, :3], quality=75)
    out["one MCU row wider than a band"] = jpeg(fixtures.photo(11000, 24, 4), quality=80)
    return out


@pytest.mark.parametrize("name", list(_jpegs()))
def test_the_band_size_never_changes_the_canonical_bytes(monkeypatch, name):
    data = _jpegs()[name]
    monkeypatch.setattr(dct, "_WRITE_BAND_BLOCKS", 10**9)  # one band: the whole picture at once
    whole = dct.write(dct.read(data))
    for blocks in (1, 7, 64, 4096):
        monkeypatch.setattr(dct, "_WRITE_BAND_BLOCKS", blocks)
        assert dct.write(dct.read(data)) == whole, blocks


def test_the_bit_packer_matches_a_bit_string_reference():
    rng = np.random.default_rng(5)
    for _ in range(200):
        n = int(rng.integers(1, 400))
        lens = rng.integers(1, 33, n)
        vals = np.array([int(rng.integers(0, 1 << int(k))) for k in lens], dtype=np.uint64)
        bits = "".join(format(int(v), f"0{int(k)}b") for v, k in zip(vals, lens, strict=True))
        cuts = sorted({0, n, *rng.integers(0, n + 1, 3).tolist()})
        out, carry, carry_len = b"", 0, 0
        for a, b in zip(cuts, cuts[1:], strict=False):
            if a == b:
                continue
            packed, carry, carry_len = dct._pack(vals[a:b], lens[a:b], carry, carry_len)
            out += packed
        assert len(out) == len(bits) // 8 and carry_len == len(bits) % 8
        assert out == int(bits[: len(out) * 8] or "0", 2).to_bytes(len(out), "big")
        assert carry == (int(bits[len(out) * 8 :], 2) if carry_len else 0)


# --------------------------------------------------------------------------
# The deep scan: strips
# --------------------------------------------------------------------------


@pytest.mark.parametrize("shape", [(1, 300), (2, 200), (3, 3), (97, 61), (300, 1), (240, 320)])
def test_the_strip_size_never_changes_the_lsb_statistics(monkeypatch, shape):
    channel = fixtures.photo(shape[1], shape[0], 2)[..., 1] if min(shape) > 1 else np.arange(shape[0] * shape[1])
    channel = channel.astype(np.uint8).reshape(shape)
    monkeypatch.setattr(analysis, "_STRIP_PIXELS", 1 << 40)  # one strip
    whole = analysis._channel_stats(channel)
    for pixels in (1, 7, 500):
        monkeypatch.setattr(analysis, "_STRIP_PIXELS", pixels)
        strips = analysis._channel_stats(channel)
        assert (strips.pairs, strips.x, strips.z, strips.w) == (whole.pairs, whole.x, whole.z, whole.w)
        assert strips.spa() == whole.spa()
        assert strips.ws() == pytest.approx(whole.ws(), rel=1e-12, abs=1e-15)


def test_the_integer_ws_terms_are_the_floating_point_formula_to_the_bit():
    def reference(s):
        s = s.astype(np.float64)
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
        sign = np.where(p % 2 == 0, -1.0, 1.0)
        return float(np.sum(weights * sign * (p - pred))), float(weights.sum())

    rng = np.random.default_rng(9)
    for trial in range(60):
        h, w = int(rng.integers(3, 80)), int(rng.integers(3, 200))
        block = rng.integers(0, 256, (h, w), dtype=np.uint8)
        if trial % 2:
            block = rng.choice(np.array([0, 1, 254, 255], np.uint8), (h, w))  # saturated: zero weights
        assert analysis._ws_sums(block) == reference(block)


# --------------------------------------------------------------------------
# The cleaning pipeline: strips
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "options",
    [Options(), Options(format="PNG"), Options(format="GIF"), Options(format="WEBP"), Options(icc="sanitize")],
    ids=["default", "png", "gif", "webp", "icc"],
)
def test_one_row_strips_give_the_same_cleaned_files(monkeypatch, in_process, fx, options):
    names = [n for n in fx if n.endswith((".png", ".gif", ".webp", ".tif", ".bmp"))] + ["jpeg_everything.jpg"]
    expected = {}
    for name in names:
        try:
            expected[name] = strip_image(fx[name], options).data
        except core.StripError as exc:
            expected[name] = str(exc)
    monkeypatch.setattr(core, "_STRIP_BYTES", 1)  # every check and comparison one row at a time
    for name in names:
        try:
            got = strip_image(fx[name], options).data
        except core.StripError as exc:
            got = str(exc)
        assert got == expected[name], name


def test_the_jpeg_coefficient_modes_compare_in_strips(monkeypatch, in_process, fx):
    data = fx["jpeg_everything.jpg"]
    canonical = strip_image(data, Options(scrub_dct="canonical")).data
    monkeypatch.setattr(core, "_STRIP_BYTES", 1)
    assert strip_image(data, Options(scrub_dct="canonical")).data == canonical
    assert strip_image(data, Options(jpeg_lossless=True)).pixels_exact
    assert strip_image(data, Options(scrub_dct="full", dct_min_psnr=0)).dct_psnr is not None


# --------------------------------------------------------------------------
# The inspector: GIF codes and PNG pixel streams
# --------------------------------------------------------------------------


def _table_walk(min_code: int, data: bytes) -> int | None:
    """The used indices by building the full LZW table with each entry's index set (the earlier implementation)."""
    clear, eoi = 1 << min_code, (1 << min_code) + 1
    first = list(range(clear)) + [0, 0]
    seen = [1 << i for i in range(clear)] + [0, 0]
    width, used, acc, bits, prev = min_code + 1, 0, 0, 0, None
    for byte in data:
        acc |= byte << bits
        bits += 8
        while bits >= width:
            code = acc & ((1 << width) - 1)
            acc >>= width
            bits -= width
            if code == clear:
                del first[clear + 2 :]
                del seen[clear + 2 :]
                width, prev = min_code + 1, None
                continue
            if code == eoi:
                return used
            if prev is None:
                if code >= clear:
                    return None
                used |= seen[code]
                prev = code
                continue
            if code < len(first):
                head, mask = first[code], seen[code]
            elif code == len(first):
                head, mask = first[prev], seen[prev] | (1 << first[prev])
            else:
                return None
            if len(first) < 4096:
                first.append(first[prev])
                seen.append(seen[prev] | (1 << head))
            used |= mask
            prev = code
            if len(first) == 1 << width and width < 12:
                width += 1
    return used


def test_the_literal_codes_name_the_same_indices_as_the_full_table_even_in_corrupt_streams():
    rng = np.random.default_rng(13)
    for trial in range(40):
        k = int(rng.integers(2, 257))
        w, h = (300, 200) if trial % 8 == 0 else (int(rng.integers(1, 70)), int(rng.integers(1, 70)))
        idx = rng.integers(0, k, (h, w)).astype(np.uint8)
        if trial % 3 == 0:
            idx = np.sort(idx, axis=1)
        im = Image.fromarray(idx, "P")
        im.putpalette(rng.integers(0, 256, 768).astype(np.uint8).tolist())
        buf = io.BytesIO()
        im.save(buf, "GIF", optimize=False)
        d = buf.getvalue()
        pos = 13 + (3 << ((d[10] & 7) + 1) if d[10] & 0x80 else 0)
        while d[pos] == 0x21:
            pos += 2
            while d[pos]:
                pos += d[pos] + 1
            pos += 1
        flags = d[pos + 9]
        pos += 10 + (3 << ((flags & 7) + 1) if flags & 0x80 else 0)
        data, _ = inspect._gif_subblocks(d, pos + 1)
        streams = [data, data[: len(data) // 3], rng.integers(0, 256, 500, dtype=np.uint8).tobytes()]
        for _ in range(10):
            b = bytearray(data)
            b[int(rng.integers(0, len(b)))] ^= 1 << int(rng.integers(0, 8))
            streams.append(bytes(b))
        for stream in streams:
            for min_code in (d[pos], int(rng.integers(1, 9))):
                assert inspect._gif_used_indices(min_code, stream) == _table_walk(min_code, stream)


def test_the_pixel_stream_is_measured_the_same_in_pieces_as_whole():
    rng = np.random.default_rng(2)
    raw = rng.integers(0, 256, 300_000, dtype=np.uint8).tobytes()
    for extra, after in ((b"", b""), (b"", b"TRAILING" * 20_000), (b"x" * 9000, b"")):
        stream = zlib.compress(raw + extra, 6) + after
        for limit in (len(raw), len(raw) // 2):
            whole = inspect._inflate_measure(stream, limit)
            for sizes in ([1] * 3000 + [len(stream)], [7, 65535, 65537, 3, len(stream)], [len(stream)]):
                pieces, pos = [], 0
                for size in sizes:
                    pieces.append(memoryview(stream)[pos : pos + size])
                    pos += size
                assert inspect._inflate_measure(pieces, limit) == whole, (extra, len(after), limit, sizes[:3])
