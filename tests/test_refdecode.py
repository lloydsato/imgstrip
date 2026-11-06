"""The second, independent reader in stage 2 (review item 3): refdecode."""

from __future__ import annotations

import io
import struct
import zlib

import numpy as np
import pytest
from PIL import Image

from imgstrip import core, dct, fixtures, oracles, refdecode
from imgstrip.core import Options, StripError, VerificationError, strip_image

NAMES = list(fixtures.all_fixtures())


def rows_of(data: bytes):
    frames = oracles.our_view(data)
    return len(frames), lambda k, top, bottom: frames[k][top:bottom]


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("fmt", ["PNG", "BMP"])
def test_every_png_and_bmp_output_is_read_by_the_independent_reader(fx, name, fmt):
    try:
        result = strip_image(fx[name], Options(format=fmt))
    except StripError:
        pytest.skip("this fixture cannot be written in this format")
    assert result.verified_by[:2] == ["structure", "Pillow"]
    assert any(v.startswith(f"imgstrip {fmt} reader") for v in result.verified_by), result.verified_by


@pytest.mark.parametrize("options", [Options(format="PNG", optimize=True), Options(format="PNG", scrub_lsb=True)])
def test_the_other_png_encoders_are_read_too(fx, options):
    for name in ("png16.png", "png_everything.png", "gif_animated.gif"):
        if name in fx:
            assert "imgstrip PNG reader" in " ".join(strip_image(fx[name], options).verified_by)


def test_a_defect_shared_by_encoder_and_pillow_is_caught(monkeypatch):
    """A common-mode failure: the encoder swaps red and blue, and so does the decoder that checks it.  Pillow's own
    comparison is fooled; the independent reader is not."""
    source = io.BytesIO()
    Image.fromarray(fixtures.photo(48, 32)).save(source, "PNG")
    real_encode, real_open = core._Job._encode, core.open_image
    swapped: set[bytes] = set()

    def encode(self, frames, out, loop, src):
        data, expected, exact = real_encode(self, frames, out, loop, src)
        r, g, b = Image.open(io.BytesIO(data)).convert("RGB").split()
        buf = io.BytesIO()
        Image.merge("RGB", (b, g, r)).save(buf, "PNG")
        swapped.add(buf.getvalue())
        return buf.getvalue(), expected, exact

    def open_image(data):
        im = real_open(data)
        if data in swapped:
            r, g, b = im.convert("RGB").split()
            return Image.merge("RGB", (b, g, r))
        return im

    monkeypatch.setattr(core._Job, "_encode", encode)
    monkeypatch.setattr(core, "open_image", open_image)
    with pytest.raises(VerificationError, match="independent reader"):
        strip_image(source.getvalue(), Options())


# -- the PNG reader on crafted files -------------------------------------------------------------------------


def chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png(width, height, depth, ctype, raw_rows: bytes, extra=b"", plte=None, trns=None, interlace=0) -> bytes:
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, ctype, 0, 0, interlace))
    if plte is not None:
        out += chunk(b"PLTE", plte)
    if trns is not None:
        out += chunk(b"tRNS", trns)
    return out + chunk(b"IDAT", zlib.compress(raw_rows) + extra) + chunk(b"IEND", b"")


def grey_rows(values: np.ndarray, kinds=None) -> bytes:
    """Filter type 0 rows of 8-bit grey values."""
    return b"".join(bytes((0,)) + bytes(row) for row in values.astype(np.uint8))


def want_grey(values):
    rgba = np.empty(values.shape + (4,), np.uint16)
    rgba[..., :3] = values[..., None].astype(np.uint16) * 257
    rgba[..., 3] = 65535
    return lambda k, t, b: rgba[t:b]


def test_crafted_png_agrees_and_one_changed_sample_does_not():
    values = np.arange(12, dtype=np.uint8).reshape(3, 4) * 20
    data = png(4, 3, 8, 0, grey_rows(values))
    refdecode.check_png(data, 1, want_grey(values))
    other = values.copy()
    other[1, 2] += 1
    with pytest.raises(refdecode.Disagreement, match="row 1"):
        refdecode.check_png(data, 1, want_grey(other))


@pytest.mark.parametrize(
    "bad, why",
    [
        (lambda v: png(4, 3, 8, 0, grey_rows(v), extra=b"\x00"), "deflate"),
        (lambda v: png(4, 3, 8, 0, grey_rows(v) + b"\x00"), "more pixel data"),
        (lambda v: png(4, 3, 8, 0, grey_rows(v)[:-1]), "ends early"),
        (lambda v: png(4, 3, 8, 0, grey_rows(v), interlace=1), "interlaced"),
        (lambda v: png(4, 3, 8, 0, grey_rows(v), trns=struct.pack(">H", int(v[0, 1]))), "colour-key"),
        (lambda v: png(4, 3, 8, 0, grey_rows(v)) + b"junk", "after IEND"),
    ],
)
def test_crafted_png_damage_is_a_disagreement(bad, why):
    values = np.arange(12, dtype=np.uint8).reshape(3, 4) * 20
    with pytest.raises(refdecode.Disagreement, match=why):
        refdecode.check_png(bad(values), 1, want_grey(values))


@pytest.mark.parametrize("depth", [1, 2, 4, 8, 16])
@pytest.mark.parametrize("ctype", [0, 2, 3, 4, 6])
def test_every_colour_type_and_depth_round_trips_through_codecs(ctype, depth):
    if depth not in oracles._DEPTHS[ctype]:
        pytest.skip("not a valid PNG combination")
    from imgstrip import codecs

    rng = np.random.default_rng(depth * 10 + ctype)
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    top = (1 << depth) - 1
    samples = rng.integers(0, top + 1, (9, 13, channels)).astype(np.uint16 if depth == 16 else np.uint8)
    palette = None
    if ctype == 3:
        n = min(1 << depth, 7)
        samples = samples % n
        palette = bytes(rng.permutation(256)[: 3 * n].astype(np.uint8))
    for strategy in ("adaptive", "none", "paeth", "up"):
        data = codecs.encode_png(
            samples if depth == 16 else samples.astype(np.uint8), depth, ctype, palette, strategy=strategy
        )
        reference = oracles.reference_png(data).frames  # the strict reader decodes every depth exactly
        refdecode.check_png(data, 1, lambda k, t, b: reference[k][t:b])


def test_the_filters_written_here_invert_the_reference_unfilter():
    """Two independent implementations of the PNG filters agree: filtering here, then the strict reader's unfilter,
    gives the raw rows back for every filter type and byte distance."""
    rng = np.random.default_rng(3)
    for bpp in (1, 2, 3, 4, 6, 8):
        raw = rng.integers(0, 256, (12, 8 * bpp)).astype(np.uint8)
        kinds = rng.integers(0, 5, 12).astype(np.uint8)
        filtered = refdecode._filtered(raw, np.zeros(raw.shape[1], np.uint8), kinds, bpp)
        stream = np.concatenate([kinds[:, None], filtered], axis=1).tobytes()
        assert np.array_equal(oracles._unfilter(stream, 12, raw.shape[1], bpp), raw)


def test_animation_frames_are_composed_and_checked():
    frames = [np.zeros((20, 30, 4), np.uint8) for _ in range(3)]
    for k, f in enumerate(frames):
        f[..., 3] = 255
        f[5 : 5 + 4 * k, 3 : 3 + 5 * k, 0] = 200  # each frame changes a growing corner, so Pillow writes sub-frames
    buf = io.BytesIO()
    imgs = [Image.fromarray(f) for f in frames]
    imgs[0].save(buf, "PNG", save_all=True, append_images=imgs[1:], duration=50)
    data = buf.getvalue()
    n, rows = rows_of(data)
    assert n == 3
    refdecode.check_png(data, 3, rows)
    wrong = [f.astype(np.uint16) * 257 for f in frames]
    wrong[2][0, 0, 1] = 257
    with pytest.raises(refdecode.Disagreement, match="frame 2"):
        refdecode.check_png(data, 3, lambda k, t, b: wrong[k][t:b])


def test_bmp_reader_on_every_layout():
    rng = np.random.default_rng(5)
    rgb = rng.integers(0, 256, (7, 11, 3), dtype=np.uint8)
    for im in (
        Image.fromarray(rgb),
        Image.fromarray(rgb).convert("L"),
        Image.fromarray(rgb).convert("1"),
        Image.fromarray(rgb).convert("P", palette=Image.Palette.ADAPTIVE, colors=12),
    ):
        buf = io.BytesIO()
        im.save(buf, "BMP")
        n, rows = rows_of(buf.getvalue())
        refdecode.check_bmp(buf.getvalue(), rows)
    from imgstrip import codecs

    rgba = rng.integers(0, 256, (7, 11, 4), dtype=np.uint8)
    rgba[rgba[..., 3] == 0] = 0
    data = codecs.encode_bmp_rgba(rgba)
    n, rows = rows_of(data)
    refdecode.check_bmp(data, rows)
    bad = bytearray(data)
    bad[-1] ^= 0x10
    with pytest.raises(refdecode.Disagreement):
        refdecode.check_bmp(bytes(bad), rows)


# -- JPEG ---------------------------------------------------------------------------------------------------------


def photo_jpeg(mode="RGB", **kw):
    buf = io.BytesIO()
    Image.fromarray(fixtures.photo(120, 80)).convert(mode).save(buf, "JPEG", quality=90, **kw)
    return buf.getvalue()


def test_the_jpeg_decoder_is_off_unless_asked_for():
    assert "imgstrip JPEG decoder" not in strip_image(photo_jpeg()).verified_by
    assert "imgstrip JPEG decoder" in strip_image(photo_jpeg(), Options(independent_jpeg=True)).verified_by


@pytest.mark.parametrize("mode", ["RGB", "L", "CMYK"])
@pytest.mark.parametrize("subsampling", [0, 1, 2])
def test_the_jpeg_decoder_agrees_with_libjpeg(mode, subsampling):
    data = photo_jpeg(mode, subsampling=subsampling)
    mine = refdecode.decode_jpeg(data, 10**8).astype(int)
    theirs = np.asarray(Image.open(io.BytesIO(data))).astype(int)
    assert np.abs(mine.reshape(theirs.shape) - theirs).mean() < refdecode.JPEG_DECODERS.mean / 2


def test_a_disagreeing_jpeg_decoder_refuses_the_file(monkeypatch):
    real = refdecode.decode_jpeg
    monkeypatch.setattr(
        refdecode, "decode_jpeg", lambda data, mp: np.clip(real(data, mp).astype(int) + 9, 0, 255).astype(np.uint8)
    )
    with pytest.raises(VerificationError, match="independent reader"):
        strip_image(photo_jpeg(), Options(independent_jpeg=True))


@pytest.mark.parametrize(
    "options", [Options(jpeg_lossless=True), Options(scrub_dct="canonical"), Options(scrub_dct="full", dct_min_psnr=0)]
)
def test_the_coefficient_modes_reread_their_output_with_the_coefficient_reader(options):
    assert "imgstrip JPEG coefficient reader" in strip_image(photo_jpeg(), options).verified_by


def test_canonical_form_is_a_fixed_point_and_other_encodings_are_not():
    canonical = strip_image(photo_jpeg(), Options(scrub_dct="canonical")).data
    core._Job._verify_canonical_form(canonical, dct.read(canonical))
    libjpeg = photo_jpeg(optimize=False)
    with pytest.raises(VerificationError, match="canonical form"):
        core._Job._verify_canonical_form(libjpeg, dct.read(libjpeg))
