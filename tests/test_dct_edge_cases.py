"""Edge cases for the coefficient codec, the canonical form and the scrub.

Geometry (one pixel, one MCU, odd sizes, extreme aspect ratios, every sampling layout), content (all-zero,
dense, extreme magnitudes, runs that straddle the ZRL boundary, a coefficient in the last position), file
structure (restart intervals, fill bytes, redefined tables, truncation, impossible headers) and the cleaning
pipeline around them (orientation, ICC, RGB and CMYK JPEGs, multi-picture files, size limits, refusals).
"""

import io
import struct

import numpy as np
import pytest
from PIL import Image

import jpeg_craft
from conftest import assert_clean
from imgstrip import Options, StripError, dct, fixtures, inspect_bytes, strip_image
from imgstrip.jpeg import JpegError
from test_dct_canonical import QT
from test_dct_codec import canonical, make, pixels

FULL = Options(scrub_dct="full", dct_min_psnr=0)
CANON = Options(scrub_dct="canonical")


def random_frame(width, height, sampling, seed=0, density=0.12):
    """A frame with sparse random coefficients in any sampling layout (what a camera or a tool could produce)."""
    rng = np.random.default_rng(seed)
    if len(sampling) == 1:
        sampling = [(1, 1)]
    hmax = max(h for h, _ in sampling)
    vmax = max(v for _, v in sampling)
    mcus_x, mcus_y = -(-width // (8 * hmax)), -(-height // (8 * vmax))
    comps = []
    for i, (h, v) in enumerate(sampling):
        cw, ch = -(-width * h // hmax), -(-height * v // vmax)
        coef = jpeg_craft.random_blocks(mcus_y * v * mcus_x * h, seed=seed + i, density=density)
        comps.append(
            dct.Component(
                ident=i + 1,
                h=h,
                v=v,
                qt=np.clip(1 + (np.arange(64) * (i + 2)) % 13, 1, 255).astype(np.uint16),
                blocks_w=-(-cw // 8),
                blocks_h=-(-ch // 8),
                coef=coef.reshape(mcus_y * v, mcus_x * h, 64),
            )
        )
    del rng
    return dct.Frame(width, height, comps, mcus_x, mcus_y)


def written(frame):
    return b"\xff\xd8" + dct.write(frame) + b"\xff\xd9"


# -- geometry ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("subsampling", [0, 1, 2])
@pytest.mark.parametrize(
    "size",
    [(1, 1), (1, 2), (2, 1), (1, 200), (200, 1), (3, 3), (8, 1), (1, 8), (9, 9), (15, 15), (16, 16), (17, 17),
     (31, 33), (32, 32), (4000, 2), (2, 1500), (257, 3)],
)  # fmt: skip
def test_extreme_and_boundary_sizes(size, subsampling):
    data = make(*size, seed=2, quality=80, subsampling=subsampling)
    out = canonical(data)
    assert np.array_equal(pixels(data), pixels(out))
    full = strip_image(data, FULL)
    assert full.size == size and pixels(full.data).shape[:2] == (size[1], size[0])


SAMPLINGS = [
    [(1, 1), (1, 1), (1, 1)],  # 4:4:4
    [(2, 1), (1, 1), (1, 1)],  # 4:2:2
    [(2, 2), (1, 1), (1, 1)],  # 4:2:0
    [(1, 2), (1, 1), (1, 1)],  # 4:4:0
    [(4, 1), (1, 1), (1, 1)],  # 4:1:1
    [(4, 2), (1, 1), (1, 1)],
    [(2, 2), (2, 1), (1, 1)],
    [(1, 1)],  # grey
    [(2, 2)],  # grey that states a sampling factor
    [(1, 1), (1, 1), (1, 1), (1, 1)],  # CMYK
    [(2, 2), (1, 1), (1, 1), (2, 2)],  # ten blocks per MCU: the interleaved limit
]


@pytest.mark.parametrize("sampling", SAMPLINGS, ids=lambda s: "-".join(f"{h}x{v}" for h, v in s))
@pytest.mark.parametrize("size", [(1, 1), (8, 8), (17, 23), (64, 40), (100, 75)])
def test_every_sampling_layout_survives_writing_and_reading(sampling, size):
    frame = random_frame(*size, sampling, seed=3)
    out = written(frame)
    im = Image.open(io.BytesIO(out))
    im.load()  # an independent decoder accepts the file
    assert im.size == size
    assert dct.visible_equal(dct.read(out), frame)
    assert canonical(out) == out


def test_more_than_ten_blocks_per_mcu_cannot_be_written_interleaved():
    frame = random_frame(48, 48, [(2, 2), (2, 2), (2, 2)])
    with pytest.raises(JpegError, match="blocks"):
        dct.write(frame)


def test_grey_jpeg_stating_a_larger_sampling_factor_decodes_as_one_block_per_mcu():
    buf = io.BytesIO()
    Image.fromarray(fixtures.photo(40, 30)).convert("L").save(buf, "JPEG", quality=85)
    data = buf.getvalue()
    sof = data.index(b"\xff\xc0")
    base_hv = sof + 4 + 6 + 1
    assert data[base_hv] == 0x11
    for hv in (0x22, 0x41, 0x14):
        patched = data[:base_hv] + bytes([hv]) + data[base_hv + 1 :]
        assert np.array_equal(pixels(patched), pixels(data))  # libjpeg agrees
        frame = dct.read(patched)
        assert (frame.comps[0].h, frame.comps[0].v) == (1, 1)
        assert np.array_equal(pixels(canonical(patched)), pixels(data))


# -- content ---------------------------------------------------------------------------------------------------


def test_picture_with_no_coefficients_but_dc():
    blocks = np.zeros((12, 64), dtype=np.int16)
    blocks[:, 0] = np.arange(12) * 5 - 30
    data = jpeg_craft.craft(blocks, 4, 3, QT)
    out = canonical(data)
    assert np.array_equal(pixels(out), pixels(data))
    scrubbed = strip_image(data, FULL)
    assert not dct.read(scrubbed.data).comps[0].coef[..., 1:].any()  # nothing to destroy, nothing invented
    assert Image.open(io.BytesIO(scrubbed.data)).size == (32, 24)


def test_every_coefficient_non_zero():
    rng = np.random.default_rng(1)
    blocks = rng.integers(1, 40, (20, 64)).astype(np.int16) * rng.choice([-1, 1], (20, 64)).astype(np.int16)
    blocks[:, 0] = rng.integers(-300, 300, 20)
    data = jpeg_craft.craft(blocks, 5, 4, QT)
    assert np.array_equal(pixels(canonical(data)), pixels(data))
    assert np.array_equal(dct.read(canonical(data)).comps[0].coef.reshape(-1, 64), blocks)


def test_extreme_magnitudes_round_trip_and_survive_the_scrub():
    blocks = np.zeros((16, 64), dtype=np.int16)
    blocks[::2, 0] = 1000
    blocks[1::2, 0] = -1000  # DC differences of 2000: eleven-bit categories
    blocks[:, 5] = 1023
    blocks[::3, 17] = -1023
    blocks[:, 63] = np.where(np.arange(16) % 2, 1023, -1023)  # a coefficient in the very last position: no EOB
    data = jpeg_craft.craft(blocks, 4, 4, QT)
    out = canonical(data)
    assert np.array_equal(dct.read(out).comps[0].coef.reshape(-1, 64), blocks)
    assert np.array_equal(pixels(out), pixels(data))
    result = strip_image(data, FULL)  # (c & ~1) | r must stay inside what an 8-bit picture can hold
    after = dct.read(result.data).comps[0].coef.reshape(-1, 64)
    assert np.abs(after[:, 1:]).max() <= 1023 and np.abs(after[:, 0]).max() <= 2047
    keep = (blocks < -2) | (blocks > 1)
    assert ((after >> 1)[keep] == (blocks >> 1)[keep]).all()  # every pair is kept
    assert (after[~keep] == 0).all()


@pytest.mark.parametrize("gap", [0, 1, 14, 15, 16, 17, 30, 31, 32, 33, 46, 47, 48, 61, 62])
def test_zero_runs_around_the_zrl_boundary(gap):
    blocks = np.zeros((3, 64), dtype=np.int16)
    blocks[:, 0] = [4, -9, 7]
    blocks[0, 1] = 3
    if 2 + gap <= 63:
        blocks[0, 2 + gap] = -2
    blocks[1, 63 - gap if gap < 62 else 1] = 5
    blocks[2, 1] = 1
    blocks[2, min(63, 1 + gap + 1)] = 1
    for kw in ({}, {"zrl_tail": True}, {"zrl_tail": True, "alias_eob": True}):
        data = jpeg_craft.craft(blocks, 3, 1, QT, **kw)
        out = canonical(data)
        assert np.array_equal(dct.read(out).comps[0].coef.reshape(-1, 64), blocks), (gap, kw)
        assert np.array_equal(pixels(out), pixels(data)), (gap, kw)
    assert len({canonical(jpeg_craft.craft(blocks, 3, 1, QT, **kw)) for kw in ({}, {"zrl_tail": True})}) == 1


def test_quantisation_extremes():
    for table in (np.ones(64, np.uint16), np.full(64, 255, np.uint16), np.arange(1, 65, dtype=np.uint16) * 9):
        data = jpeg_craft.craft(jpeg_craft.random_blocks(6, seed=5, density=0.05), 3, 2, table, wide_dqt=int(table.max()) > 255)
        out = canonical(data)
        assert np.array_equal(pixels(out), pixels(data))
        assert np.array_equal(dct.read(out).comps[0].qt, table)


def test_scrubbing_a_picture_whose_coefficients_are_all_small_leaves_a_flat_picture():
    blocks = np.zeros((9, 64), dtype=np.int16)
    blocks[:, 0] = 8
    blocks[:, 1:6] = np.array([1, -1, 0, -2, 1])
    data = jpeg_craft.craft(blocks, 3, 3, QT)
    result = strip_image(data, FULL)
    after = dct.read(result.data).comps[0].coef.reshape(-1, 64)
    assert not after[:, 1:].any()
    assert ((after[:, 0] >> 1) == (blocks[:, 0] >> 1)).all()


# -- file structure --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("interval", [1, 2, 7, 24, 25, 1000, 65535])
def test_restart_intervals_from_every_block_to_longer_than_the_picture(interval):
    blocks = jpeg_craft.random_blocks(24, seed=4)
    data = jpeg_craft.craft(blocks, 6, 4, QT, restart=interval)
    assert np.array_equal(dct.read(data).comps[0].coef.reshape(-1, 64), blocks)
    assert canonical(data) == canonical(jpeg_craft.craft(blocks, 6, 4, QT))


def test_fill_bytes_before_markers_and_trailing_garbage():
    data = make(40, 30, seed=1, quality=80)
    sos = data.index(b"\xff\xda")
    filled = data[:sos] + b"\xff\xff\xff" + data[sos:-2] + b"\xff\xff\xff\xd9" + b"TRAILING-GARBAGE" * 10
    assert np.array_equal(pixels(filled), pixels(data))
    assert canonical(filled) == canonical(data)
    result = strip_image(filled, CANON)
    assert b"TRAILING-GARBAGE" not in result.data and result.data.count(b"\xff\xd9") == 1


def test_table_redefined_between_scans_is_latched_per_scan():
    # Progressive libjpeg files redefine the Huffman tables before almost every scan.
    data = make(60, 45, seed=2, quality=75, progressive=True)
    assert data.count(b"\xff\xc4") > 3
    assert np.array_equal(pixels(canonical(data)), pixels(data))


def test_comment_and_application_segments_anywhere_are_ignored():
    data = make(32, 24, seed=1, quality=80)
    sos = data.index(b"\xff\xda")
    noisy = data[:sos] + b"\xff\xfe\x00\x07hello" + b"\xff\xe5\x00\x02" + data[sos:]
    assert canonical(noisy) == canonical(data)


@pytest.mark.parametrize("cut", ["no_eoi", "only_soi", "empty", "text", "soi_then_eoi", "dht_only"])
def test_unusable_files_are_refused(cut):
    data = make(32, 24, quality=80)
    bad = {
        "no_eoi": data[:-2],
        "only_soi": b"\xff\xd8",
        "empty": b"",
        "text": b"not a jpeg at all",
        "soi_then_eoi": b"\xff\xd8\xff\xd9",
        "dht_only": data[: data.index(b"\xff\xda")] + b"\xff\xd9",
    }[cut]
    with pytest.raises(JpegError):
        dct.read(bad)
    with pytest.raises(StripError):
        strip_image(bad, CANON)


def test_impossible_headers_are_refused():
    data = make(32, 24, quality=80)
    sof = data.index(b"\xff\xc0")
    length = int.from_bytes(data[sof + 2 : sof + 4], "big")
    segment = data[sof : sof + 2 + length]
    rest = data[sof + 2 + length :]
    head = data[:sof]
    with pytest.raises(JpegError):  # a component count no JPEG has
        dct.read(head + segment[:9] + b"\x07" + segment[10:] + rest)
    with pytest.raises(JpegError):  # height 0 means "see the DNL marker", which is not supported
        dct.read(head + segment[:5] + b"\x00\x00" + segment[7:] + rest)
    with pytest.raises(JpegError, match="more than one frame"):
        dct.read(head + segment + segment + rest)
    with pytest.raises(JpegError, match="before frame"):
        dct.read(b"\xff\xd8" + data[data.index(b"\xff\xda") :])
    with pytest.raises(JpegError):  # a segment length below two
        dct.read(head + b"\xff\xc0\x00\x00" + segment[4:] + rest)
    with pytest.raises(JpegError):  # a segment that runs past the end of the file
        dct.read(head + b"\xff\xc0\xff\xff" + segment[4:] + rest)


def test_huffman_tables_that_cannot_exist_are_refused():
    data = make(32, 24, quality=80, optimize=True)
    dht = data.index(b"\xff\xc4")
    length = int.from_bytes(data[dht + 2 : dht + 4], "big")
    body = bytearray(data[dht + 4 : dht + 2 + length])
    oversubscribed = bytearray(body)
    oversubscribed[1] = 200  # two hundred codes of length one
    assert b"\xff\xc4" in data
    with pytest.raises(JpegError):
        dct.read(data[: dht + 4] + bytes(oversubscribed) + data[dht + 2 + length :])
    wrong_count = bytearray(body)
    wrong_count[16] += 1
    with pytest.raises(JpegError):
        dct.read(data[: dht + 4] + bytes(wrong_count) + data[dht + 2 + length :])


def test_progressive_headers_that_cannot_exist_are_refused():
    data = make(32, 24, quality=80, progressive=True)
    first = data.index(b"\xff\xda")
    ns = data[first + 4]
    ss = first + 4 + 1 + 2 * ns
    for offset, value in ((0, 70), (1, 3), (2, 0xFF)):  # Ss past Se, Se past 63, absurd approximation
        bad = bytearray(data)
        bad[ss + offset] = value
        with pytest.raises(JpegError):
            dct.read(bytes(bad))


# -- the pipeline around it ------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [CANON, FULL])
def test_orientation_is_kept_as_one_tag_and_pixels_stay_unrotated(fx, mode):
    result = strip_image(fx["jpeg_everything.jpg"], mode)
    report = inspect_bytes(result.data)
    assert report.info["exif"].startswith(b"MM\x00\x2a")
    labels = {f.label for f in report.findings}
    assert "EXIF" in labels and labels <= {"EXIF", "ICC color profile"}
    assert fixtures.CANARY not in result.data
    assert Image.open(io.BytesIO(result.data)).getexif() == {274: 6}


@pytest.mark.parametrize("mode", [CANON, FULL])
def test_icc_profile_modes(fx, mode):
    for icc in ("convert", "sanitize", "drop"):
        opts = Options(scrub_dct=mode.scrub_dct, dct_min_psnr=0, icc=icc)
        result = strip_image(fx["jpeg_everything.jpg"], opts)
        profile = inspect_bytes(result.data).info.get("icc")
        assert profile is None or (b"CANARY" not in profile and b"APPL" not in profile)


def test_a_file_with_an_extra_picture_after_the_first_loses_it(fx):
    result = strip_image(fx["mpo_depth.jpg"], CANON)
    assert result.data.count(b"\xff\xd8") == 1 and b"MPF" not in result.data
    assert any("embedded image" in a for a in result.actions)


def test_rgb_jpeg_with_rgb_component_ids_keeps_its_colour_space():
    buf = io.BytesIO()
    Image.fromarray(fixtures.photo(48, 40)).save(buf, "JPEG", quality=85, subsampling=0)
    data = bytearray(buf.getvalue())
    app0 = data.index(b"\xff\xe0")
    adobe = b"\xff\xee\x00\x0eAdobe\x00\x64\x00\x00\x00\x00\x00"  # transform 0: the components are R, G and B
    data[app0 : app0 + 18] = adobe
    sof = data.index(b"\xff\xc0")
    for i, ident in enumerate((82, 71, 66)):
        data[sof + 10 + 3 * i] = ident
    sos = data.index(b"\xff\xda")
    for i, ident in enumerate((82, 71, 66)):
        data[sos + 5 + 2 * i] = ident
    data = bytes(data)
    assert Image.open(io.BytesIO(data)).mode == "RGB"
    assert not np.array_equal(pixels(data), pixels(make(48, 40, quality=85, subsampling=0)))  # really RGB, not YCbCr
    for opts in (CANON, FULL):
        out = strip_image(data, opts).data
        assert Image.open(io.BytesIO(out)).mode == "RGB"
        if opts is CANON:
            assert np.array_equal(pixels(out), pixels(data))
        assert b"Adobe" in out and b"JFIF" not in out
        assert dct.read(out).comps[0].ident == 82


def test_cmyk_jpeg_through_every_mode(fx):
    for opts in (CANON, FULL):
        out = strip_image(fx["cmyk.jpg"], opts).data
        assert Image.open(io.BytesIO(out)).mode == "CMYK"
        if opts is CANON:
            assert np.array_equal(pixels(out), pixels(fx["cmyk.jpg"]))


def test_size_limits_apply_before_the_codec_allocates_anything():
    data = make(300, 300, quality=80)
    with pytest.raises(StripError, match="MP"):
        strip_image(data, Options(scrub_dct="full", max_pixels=1000))
    with pytest.raises(JpegError, match="larger"):
        dct.read(data, max_pixels=1000)
    huge = bytearray(data)
    sof = huge.index(b"\xff\xc0")
    huge[sof + 5 : sof + 9] = b"\xff\xff\xff\xff"  # 65535 x 65535 in the header
    with pytest.raises(JpegError):
        dct.read(bytes(huge))


def test_a_larger_picture_end_to_end():
    data = make(1200, 800, seed=3, quality=88)
    out = strip_image(data, CANON)
    assert np.array_equal(pixels(out.data), pixels(data))
    full = strip_image(data, FULL)
    assert full.dct_psnr > 27 and len(full.data) < len(data)
    assert_clean(full.data, ".jpg")


def test_the_scrub_refuses_a_picture_it_would_wreck_and_writes_nothing():
    data = make(64, 48, quality=30, subsampling=2)
    with pytest.raises(StripError, match="dB"):
        strip_image(data, Options(scrub_dct="full", dct_min_psnr=70))


def test_orientation_can_be_discarded_without_rotating():
    tagged = fixtures.jpeg_everything(6)
    result = strip_image(tagged, Options(scrub_dct="canonical", auto_orient=False))
    assert any("orientation tag discarded" in w for w in result.warnings)
    assert not inspect_bytes(result.data).info.get("exif")


def test_struct_helpers_are_not_confused_by_odd_lengths():
    # a 2-byte APPn (length field only) and a zero-length comment are legal and must not derail the marker walk
    data = make(24, 16, quality=80)
    sos = data.index(b"\xff\xda")
    odd = data[:sos] + b"\xff\xe3" + struct.pack(">H", 2) + b"\xff\xfe" + struct.pack(">H", 2) + data[sos:]
    assert canonical(odd) == canonical(data)
