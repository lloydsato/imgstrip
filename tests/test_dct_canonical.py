"""Layer A: one canonical form for every way of encoding the same coefficients.

Everything around the coefficients that a decoder ignores (Huffman table layout, unused tables, redundant tokens,
padding bits, restart markers, scan scripts, blocks outside the picture, component ids) is a place to hide data.
The canonical writer makes the file a pure function of the coefficients, the quantisation tables and the geometry,
which these tests check by hiding things in every one of those places and asking for byte-identical output.
"""

import itertools

import numpy as np
import pytest

import jpeg_craft
from imgstrip import Options, StripError, dct, fixtures, inspect_bytes, strip_image
from imgstrip.dct import ZIGZAG
from test_dct_codec import canonical, make, pixels


def _natural(zigzag_table):
    nat = np.empty(64, np.uint16)
    nat[list(ZIGZAG)] = zigzag_table
    return nat


BW, BH = 6, 4
BLOCKS = jpeg_craft.random_blocks(BW * BH, seed=2)
QT = _natural(np.clip(np.arange(1, 65) % 23 + 2, 1, 255))

KNOBS = [
    {},
    {"table": "skewed"},
    {"alias_eob": True},
    {"zrl_tail": True},
    {"zrl_tail": True, "alias_eob": True, "table": "skewed"},
    {"restart": 1},
    {"restart": 5},
    {"pad_bit": 0},
    {"extra_dqt": True},
    {"extra_dht": True},
    {"wide_dqt": True},
    {"ext_sequential": True},
    {"comp_id": 0},
    {"comp_id": 255},
    {"com": b"x" * 300, "jfif": False},
]


def test_the_variants_are_valid_and_alike():
    """The crafted files are real JPEGs that every decoder reads as one picture (so they test something)."""
    ref = jpeg_craft.craft(BLOCKS, BW, BH, QT)
    for kw in KNOBS:
        for seed in range(3):
            v = jpeg_craft.craft(BLOCKS, BW, BH, QT, seed=seed, **kw)
            assert np.array_equal(pixels(v), pixels(ref)), kw
            assert np.array_equal(dct.read(v).comps[0].coef.reshape(-1, 64), BLOCKS), kw


@pytest.mark.parametrize("kw", KNOBS, ids=lambda kw: ",".join(sorted(kw)) or "plain")
def test_every_hiding_place_collapses_to_one_output(kw):
    reference = canonical(jpeg_craft.craft(BLOCKS, BW, BH, QT))
    for seed in range(4):
        assert canonical(jpeg_craft.craft(BLOCKS, BW, BH, QT, seed=seed, **kw)) == reference


def test_all_knobs_at_once_and_random_combinations():
    reference = canonical(jpeg_craft.craft(BLOCKS, BW, BH, QT))
    rng = np.random.default_rng(1)
    names = [list(k)[0] for k in KNOBS[1:] if len(k) == 1 and isinstance(list(k.values())[0], bool)]
    for _ in range(40):
        kw = {n: True for n in names if rng.random() < 0.5}
        if rng.random() < 0.5:
            kw["table"] = "skewed"
        kw["restart"] = int(rng.integers(0, 7))
        kw["pad_bit"] = int(rng.integers(0, 2))
        kw["comp_id"] = int(rng.integers(0, 256))
        assert canonical(jpeg_craft.craft(BLOCKS, BW, BH, QT, seed=int(rng.integers(0, 99)), **kw)) == reference


def test_canonical_output_of_canonical_output_is_unchanged():
    out = canonical(jpeg_craft.craft(BLOCKS, BW, BH, QT, table="skewed", restart=2))
    assert canonical(out) == out


@pytest.mark.parametrize("subsampling", [0, 1, 2])
def test_one_picture_encoded_every_libjpeg_way_has_one_canonical_form(subsampling):
    base = dict(seed=6, quality=82, subsampling=subsampling)
    variants = [
        make(70, 53, **base),
        make(70, 53, optimize=True, **base),
        make(70, 53, progressive=True, **base),
        make(70, 53, restart_marker_blocks=2, **base),
        make(70, 53, restart_marker_rows=1, optimize=True, **base),
    ]
    outs = {canonical(v) for v in variants}
    assert len(outs) == 1
    for v in variants:
        assert np.array_equal(pixels(v), pixels(next(iter(outs))))


def test_data_in_blocks_outside_the_picture_is_removed_and_changes_no_pixel(monkeypatch):
    """A 4:2:0 picture whose size is not a multiple of the MCU has blocks nobody sees: they must not survive."""
    data = make(90, 70, seed=2, quality=85, subsampling=2)
    clean = canonical(data)
    frame = dct.read(data)
    luma = frame.comps[0]
    assert luma.coef.shape[0] > luma.blocks_h  # there is a hidden row of blocks
    rng = np.random.default_rng(0)
    luma.coef[luma.blocks_h :, :, :] = rng.integers(-30, 31, luma.coef[luma.blocks_h :].shape)
    luma.coef[:, luma.blocks_w :, :] = rng.integers(-30, 31, luma.coef[:, luma.blocks_w :].shape)
    with monkeypatch.context() as m:
        m.setattr(dct, "canonicalise_padding", lambda frame: None)
        tainted = b"\xff\xd8" + dct.write(frame) + b"\xff\xd9"
    assert tainted != clean
    assert np.array_equal(pixels(tainted), pixels(data)), "blocks outside the picture must not affect any pixel"
    assert canonical(tainted) == clean


def test_padding_bits_of_the_last_byte_are_not_a_channel():
    data = make(33, 25, seed=1, quality=70)
    out = canonical(data)
    reference = dct.read(out)
    last = len(out) - 3  # the byte before the end-of-image marker
    seen = {out}
    for bits in range(1, 8):  # try to rewrite 1..7 low bits of the last byte; keep what still decodes alike
        for value in range(1 << bits):
            if out[last] == 0xFF:
                break
            b = (out[last] & ~((1 << bits) - 1)) | value
            if b == 0xFF:
                continue
            candidate = out[:last] + bytes([b]) + out[last + 1 :]
            try:
                if dct.visible_equal(dct.read(candidate), reference):
                    seen.add(candidate)
            except Exception:  # noqa: BLE001 - an invalid candidate is simply not a variant
                pass
    assert len(seen) > 1, "no padding bit could be changed, the test does not test anything"
    assert {canonical(v) for v in seen} == {out}


# -- end to end through the cleaner ----------------------------------------------------------------------------


def _markers(data):
    out, pos = [], 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        out.append(marker)
        if marker == 0xDA:
            break
        pos += 2 + int.from_bytes(data[pos + 2 : pos + 4], "big")
    return out


def test_strip_canonical_removes_unused_tables_and_keeps_every_coefficient():
    dirty = jpeg_craft.craft(
        BLOCKS, BW, BH, QT, extra_dqt=True, extra_dht=True, restart=2, com=b"CANARY-HIDDEN", table="skewed"
    )
    assert b"CANARY-HIDDEN" in dirty
    result = strip_image(dirty, Options(scrub_dct="canonical"))
    assert result.dct_scrub == "canonical" and result.pixels_exact and result.verified_exact
    markers = _markers(result.data)
    assert markers.count(0xDB) == 1 and markers.count(0xC4) == 2
    assert 0xDD not in markers and 0xFE not in markers
    assert result.data.endswith(b"\xff\xd9") and result.data.count(b"\xff\xd9") == 1
    assert np.array_equal(pixels(result.data), pixels(dirty))
    assert dct.visible_equal(dct.read(result.data), dct.read(dirty))


def test_strip_canonical_is_deterministic_and_idempotent(fx):
    for name in ("jpeg_everything.jpg", "jpeg_scan_tricks.jpg", "cmyk.jpg"):
        if name not in fx:
            continue
        a = strip_image(fx[name], Options(scrub_dct="canonical"))
        b = strip_image(fx[name], Options(scrub_dct="canonical"))
        assert a.data == b.data
        c = strip_image(a.data, Options(scrub_dct="canonical"))
        assert c.data == a.data


def test_canonical_mode_leaves_default_behaviour_alone(fx):
    for name in [n for n in fx if n.endswith(".jpg")]:
        assert strip_image(fx[name], Options()).data == strip_image(fx[name], Options(scrub_dct="off")).data
        assert strip_image(fx[name], Options(scrub_dct=False)).data == strip_image(fx[name], Options()).data


def test_canonicalising_keeps_the_stego_coefficients(tmp_path):
    """Layer A alone is lossless, so it must not (and does not claim to) touch a payload in the coefficients."""
    import dct_embedders as E

    cover = make(160, 120, seed=3, quality=85)
    stego, bits = E.make_stego(cover, "jsteg", 800)
    out = strip_image(stego, Options(scrub_dct="canonical")).data
    assert E.read_back(out, "jsteg", bits) == 0.0
    assert inspect_bytes(out).findings == []


def test_orientation_and_icc_are_handled_like_jpeg_lossless(fx):
    a = strip_image(fx["jpeg_everything.jpg"], Options(jpeg_lossless=True))
    b = strip_image(fx["jpeg_everything.jpg"], Options(scrub_dct="canonical"))
    ra, rb = inspect_bytes(a.data), inspect_bytes(b.data)
    assert ra.info.get("exif") == rb.info.get("exif")
    assert ra.info.get("icc") == rb.info.get("icc")
    assert [f.label for f in ra.findings] == [f.label for f in rb.findings]
    assert fixtures.CANARY not in b.data


def test_messages_name_jpeg_canonical_not_scrub_dct(fx):
    jpg = fx["jpeg_everything.jpg"]
    result = strip_image(jpg, Options(scrub_dct="canonical", scrub_lsb=True))
    assert "--scrub-lsb has no effect with --jpeg-canonical" in result.warnings
    with pytest.raises(StripError, match=r"canonical JPEG rebuilding \(--jpeg-canonical\)"):
        strip_image(jpg, Options(scrub_dct="canonical", scrub_prnu=True))
    with pytest.raises(StripError, match="^--jpeg-canonical works on JPEG files"):
        strip_image(jpg, Options(scrub_dct="canonical", format="png"))


def test_combinations_do_not_explode():
    for knobs in itertools.product([0, 3], [0, 1], ["fixed", "skewed"]):
        restart, pad, table = knobs
        v = jpeg_craft.craft(BLOCKS, BW, BH, QT, restart=restart, pad_bit=pad, table=table)
        assert strip_image(v, Options(scrub_dct="canonical")).pixels_exact
