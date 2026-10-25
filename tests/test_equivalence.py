"""The default mode's equivalence relation (review item 6): which values of an input reach its output.

Two inputs that differ only in what the default mode discards (metadata, how the file is encoded, colour hidden
under transparent pixels, an orientation tag that the pixels then carry) must give byte-identical outputs.  Inputs
that differ in a value the output keeps (docs/REFERENCE.md, "What the output keeps") give outputs that differ in that
value and nothing else, and Result.kept names it.
"""

from __future__ import annotations

import io
import struct
import zlib

import numpy as np
import pytest
from PIL import Image, ImageCms, JpegImagePlugin, PngImagePlugin

from imgstrip import codecs, fixtures
from imgstrip.core import Options, strip_image

PHOTO = fixtures.photo(96, 64)


def save(img: Image.Image, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def exif(**tags) -> Image.Exif:
    e = Image.Exif()
    e[0x010F] = "CANARY camera"  # Make
    e[0x013B] = "CANARY artist"  # Artist
    for k, v in tags.items():
        e[int(k[1:], 16)] = v
    return e


def png_text(**chunks) -> PngImagePlugin.PngInfo:
    info = PngImagePlugin.PngInfo()
    for k, v in chunks.items():
        info.add_text(k, v)
    return info


def same_output(*inputs: bytes, options: Options | None = None) -> None:
    outputs = [strip_image(data, options or Options()).data for data in inputs]
    assert all(o == outputs[0] for o in outputs[1:])


# -- outside the relation: byte-identical outputs -------------------------------------------------------------


def test_png_encodings_and_metadata_are_discarded():
    img = Image.fromarray(PHOTO)
    raw = np.asarray(img)
    same_output(
        save(img, "PNG"),
        save(img, "PNG", compress_level=0),
        save(img, "PNG", compress_level=9, optimize=True),
        save(img, "PNG", pnginfo=png_text(Author="CANARY", Comment="CANARY secret")),
        save(img, "PNG", exif=exif()),
        save(img, "PNG", dpi=(300, 300)),
        *(codecs.encode_png(raw, 8, 2, None, strategy=s) for s in ("none", "up", "paeth", "adaptive")),
    )


def test_jpeg_encodings_and_metadata_are_discarded():
    img = Image.fromarray(PHOTO)
    same_output(
        save(img, "JPEG", quality=90),
        save(img, "JPEG", quality=90, progressive=True),
        save(img, "JPEG", quality=90, optimize=True),
        save(img, "JPEG", quality=90, restart_marker_blocks=4),
        save(img, "JPEG", quality=90, exif=exif(), comment=b"CANARY comment"),
        save(img, "JPEG", quality=90, dpi=(72, 72)),
    )


def test_palette_order_and_gif_extensions_are_discarded():
    rng = np.random.default_rng(1)
    index = rng.integers(0, 6, (40, 50)).astype(np.uint8)
    colours = rng.integers(0, 256, (6, 3)).astype(np.uint8)
    order = rng.permutation(6)
    a = Image.fromarray(index, "P")
    a.putpalette(colours.tobytes())
    b = Image.fromarray(np.argsort(order).astype(np.uint8)[index], "P")
    b.putpalette(colours[order].tobytes())
    assert np.array_equal(np.asarray(a.convert("RGB")), np.asarray(b.convert("RGB")))
    for fmt, kw in (("GIF", {"comment": b"CANARY"}), ("PNG", {"pnginfo": png_text(Title="CANARY")})):
        same_output(save(a, fmt), save(b, fmt), save(a, fmt, **kw))


def test_tiff_compression_and_tags_are_discarded():
    img = Image.fromarray(PHOTO)
    same_output(
        save(img, "TIFF"),
        save(img, "TIFF", compression="tiff_lzw"),
        save(img, "TIFF", compression="tiff_adobe_deflate"),
        save(img, "TIFF", tiffinfo={315: "CANARY artist", 270: "CANARY description"}),
    )


def test_lossless_webp_settings_and_metadata_are_discarded():
    img = Image.fromarray(PHOTO)
    same_output(
        save(img, "WEBP", lossless=True, method=0),
        save(img, "WEBP", lossless=True, method=6, quality=100),
        save(img, "WEBP", lossless=True, exif=exif()),
    )


def test_colour_under_transparent_pixels_is_discarded():
    rgba = np.dstack([PHOTO, np.full(PHOTO.shape[:2], 255, np.uint8)])
    rgba[10:30, 20:40, 3] = 0
    other = rgba.copy()
    other[10:30, 20:40, :3] = 0  # what a viewer shows there: nothing
    noise = rgba.copy()
    noise[10:30, 20:40, :3] = np.random.default_rng(2).integers(0, 256, (20, 20, 3))
    for fmt in ("PNG", "WEBP", "TIFF"):
        kw = {"lossless": True} if fmt == "WEBP" else {}
        same_output(*(save(Image.fromarray(x), fmt, **kw) for x in (rgba, other, noise)))


def test_an_orientation_tag_is_the_same_as_rotated_pixels():
    img = Image.fromarray(PHOTO)
    tagged = save(img, "PNG", exif=exif(x0112=6))  # 6: shown rotated 90 degrees clockwise
    rotated = save(img.transpose(Image.Transpose.ROTATE_270), "PNG")
    same_output(tagged, rotated)


def test_an_srgb_profile_is_the_same_as_none():
    img = Image.fromarray(PHOTO)
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    same_output(save(img, "PNG"), save(img, "PNG", icc_profile=srgb))


def test_choosing_the_format_removes_it_from_the_relation():
    img = Image.fromarray(PHOTO)
    inputs = [save(img, "PNG"), save(img, "BMP"), save(img, "TIFF"), save(img, "WEBP", lossless=True)]
    outputs = {strip_image(d).output_format for d in inputs}
    assert outputs == {"PNG", "BMP", "TIFF", "WEBP"}  # kept: each output is in its input's format
    same_output(*inputs, options=Options(format="PNG"))


# -- inside the relation: the output keeps the value, and nothing else changes --------------------------------


def frames_of(data: bytes) -> list[np.ndarray]:
    im = Image.open(io.BytesIO(data))
    out = []
    for i in range(getattr(im, "n_frames", 1)):
        im.seek(i)
        out.append(np.asarray(im.convert("RGBA")))
    return out


def durations_of(data: bytes) -> list[int]:
    im = Image.open(io.BytesIO(data))
    out = []
    for i in range(im.n_frames):
        im.seek(i)
        im.load()  # WebP sets the duration when the frame is read
        out.append(im.info.get("duration"))
    return out


def animation(n=3, seed=3):
    rng = np.random.default_rng(seed)
    return [Image.fromarray(rng.integers(0, 256, (24, 32, 3)).astype(np.uint8)) for _ in range(n)]


def apng(frames, durations, **kw) -> bytes:
    return save(frames[0], "PNG", save_all=True, append_images=frames[1:], duration=durations, **kw)


def with_delays(data: bytes, delays: list[tuple[int, int]]) -> bytes:
    """An APNG with each fcTL's delay fraction replaced (num, den)."""
    out, pos, k = bytearray(data[:8]), 8, 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind, body = data[pos + 4 : pos + 8], bytearray(data[pos + 8 : pos + 8 + length])
        if kind == b"fcTL":
            body[20:24] = struct.pack(">HH", *delays[k])
            k += 1
        out += struct.pack(">I", length) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        pos += 12 + length
    return bytes(out)


def test_durations_are_kept_as_whole_milliseconds():
    frames = animation()
    exact = apng(frames, [100, 250, 40])
    fractional = with_delays(exact, [(1001, 10000), (2502, 10000), (399, 10000)])  # 100.1, 250.2, 39.9 ms
    assert durations_of(fractional) != durations_of(exact)
    same_output(exact, fractional)
    result = strip_image(exact)
    assert result.kept["durations"] == [100, 250, 40]
    assert durations_of(result.data) == [100, 250, 40]


def test_timing_loop_and_order_change_only_themselves():
    frames = animation()
    base = strip_image(apng(frames, [100, 100, 100], loop=0))
    slower = strip_image(apng(frames, [100, 300, 100], loop=0))
    looped = strip_image(apng(frames, [100, 100, 100], loop=3))
    reordered = strip_image(apng(frames[::-1], [100, 100, 100], loop=0))
    for other in (slower, looped, reordered):
        assert other.data != base.data
    want = frames_of(base.data)
    assert all(np.array_equal(a, b) for a, b in zip(frames_of(slower.data), want, strict=True))
    assert all(np.array_equal(a, b) for a, b in zip(frames_of(looped.data), want, strict=True))
    assert all(np.array_equal(a, b) for a, b in zip(frames_of(reordered.data), want[::-1], strict=True))
    assert durations_of(slower.data) == [100, 300, 100] and slower.kept["durations"] == [100, 300, 100]
    assert Image.open(io.BytesIO(looped.data)).info["loop"] == 3 and looped.kept["loop"] == 3
    assert base.kept["frames"] == 3 and base.kept["loop"] == 0


@pytest.mark.parametrize("fmt", ["GIF", "WEBP"])
def test_other_animated_formats_keep_the_same_values(fmt):
    frames = animation()
    kw = {"lossless": True} if fmt == "WEBP" else {}
    result = strip_image(
        save(frames[0], fmt, save_all=True, append_images=frames[1:], duration=[50, 120, 80], loop=2, **kw)
    )
    assert result.kept["frames"] == 3 and result.kept["durations"] == [50, 120, 80] and result.kept["loop"] == 2
    assert durations_of(result.data) == [50, 120, 80]


@pytest.mark.parametrize("subsampling", [0, 1, 2])
def test_jpeg_chroma_subsampling_is_kept(subsampling):
    data = save(Image.fromarray(PHOTO), "JPEG", quality=90, subsampling=subsampling)
    result = strip_image(data)
    assert JpegImagePlugin.get_sampling(Image.open(io.BytesIO(result.data))) == subsampling
    assert result.kept["chroma"] == {0: "4:4:4", 1: "4:2:2", 2: "4:2:0"}[subsampling]
    assert "chroma" not in strip_image(data, Options(format="PNG")).kept


@pytest.mark.parametrize("out", ["PNG", "TIFF", "BMP", "JPEG", "WEBP", "AVIF", "GIF"])
def test_the_colour_model_follows_the_pixels_not_the_input(out):
    """Grey stored as grey or as colour, an alpha channel that is opaque everywhere or none, a palette or the same
    colours stored directly: the same pixels, so the same output."""
    grey = Image.fromarray(PHOTO).convert("L")
    few = Image.fromarray(PHOTO).quantize(16)
    options = Options(format=out)
    same_output(save(grey, "PNG"), save(grey.convert("RGB"), "PNG"), save(grey.convert("RGBA"), "PNG"), options=options)
    colour = Image.fromarray(PHOTO)
    same_output(save(colour, "PNG"), save(colour.convert("RGBA"), "PNG"), options=options)
    same_output(save(few, "PNG"), save(few.convert("RGB"), "PNG"), save(few.convert("RGBA"), "PNG"), options=options)


def test_cmyk_and_16_bit_depth_are_kept():
    cmyk = strip_image(save(Image.fromarray(PHOTO).convert("CMYK"), "JPEG", quality=90))
    assert cmyk.kept["colour"] == "CMYK" and Image.open(io.BytesIO(cmyk.data)).mode == "CMYK"
    assert "colour" not in strip_image(save(Image.fromarray(PHOTO), "JPEG", quality=90)).kept
    deep = codecs.encode_png(PHOTO.astype(np.uint16) * 257, 16, 2, None)  # 16-bit samples that 8 bits could hold
    result = strip_image(deep)
    assert result.kept["depth"] == 16
    assert result.data != strip_image(save(Image.fromarray(PHOTO), "PNG")).data


def test_lossless_webp_stays_lossless_and_lossy_stays_lossy():
    img = Image.fromarray(PHOTO)
    assert strip_image(save(img, "WEBP", lossless=True)).kept["lossless"] is True
    assert strip_image(save(img, "WEBP", quality=80)).kept["lossless"] is False
    assert "lossless" not in strip_image(save(img, "WEBP", quality=80), Options(format="PNG")).kept


def test_kept_lists_only_what_came_from_the_input():
    img = Image.fromarray(PHOTO)
    assert strip_image(save(img, "PNG")).kept == {"format": "PNG"}
    assert strip_image(save(img, "PNG"), Options(format="JPEG")).kept == {}
    assert strip_image(save(img, "JPEG", subsampling=2)).kept == {"format": "JPEG", "chroma": "4:2:0"}
    assert strip_image(save(img, "JPEG"), Options(scrub_dct="canonical")).kept == {
        "format": "JPEG",
        "dct": "coefficients",
    }
    sanitised = strip_image(fixtures.all_fixtures()["png_everything.png"], Options(icc="sanitize"))
    assert sanitised.kept.get("profile") == "rebuilt from the input's"
