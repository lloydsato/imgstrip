"""Custom encoders/decoders against independent decoders."""

import io
import subprocess

import numpy as np
import pytest
from conftest import HAS_MAGICK
from PIL import Image

from imgstrip import codecs
from imgstrip.inspect import inspect_bytes

RNG = np.random.default_rng(42)


@pytest.mark.parametrize("strategy", ["adaptive", "none", "sub", "up", "avg", "paeth"])
@pytest.mark.parametrize(
    "depth,ctype,channels",
    [(8, 0, 1), (8, 2, 3), (8, 4, 2), (8, 6, 4), (16, 0, 1), (16, 2, 3), (16, 4, 2), (16, 6, 4)],
)
def test_png_encoder_roundtrip(depth, ctype, channels, strategy):
    dtype = np.uint16 if depth == 16 else np.uint8
    hi = 65536 if depth == 16 else 256
    arr = RNG.integers(0, hi, (17, 23, channels), dtype=dtype)
    arr[5:9] = arr[4]  # some repetition so filters differ
    data = codecs.encode_png(arr if channels > 1 else arr[..., 0], depth, ctype, strategy=strategy)
    assert inspect_bytes(data).clean
    im = Image.open(io.BytesIO(data))
    if depth == 16 and channels > 1:
        kind = codecs.high_bit_depth_kind(im)
        got = codecs.decode_16bit(lambda: Image.open(io.BytesIO(data)), 0, kind)
    else:
        im.load()
        got = np.asarray(im)
        got = got[..., None] if got.ndim == 2 else got
    assert np.array_equal(got.astype(np.uint32), arr.astype(np.uint32))


@pytest.mark.parametrize("depth", [1, 2, 4, 8])
def test_png_encoder_palette_bit_depths(depth):
    n = 1 << depth
    idx = RNG.integers(0, n, (13, 29), dtype=np.uint8)
    palette = RNG.integers(0, 256, (n, 3), dtype=np.uint8)
    data = codecs.encode_png(idx, depth, 3, palette=palette.tobytes(), trns=bytes([0, 128]))
    im = Image.open(io.BytesIO(data))
    assert np.array_equal(np.asarray(im), idx)
    rgba = np.asarray(im.convert("RGBA"))
    assert rgba[idx == 0][..., 3].max(initial=0) == 0
    assert inspect_bytes(data).clean


@pytest.mark.skipif(not HAS_MAGICK, reason="ImageMagick not installed")
@pytest.mark.parametrize("channels,fmt", [(3, "rgb"), (4, "rgba"), (2, "graya")])
def test_16bit_decode_matches_imagemagick(tmp_path, channels, fmt):
    arr = RNG.integers(0, 65536, (19, 31, channels), dtype=np.uint16)
    ctype = {2: 4, 3: 2, 4: 6}[channels]
    path = tmp_path / "x.png"
    path.write_bytes(codecs.encode_png(arr, 16, ctype))
    oracle = np.frombuffer(
        subprocess.run(
            ["convert", str(path), "-depth", "16", "-endian", "MSB", f"{fmt}:-"], capture_output=True, check=True
        ).stdout,
        ">u2",
    ).reshape(arr.shape)
    assert np.array_equal(oracle, arr)  # our encoder is right ...
    im = Image.open(path)
    kind = codecs.high_bit_depth_kind(im)
    got = codecs.decode_16bit(lambda: Image.open(path), 0, kind)
    assert np.array_equal(got, arr)  # ... and so is the dual-pass decoder


@pytest.mark.skipif(not HAS_MAGICK, reason="ImageMagick not installed")
def test_16bit_interlaced_png_and_tiff_decode(tmp_path):
    arr = RNG.integers(0, 65536, (21, 18, 3), dtype=np.uint16)
    raw = tmp_path / "x.rgb"
    raw.write_bytes(arr.astype(">u2").tobytes())
    for out, extra in (
        ("i.png", ["-interlace", "PNG"]),
        ("t.tif", ["-compress", "zip"]),
        ("u.tif", ["-compress", "none"]),
    ):
        target = tmp_path / out
        subprocess.run(
            ["convert", "-size", "18x21", "-depth", "16", "-endian", "MSB", f"rgb:{raw}", *extra, str(target)],
            check=True,
        )
        im = Image.open(target)
        kind = codecs.high_bit_depth_kind(im)
        assert kind == "RGB", out
        got = codecs.decode_16bit(lambda: Image.open(target), 0, kind)
        assert np.array_equal(got, arr), out


def test_pnm16_decoder():
    arr = RNG.integers(0, 1024, (7, 9, 3), dtype=np.uint16)
    data = b"P6\n# CANARY comment\n9 7\n1023\n" + arr.astype(">u2").tobytes()
    got = codecs.decode_pnm16(io.BytesIO(data))
    assert np.array_equal(got, ((arr.astype(np.uint32) * 65535 + 511) // 1023).astype(np.uint16))


def test_gif_encoder_transparent_animation_exact():
    frames = []
    for i in range(4):
        f = np.zeros((20, 30, 4), np.uint8)
        f[..., :3] = RNG.integers(0, 4, (20, 30, 3)) * 80
        f[..., 3] = 255
        f[i * 4 : i * 4 + 6, 5:25, 3] = 0
        f[f[..., 3] == 0] = 0
        frames.append(f)
    data, quantized = codecs.encode_gif(frames, [40, 50, 60, 70], 3)
    assert not quantized
    assert inspect_bytes(data).clean
    im = Image.open(io.BytesIO(data))
    assert im.n_frames == 4 and im.info["loop"] == 3
    for i, f in enumerate(frames):
        im.seek(i)
        got = np.asarray(im.convert("RGBA")).copy()
        got[got[..., 3] == 0] = 0
        assert np.array_equal(got, f), i
        assert im.info["duration"] == [40, 50, 60, 70][i]


def test_gif_encoder_quantizes_only_when_needed():
    many = np.dstack([RNG.integers(0, 256, (32, 32, 3), dtype=np.uint8), np.full((32, 32), 255, np.uint8)])
    data, quantized = codecs.encode_gif([many], None, None)
    assert quantized == [0]
    assert inspect_bytes(data).clean


def test_bmp_rgba_writer():
    arr = RNG.integers(0, 256, (11, 13, 4), dtype=np.uint8)
    data = codecs.encode_bmp_rgba(arr)
    im = Image.open(io.BytesIO(data))
    assert im.mode == "RGBA"
    assert np.array_equal(np.asarray(im), arr)
    assert inspect_bytes(data).clean


def test_tiff_writer_multipage_mixed_modes():
    pages = [
        codecs.TiffPage(RNG.integers(0, 256, (10, 12, 3), dtype=np.uint8), 2),
        codecs.TiffPage(RNG.integers(0, 65536, (7, 5, 4), dtype=np.uint16), 2, extra=1),
        codecs.TiffPage((RNG.random((9, 17)) > 0.5).astype(np.uint8), 1, bilevel=True),
    ]
    data = codecs.encode_tiff(pages)
    report = inspect_bytes(data)
    assert report.clean and report.info["pages"] == 3
    im = Image.open(io.BytesIO(data))
    assert im.n_frames == 3
    assert np.array_equal(np.asarray(im), pages[0].array)
    im.seek(1)
    got = codecs.decode_16bit(lambda: _seeked(data, 1), 1, codecs.high_bit_depth_kind(im))
    assert np.array_equal(got, pages[1].array)
    im.seek(2)
    assert np.array_equal(np.asarray(im).astype(np.uint8), pages[2].array[..., 0])


def _seeked(data, frame):
    return Image.open(io.BytesIO(data))


def _multipage_tiff(arrays, mode):
    import io
    import warnings

    from PIL import Image

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        frames = [Image.fromarray(a, mode) if mode else Image.fromarray(a) for a in arrays]
    buf = io.BytesIO()
    frames[0].save(buf, "TIFF", save_all=True, append_images=frames[1:])
    return buf.getvalue()


def test_multipage_16bit_tiff_is_refused_not_clipped():
    import numpy as np
    import pytest

    from imgstrip import Options, StripError, strip_image

    gradient = np.tile(np.linspace(0, 65535, 64).astype(np.uint16), (32, 1))
    data = _multipage_tiff([gradient, gradient[::-1].copy()], "I;16")
    with pytest.raises(StripError, match="more than 8 bits"):
        strip_image(data, Options())


def test_multipage_8bit_and_single_16bit_tiff_unchanged():
    import io

    import numpy as np
    from PIL import Image

    from imgstrip import Options, strip_image

    ramp = np.tile(np.arange(64, dtype=np.uint8), (32, 1))
    result = strip_image(_multipage_tiff([ramp, ramp[::-1].copy()], None), Options())
    assert result.frames == 2 and result.pixels_exact
    gradient = np.tile(np.linspace(0, 65535, 64).astype(np.uint16), (32, 1))
    single = strip_image(_multipage_tiff([gradient], "I;16"), Options())
    assert single.pixels_exact
    assert np.array_equal(np.asarray(Image.open(io.BytesIO(single.data))), gradient)
