"""End-to-end stripping of the worst-case fixtures."""

import io

import numpy as np
import pytest
from conftest import assert_clean, decode, rgba, visible
from PIL import Image

from imgstrip import Options, StripError, fixtures, inspect_bytes, strip_image
from imgstrip.core import heif_available

SUFFIX = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "GIF": ".gif",
    "TIFF": ".tif",
    "BMP": ".bmp",
    "AVIF": ".avif",
    "HEIF": ".heic",
}


@pytest.mark.parametrize("name", list(fixtures.all_fixtures()))
def test_every_fixture_is_cleaned(fx, name):
    data = fx[name]
    before = inspect_bytes(data)
    result = strip_image(data)
    assert_clean(
        result.data,
        SUFFIX[result.output_format],
        allow=("Pixels outside the visible crop (clean aperture)",) if result.output_format == "HEIF" else (),
    )
    # Every metadata item the inspector saw in the input is reported as removed.
    assert result.removed == before.findings


@pytest.mark.parametrize(
    "fmt", ["JPEG", "PNG", "WEBP", "GIF", "TIFF", "BMP", "AVIF"] + (["HEIF"] if heif_available() else [])
)
@pytest.mark.parametrize(
    "name", ["jpeg_everything.jpg", "png_everything.png", "gif_animated.gif", "webp_lossless_alpha.webp"]
)
def test_cross_format_conversion_is_clean(fx, name, fmt):
    result = strip_image(fx[name], Options(format=fmt))
    assert result.output_format == fmt
    assert decode(result.data).format in (fmt, "MPO")
    assert_clean(
        result.data, SUFFIX[fmt], allow=("Pixels outside the visible crop (clean aperture)",) if fmt == "HEIF" else ()
    )


def test_jpeg_orientation_is_baked_in(fx):
    result = strip_image(fx["jpeg_everything.jpg"])  # 160x120 with orientation 6
    assert result.size == (120, 160)
    assert decode(result.data).size == (120, 160)
    assert any("orientation" in a for a in result.actions)


def test_no_auto_orient_keeps_geometry(fx):
    result = strip_image(fx["jpeg_everything.jpg"], Options(auto_orient=False))
    assert result.size == (160, 120)
    assert any("orientation" in w for w in result.warnings)


def test_mpo_secondary_image_removed(fx):
    result = strip_image(fx["mpo_depth.jpg"])
    im = decode(result.data)
    assert im.format == "JPEG" and getattr(im, "n_frames", 1) == 1
    assert b"MPF\x00" not in result.data


def test_png_lossless_pixels_identical(fx):
    data = fixtures.png_lsb_stego()
    result = strip_image(data)
    assert result.pixels_exact
    assert np.array_equal(rgba(result.data), rgba(data))


def test_palette_png_canonicalised(fx):
    data = fx["png_palette.png"]
    result = strip_image(data)
    assert np.array_equal(visible(rgba(result.data)), visible(rgba(data)))
    out = decode(result.data)
    assert out.mode == "P"
    used = len(out.getpalette()) // 3
    assert used <= 6  # 7 used indices, one a duplicate colour -> 6 distinct colours
    assert b"CANARY-PALETTE" not in result.data


def test_hidden_colour_under_alpha_is_zeroed(fx):
    data = fx["png_hidden_alpha.png"]
    result = strip_image(data)
    out = rgba(result.data)
    transparent = out[..., 3] == 0
    assert transparent.sum() == 1200
    assert not out[transparent].any()
    assert np.array_equal(out[~transparent], rgba(data)[~transparent])


def test_16bit_png_precision_and_colour_conversion(fx):
    from imgstrip import codecs

    data = fx["png_16bit_adobe.png"]
    result = strip_image(data)
    assert result.pixels_exact
    kind = codecs.high_bit_depth_kind(Image.open(io.BytesIO(result.data)))
    assert kind == "RGB"
    arr = codecs.decode_16bit(lambda: Image.open(io.BytesIO(result.data)), 0, "RGB")
    assert arr.dtype == np.uint16 and (arr & 0xFF).any()
    assert any("Adobe RGB" in a for a in result.actions)


@pytest.mark.parametrize("primaries,gamma", [(fixtures.ADOBE_RGB, 563 / 256), (fixtures.DISPLAY_P3, None)])
def test_16bit_colour_engine_matches_littlecms(primaries, gamma):
    """The numpy matrix/TRC engine agrees with LittleCMS on 8-bit-representable input."""
    from imgstrip import icc

    rng = np.random.default_rng(0)
    src8 = rng.integers(0, 256, (128, 128, 3), dtype=np.uint8)
    profile = fixtures.rgb_profile("test", primaries, gamma)
    lcms = np.asarray(icc.convert_image_8bit(Image.fromarray(src8), profile)).astype(int)
    ours = icc.MatrixShaper(profile).to_srgb_16bit(src8.astype(np.uint16) * 257)
    ours8 = ((ours.astype(np.uint32) + 128) // 257).astype(int)
    assert np.abs(lcms - ours8).max() <= 1


def _durations(im, start=0):
    out = []
    for i in range(start, im.n_frames):
        im.seek(i)
        im.load()
        out.append(im.info.get("duration"))
    return out


def test_apng_default_image_dropped_and_timing_kept(fx):
    src = Image.open(io.BytesIO(fx["apng_default.png"]))
    assert src.info.get("default_image") and src.n_frames == 3
    result = strip_image(fx["apng_default.png"])
    im = Image.open(io.BytesIO(result.data))
    assert im.n_frames == 2 and not im.info.get("default_image")
    assert im.info.get("loop") == 2
    assert _durations(im) == _durations(src, start=1)
    for i in range(2):
        src.seek(i + 1)
        im.seek(i)
        assert np.array_equal(np.asarray(src.convert("RGBA")), np.asarray(im.convert("RGBA")))


def test_gif_animation_exact(fx):
    data = fx["gif_animated.gif"]
    result = strip_image(data)
    src, out = Image.open(io.BytesIO(data)), Image.open(io.BytesIO(result.data))
    assert out.n_frames == src.n_frames == 4
    assert out.info.get("loop") == 0
    for i in range(4):
        src.seek(i)
        out.seek(i)
        assert np.array_equal(visible(np.asarray(src.convert("RGBA"))), visible(np.asarray(out.convert("RGBA"))))
        assert out.info["duration"] == src.info["duration"]


def test_gif_without_loop_extension_plays_once():
    frames = [Image.new("P", (8, 8), i) for i in range(3)]
    for f in frames:
        f.putpalette([0, 0, 0, 255, 0, 0, 0, 255, 0])
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=100)
    assert b"NETSCAPE" not in buf.getvalue()
    result = strip_image(buf.getvalue())
    assert b"NETSCAPE" not in result.data
    assert Image.open(io.BytesIO(result.data)).n_frames == 3


def test_gif_frames_with_many_palettes_stay_exact():
    """Each frame uses its own 200-colour local palette; the composite has >256 colours."""
    rng = np.random.default_rng(5)
    frames = []
    for i in range(3):
        pal = rng.integers(0, 256, (200, 3), dtype=np.uint8)
        idx = np.zeros((30, 40), np.uint8)
        idx[i * 10 : i * 10 + 10] = rng.integers(0, 200, (10, 40))
        im = Image.frombytes("P", (40, 30), idx.tobytes())
        im.putpalette(pal.tobytes())
        frames.append(im)
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=50, optimize=False)
    result = strip_image(buf.getvalue())
    assert result.pixels_exact
    src, out = Image.open(io.BytesIO(buf.getvalue())), Image.open(io.BytesIO(result.data))
    for i in range(3):
        src.seek(i)
        out.seek(i)
        assert np.array_equal(np.asarray(src.convert("RGBA")), np.asarray(out.convert("RGBA")))


def test_webp_animation_kept(fx):
    result = strip_image(fx["webp_animated.webp"])
    im = Image.open(io.BytesIO(result.data))
    assert im.n_frames == 3 and im.info.get("loop") == 0
    assert result.pixels_exact


def test_tiff_thumbnail_page_removed(fx):
    data = fx["tiff_multipage.tif"]
    result = strip_image(data)
    out = Image.open(io.BytesIO(result.data))
    assert out.n_frames == 2
    src = Image.open(io.BytesIO(data))
    for i in range(2):
        src.seek(i)
        out.seek(i)
        assert np.array_equal(np.asarray(src), np.asarray(out))


def test_cmyk_jpeg_stays_cmyk(fx):
    result = strip_image(fx["cmyk.jpg"])
    out = decode(result.data)
    assert out.mode == "CMYK"
    src = np.asarray(decode(fx["cmyk.jpg"])).astype(int)
    assert np.abs(np.asarray(out).astype(int) - src).mean() < 3


def test_avif_orientation_applied(fx):
    result = strip_image(fx["avif_everything.avif"])  # 64x48, orientation 8
    assert result.size == (48, 64)


@pytest.mark.skipif(not heif_available(), reason="pillow-heif not installed")
def test_heic_rotation_applied_once(fx):
    result = strip_image(fx["heic_everything.heic"])  # 64x48, orientation 6 -> irot
    assert result.size == (48, 64)
    jpeg = strip_image(fx["heic_everything.heic"], Options(format="jpeg"))
    assert decode(jpeg.data).size == (48, 64)


def test_avif_clean_aperture_crop_removes_hidden_pixels():
    from isobmff_tools import add_clap

    arr = np.full((48, 64, 3), 90, np.uint8)
    arr[:, :16] = (255, 0, 255)  # "secret" band outside the visible crop
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "AVIF", quality=100, subsampling="4:4:4")
    clapped = add_clap(buf.getvalue(), 48, 48, (16, 2), (0, 1))
    assert any("visible crop" in f.label for f in inspect_bytes(clapped).findings)
    result = strip_image(clapped)
    assert result.size == (48, 48)
    out = rgba(result.data).astype(int)
    assert not ((out[..., 0] > 200) & (out[..., 1] < 60)).any()


def test_scrub_lsb_changes_at_most_one_level_and_destroys_payload(fx):
    from imgstrip.analysis import lsb_payload_estimate

    clean = fixtures.photo(200, 150, 4)
    buf = io.BytesIO()
    Image.fromarray(clean).save(buf, "PNG")
    result = strip_image(buf.getvalue(), Options(scrub_lsb=True))
    out = np.asarray(decode(result.data)).astype(int)
    assert np.abs(out - clean.astype(int)).max() == 1
    # The LSB plane is now random noise, which steganalysis reads as a large payload
    # (estimates on this small image range ~0.65-1.0 over many runs).
    assert max(lsb_payload_estimate(out[..., c].astype(np.uint8)) for c in range(3)) > 0.5
    # ... and carries no information about the original LSB plane.
    agreement = ((out & 1) == (clean & 1)).mean()
    assert 0.45 < agreement < 0.55


def test_scrub_lsb_keeps_opaque_and_transparent_alpha(fx):
    result = strip_image(fx["webp_lossless_alpha.webp"], Options(scrub_lsb=True))
    src, out = rgba(fx["webp_lossless_alpha.webp"]), rgba(result.data)
    assert np.array_equal(src[..., 3] == 255, out[..., 3] == 255)
    assert np.array_equal(src[..., 3] == 0, out[..., 3] == 0)


@pytest.mark.parametrize("policy", ["convert", "sanitize", "drop"])
def test_icc_policies(fx, policy):
    result = strip_image(fx["png_everything.png"], Options(icc=policy))
    report = inspect_bytes(result.data)
    has_icc = any(f.label == "ICC color profile" for f in report.findings)
    assert has_icc == (policy == "sanitize")
    if policy == "sanitize":
        assert b"CANARY" not in report.info["icc"]
        assert b"desc" in report.info["icc"]


def test_sanitized_profile_preserves_appearance(fx):
    """Converting the original and the sanitised-profile output to sRGB gives the same colours."""
    from imgstrip import icc

    data = fx["png_everything.png"]
    original = decode(data)
    profile = original.info["icc_profile"]
    sanitized = strip_image(data, Options(icc="sanitize"))
    out = decode(sanitized.data)
    a = np.asarray(icc.convert_image_8bit(original.convert("RGB"), profile)).astype(int)
    b = np.asarray(icc.convert_image_8bit(out.convert("RGB"), out.info["icc_profile"])).astype(int)
    assert np.abs(a - b).max() <= 1


def _profile_with_short_curves() -> bytes:
    """A profile LittleCMS accepts, whose tone curves claim more table entries than they hold."""
    import struct

    icc = bytearray(fixtures.rgb_profile("Short curves", fixtures.ADOBE_RGB))
    curve = b"curv\x00\x00\x00\x00" + struct.pack(">I", 4096) + np.linspace(0, 65535, 256).astype(">u2").tobytes()
    (count,) = struct.unpack_from(">I", icc, 128)
    for i in range(count):
        sig = icc[132 + 12 * i : 136 + 12 * i]
        if sig in (b"rTRC", b"gTRC", b"bTRC"):
            struct.pack_into(">II", icc, 136 + 12 * i, len(icc), len(curve))
    icc += curve
    struct.pack_into(">I", icc, 0, len(icc))
    return bytes(icc)


@pytest.mark.parametrize(
    "source, options",
    [("jpeg", Options(icc="sanitize")), ("jpeg", Options(icc="sanitize", jpeg_lossless=True)), ("png16", Options())],
)
def test_a_malformed_profile_falls_back_instead_of_failing(source, options):
    from imgstrip.codecs import encode_png

    profile = _profile_with_short_curves()
    if source == "jpeg":
        buf = io.BytesIO()
        Image.fromarray(fixtures.photo()).save(buf, "JPEG", icc_profile=profile)
        data = buf.getvalue()
    else:
        data = encode_png(fixtures.photo(40, 30).astype(np.uint16) * 257, 16, 2, icc=profile)
    result = strip_image(data, options)
    assert any("ICC profile" in w for w in result.warnings)
    assert_clean(result.data)


def test_quality_option():
    buf = io.BytesIO()
    Image.fromarray(fixtures.photo(200, 150)).save(buf, "JPEG", quality=95)
    small = strip_image(buf.getvalue(), Options(quality=30)).data
    large = strip_image(buf.getvalue(), Options(quality=95)).data
    assert len(small) < len(large)


def test_optimize_never_grows_png(fx):
    plain = strip_image(fx["png_everything.png"]).data
    optimized = strip_image(fx["png_everything.png"], Options(optimize=True)).data
    assert len(optimized) <= len(plain)
    assert np.array_equal(rgba(plain), rgba(optimized))


def test_png_colour_type_reduction_is_lossless():
    gray = np.repeat(fixtures.photo(50, 40)[..., :1], 3, axis=2)
    rgba_opaque = np.dstack([gray, np.full(gray.shape[:2], 255, np.uint8)])
    buf = io.BytesIO()
    Image.fromarray(rgba_opaque, "RGBA").save(buf, "PNG")
    result = strip_image(buf.getvalue())
    assert decode(result.data).mode in ("L", "P")
    assert np.array_equal(rgba(result.data), rgba_opaque)


def test_too_many_pixels_rejected_before_decoding():
    import struct
    import zlib

    def chunk(t, b):
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)

    bomb = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 100000, 100000, 8, 2, 0, 0, 0))
    bomb += chunk(b"IDAT", zlib.compress(b"\x00" * 1000)) + chunk(b"IEND", b"")
    with pytest.raises(StripError, match="limit"):
        strip_image(bomb)


def test_empty_and_garbage_inputs():
    with pytest.raises(StripError, match="empty"):
        strip_image(b"")
    with pytest.raises(StripError, match="not a supported image"):
        strip_image(b"hello world, definitely not an image")


def test_truncated_image_fails_cleanly(fx):
    data = fx["png_everything.png"]
    with pytest.raises(StripError):
        strip_image(data[: len(data) // 2])
    buf = io.BytesIO()
    Image.fromarray(fixtures.photo()).save(buf, "JPEG")
    with pytest.raises(StripError):
        strip_image(buf.getvalue()[:-400])


def test_corrupt_exif_and_invalid_orientation_tolerated():
    base = io.BytesIO()
    Image.fromarray(fixtures.photo(40, 30)).save(base, "JPEG")
    bad_exif = b"Exif\x00\x00MM\x00*\x00\x00\x00\x08\xff\xff\xff\xff garbage"
    seg = b"\xff\xe1" + (len(bad_exif) + 2).to_bytes(2, "big") + bad_exif
    data = base.getvalue()[:2] + seg + base.getvalue()[2:]
    result = strip_image(data)
    assert result.size == (40, 30)
    weird = fixtures.exif_blob(orientation=42, gps=False)
    seg = b"\xff\xe1" + (len(weird) + 2).to_bytes(2, "big") + weird
    result = strip_image(base.getvalue()[:2] + seg + base.getvalue()[2:])
    assert result.size == (40, 30)
    assert any("orientation" in w for w in result.warnings)


def test_xmp_only_orientation_is_ignored():
    """Viewers ignore XMP tiff:Orientation, so rotating on it would change the picture."""
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_itxt("XML:com.adobe.xmp", '<x:xmpmeta><rdf:Description tiff:Orientation="6"/></x:xmpmeta>')
    buf = io.BytesIO()
    Image.fromarray(fixtures.photo(40, 30)).save(buf, "PNG", pnginfo=info)
    assert strip_image(buf.getvalue()).size == (40, 30)


@pytest.mark.parametrize("size", [(1, 1), (1, 300), (300, 1), (2, 3)])
def test_tiny_and_extreme_aspect_images(size):
    for fmt in ("PNG", "JPEG", "GIF", "WEBP", "TIFF", "BMP"):
        buf = io.BytesIO()
        Image.new("RGB", size, (200, 10, 30)).save(buf, fmt)
        result = strip_image(buf.getvalue())
        assert decode(result.data).size == size


@pytest.mark.parametrize("orientation", range(1, 9))
def test_all_orientations_match_reference(orientation):
    from imgstrip.core import _TRANSPOSE

    ref = fixtures.photo(60, 40)
    stored = Image.fromarray(ref)
    # Store the picture so that applying ``orientation`` restores it.
    inverse = {1: None, 2: 2, 3: 3, 4: 4, 5: 5, 6: 8, 7: 7, 8: 6}[orientation]
    if inverse:
        stored = stored.transpose(_TRANSPOSE[inverse])
    buf = io.BytesIO()
    stored.save(buf, "PNG", exif=fixtures.exif_blob(orientation, gps=False))
    result = strip_image(buf.getvalue())
    assert np.array_equal(np.asarray(decode(result.data).convert("RGB")), ref)


def test_16bit_orientation_uses_numpy_path():
    from imgstrip import codecs
    from imgstrip.core import _TRANSPOSE, _TRANSPOSE_NP

    rng = np.random.default_rng(1)
    arr8 = rng.integers(0, 256, (5, 7, 3), dtype=np.uint8)
    for o in range(2, 9):
        want = np.asarray(Image.fromarray(arr8).transpose(_TRANSPOSE[o]))
        assert np.array_equal(_TRANSPOSE_NP[o](arr8), want), o
    del codecs


def test_wrong_extension_detected_by_content(tmp_path):
    buf = io.BytesIO()
    Image.fromarray(fixtures.photo(20, 10)).save(buf, "PNG")
    result = strip_image(buf.getvalue())  # name is irrelevant: content decides
    assert result.output_format == "PNG"


def test_grayscale_and_bilevel_and_16bit_gray_png():
    for mode, arr in (("L", fixtures.photo(30, 20)[..., 0]), ("1", None), ("I;16", None)):
        if mode == "1":
            im = Image.fromarray(fixtures.photo(30, 20)[..., 0] > 128)
        elif mode == "I;16":
            im = Image.frombytes("I;16", (30, 20), (np.arange(600, dtype="<u2") * 97).tobytes())
        else:
            im = Image.fromarray(arr)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        result = strip_image(buf.getvalue())
        assert result.pixels_exact
        assert np.array_equal(np.asarray(decode(result.data)), np.asarray(im))


def _png_with_chunks(arr, *chunks):
    import struct
    import zlib

    def chunk(t, b):
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)

    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG")
    d = buf.getvalue()
    pos = d.index(b"IDAT") - 4
    return d[:pos] + b"".join(chunk(t, b) for t, b in chunks) + d[pos:]


def test_png_gamma_converted_like_a_browser():
    import struct

    ramp = np.tile(np.arange(256, dtype=np.uint8), (4, 1))
    data = _png_with_chunks(ramp, (b"gAMA", struct.pack(">I", 55556)))  # encoded with gamma 1/1.8
    result = strip_image(data)
    got = np.asarray(decode(result.data))[0].astype(float)
    linear = (np.arange(256) / 255) ** 1.8
    want = np.where(linear <= 0.0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055) * 255
    assert np.abs(got - want).max() < 2
    assert inspect_bytes(result.data).clean


def test_png_srgb_gamma_left_alone():
    import struct

    arr = fixtures.photo(40, 30)
    data = _png_with_chunks(arr, (b"gAMA", struct.pack(">I", 45455)))
    result = strip_image(data)
    assert np.array_equal(np.asarray(decode(result.data)), arr)


def test_png_chrm_wide_gamut_converted():
    import struct

    arr = fixtures.photo(40, 30)
    adobe = (0.3127, 0.3290, 0.64, 0.33, 0.21, 0.71, 0.15, 0.06)
    chrm = struct.pack(">8I", *(int(round(v * 100000)) for v in adobe))
    data = _png_with_chunks(arr, (b"gAMA", struct.pack(">I", 45455)), (b"cHRM", chrm))
    result = strip_image(data)
    out = np.asarray(decode(result.data)).astype(int)
    assert not np.array_equal(out, arr)  # colours were transformed ...
    assert np.abs(out - arr.astype(int)).mean() < 25  # ... but the picture is the same
    assert inspect_bytes(result.data).clean


def test_16bit_png_gamma_uses_numpy_engine():
    import struct

    from imgstrip.codecs import encode_png

    arr = (np.linspace(0, 65535, 64 * 3).reshape(1, 64, 3).repeat(4, 0)).astype(np.uint16)
    d = encode_png(arr, 16, 2)
    pos = d.index(b"IDAT") - 4
    import zlib

    body = struct.pack(">I", 55556)
    g = struct.pack(">I", 4) + b"gAMA" + body + struct.pack(">I", zlib.crc32(b"gAMA" + body) & 0xFFFFFFFF)
    result = strip_image(d[:pos] + g + d[pos:])
    assert result.pixels_exact and any("16-bit" in a for a in result.actions)


def test_camera_raw_rejected():
    from imgstrip.fixtures import _ifd

    buf = bytearray(b"MM\x00\x2a\x00\x00\x00\x08")
    _ifd(buf, [(256, 4, [1]), (257, 4, [1]), (50706, 1, [1, 4, 0, 0])])
    with pytest.raises(StripError, match="RAW"):
        strip_image(bytes(buf))
    with pytest.raises(StripError, match="RAW"):
        strip_image(b"II*\x00\x10\x00\x00\x00CR\x02\x00" + b"\x00" * 64)


def test_gif_frame_beyond_canvas_is_clipped():
    """Viewers clip frames to the logical screen; pixels outside it are hidden data."""
    import struct

    a = Image.new("P", (16, 16), 1)
    b = Image.new("P", (16, 16), 2)
    for im in (a, b):
        im.putpalette([0, 0, 0, 255, 0, 0, 0, 0, 255])
    buf = io.BytesIO()
    a.save(buf, "GIF", save_all=True, append_images=[b], duration=50)
    data = bytearray(buf.getvalue())
    second = data.index(b"\x2c", data.index(b"\x2c") + 1)
    struct.pack_into("<HH", data, second + 1, 8, 8)  # frame 2 hangs off the 16x16 screen
    result = strip_image(bytes(data))
    out = Image.open(io.BytesIO(result.data))
    assert out.size == (16, 16) and out.n_frames == 2
    assert any("outside the animation canvas" in a for a in result.actions)


def test_semi_transparent_pixels_kept_bit_exact():
    rng = np.random.default_rng(3)
    arr = rng.integers(0, 256, (20, 30, 4), dtype=np.uint8)
    arr[..., 3] = rng.integers(1, 4, (20, 30))  # nearly invisible, but must survive exactly
    buf = io.BytesIO()
    Image.fromarray(arr, "RGBA").save(buf, "PNG")
    result = strip_image(buf.getvalue())
    assert np.array_equal(rgba(result.data), arr)
