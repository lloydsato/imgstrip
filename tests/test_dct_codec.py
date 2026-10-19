"""The coefficient codec (imgstrip.dct): reads every JPEG variant exactly and writes one canonical form."""

import io

import numpy as np
import pytest
from PIL import Image

from imgstrip import dct, fixtures
from imgstrip.jpeg import JpegError


def make(w, h, mode="RGB", seed=0, **kw):
    arr = fixtures.photo(w, h)
    if seed:
        rng = np.random.default_rng(seed)
        arr = np.clip(arr.astype(int) + rng.integers(-9, 10, arr.shape), 0, 255).astype(np.uint8)
    im = Image.fromarray(arr)
    if mode != "RGB":
        im = im.convert(mode)
    buf = io.BytesIO()
    im.save(buf, "JPEG", **kw)
    return buf.getvalue()


def pixels(data):
    return np.asarray(Image.open(io.BytesIO(data)))


def canonical(frame_or_data):
    frame = dct.read(frame_or_data) if isinstance(frame_or_data, bytes) else frame_or_data
    return b"\xff\xd8" + dct.write(frame) + b"\xff\xd9"


SIZES = [(1, 1), (7, 9), (8, 8), (16, 16), (17, 13), (33, 40), (64, 48), (95, 71)]
CONFIGS = [
    {"quality": 90},
    {"quality": 75, "optimize": True},
    {"quality": 50, "subsampling": 0},
    {"quality": 95, "subsampling": 1, "optimize": True},
    {"quality": 85, "progressive": True},
    {"quality": 85, "progressive": True, "subsampling": 0},
    {"quality": 70, "restart_marker_blocks": 3},
    {"quality": 70, "restart_marker_rows": 1, "subsampling": 1},
    {"quality": 1},
    {"quality": 100, "subsampling": 0},
]


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("kw", CONFIGS, ids=lambda kw: ",".join(f"{k}={v}" for k, v in kw.items()))
def test_canonical_form_decodes_to_the_same_picture(size, kw):
    data = make(*size, seed=3, **kw)
    frame = dct.read(data)
    out = canonical(frame)
    assert np.array_equal(pixels(data), pixels(out))
    assert dct.visible_equal(dct.read(out), frame)


@pytest.mark.parametrize("mode", ["L", "CMYK"])
@pytest.mark.parametrize("size", [(8, 8), (17, 13), (64, 48)])
def test_grayscale_and_cmyk(mode, size):
    data = make(*size, mode=mode, seed=2, quality=88)
    out = canonical(data)
    assert np.array_equal(pixels(data), pixels(out))


def test_every_jpeg_fixture_round_trips(fx):
    seen = 0
    for name, data in fx.items():
        if not name.endswith(".jpg"):
            continue
        try:
            frame = dct.read(data)
        except JpegError:
            continue  # arithmetic-coded or otherwise outside the codec: refused, never guessed
        seen += 1
        out = canonical(frame)
        assert np.array_equal(pixels(data), pixels(out)), name
    assert seen >= 3


@pytest.mark.parametrize("quality", [30, 75, 90, 97])
@pytest.mark.parametrize("subsampling,size", [(0, (64, 48)), (1, (64, 48)), (2, (64, 48)), (2, (48, 32))])
def test_matches_libjpeg_optimised_huffman_coding_byte_for_byte(quality, subsampling, size):
    # With no blocks outside the picture the canonical form is exactly what libjpeg writes for optimised tables.
    data = make(*size, seed=5, quality=quality, subsampling=subsampling, optimize=True)
    assert canonical(data)[2:-2] == data[data.index(b"\xff\xdb") : -2]


def test_progressive_scan_scripts_with_refinement_are_decoded():
    data = make(120, 90, seed=4, quality=80, progressive=True)
    frame = dct.read(data)
    assert frame.progressive and frame.scans > 6  # DC first/refine, AC first/refine for each component
    assert np.array_equal(pixels(data), pixels(canonical(frame)))


def test_sixteen_bit_quantisation_tables():
    table = [int(300 + 7 * i) for i in range(64)]
    buf = io.BytesIO()
    Image.fromarray(fixtures.photo(40, 40)).save(buf, "JPEG", qtables=[table, table])
    data = buf.getvalue()
    assert b"\xff\xdb\x00\x83" in data  # 16-bit precision DQT
    out = canonical(data)
    assert np.array_equal(pixels(data), pixels(out))
    assert b"\xff\xc1" in out  # extended sequential: the only baseline-compatible way to carry 16-bit tables


def test_identical_tables_are_written_once_and_ids_are_canonical():
    data = make(48, 48, quality=80)
    frame = dct.read(data)
    frame.comps[1].qt = frame.comps[0].qt.copy()
    frame.comps[2].qt = frame.comps[0].qt.copy()
    out = dct.write(frame)
    assert out.count(b"\xff\xdb") == 1


def test_max_pixels_is_enforced_before_allocating():
    data = make(64, 64)
    with pytest.raises(JpegError, match="larger than"):
        dct.read(data, max_pixels=1000)


def test_second_image_after_the_first_is_ignored():
    data = make(40, 40, quality=80)
    assert dct.visible_equal(dct.read(data + data), dct.read(data))


# -- what the codec refuses ------------------------------------------------------------------------------------


def _patched(data, find, replace, count=1):
    assert data.count(find) >= 1
    return data.replace(find, replace, count)


@pytest.mark.parametrize(
    "marker,why",
    [(0xC9, "arithmetic"), (0xCA, "arithmetic"), (0xC3, "lossless"), (0xC5, "hierarchical"), (0xCF, "arithmetic")],
)
def test_unsupported_processes_are_refused(marker, why):
    data = make(32, 32, quality=80)
    sof = data.index(b"\xff\xc0")
    bad = data[: sof + 1] + bytes([marker]) + data[sof + 2 :]
    with pytest.raises(JpegError):
        dct.read(bad)


def test_twelve_bit_precision_is_refused():
    data = make(32, 32, quality=80)
    sof = data.index(b"\xff\xc0")
    bad = data[: sof + 4] + b"\x0c" + data[sof + 5 :]
    with pytest.raises(JpegError, match="12-bit"):
        dct.read(bad)


def test_missing_scan_for_a_component_is_refused():
    data = make(32, 32, quality=80, progressive=True)
    # drop the last scan: some component then lacks its AC data, but all have DC; truncating before the first
    # scan leaves components without any data
    first_sos = data.index(b"\xff\xda")
    with pytest.raises(JpegError):
        dct.read(data[:first_sos] + b"\xff\xd9")


def test_truncated_files_never_decode_to_something_else():
    data = make(48, 40, seed=1, quality=80)
    for cut in range(2, len(data) - 2, max(1, len(data) // 60)):
        with pytest.raises(JpegError):
            dct.read(data[:cut])


def test_coefficients_no_8_bit_image_can_have_are_refused_on_write():
    frame = dct.read(make(32, 32, quality=80))
    frame.comps[0].coef[0, 0, 5] = 1500
    with pytest.raises(JpegError, match="too large"):
        dct.write(frame)
    frame = dct.read(make(32, 32, quality=80))
    frame.comps[0].coef[0, 0, 0] = 30000
    with pytest.raises(JpegError, match="too large"):
        dct.write(frame)


def test_bit_flips_and_truncations_fail_cleanly():
    """Corrupt input must end in JpegError or in a decoded picture, never another exception or a hang."""
    rng = np.random.default_rng(11)
    sources = [
        make(40, 32, seed=1, quality=75),
        make(40, 32, seed=1, quality=75, progressive=True),
        make(40, 32, seed=1, quality=75, restart_marker_blocks=2),
        make(40, 32, mode="CMYK", quality=75),
    ]
    for data in sources:
        for _ in range(250):
            buf = bytearray(data)
            for _ in range(int(rng.integers(1, 4))):
                i = int(rng.integers(0, len(buf)))
                buf[i] ^= 1 << int(rng.integers(0, 8))
            if rng.random() < 0.2:
                del buf[int(rng.integers(2, len(buf))) :]
            try:
                frame = dct.read(bytes(buf), max_pixels=1_000_000)
                dct.write(frame)
            except JpegError:
                pass
