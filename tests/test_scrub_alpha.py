"""The transparency channel as a hiding place: ``--scrub-alpha`` / ``Options(scrub_alpha=True)`` (new in 1.2.8).

``--scrub-lsb`` keeps alpha 0 and 255 exactly, so a message written as 255-versus-254 over a picture's solid areas, or
0-versus-1 over its clear ones, passed through it. The alpha scrub snaps alpha within a tolerance of fully opaque or
fully clear to exactly that, drops the colour under what became clear, and randomises the lowest bit of the rest.

These tests pin: what it destroys and, as honestly, what it does not; that it is off by default and then changes
nothing; every image kind that can carry transparency (and the ones that cannot); odd settings; and that the command
line, the web API, the credentials and the self-test all tell the same story.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from imgstrip import codecs, credentials, selftest, stego
from imgstrip.cli import main
from imgstrip.core import Frame, Options, Report, StripError, _Job, canonical_frames, strip_image
from imgstrip.web import server as web
from test_web import app  # noqa: F401  (the fixture)

H = W = 96


# --------------------------------------------------------------------------
# Builders and readers
# --------------------------------------------------------------------------


def png_of(arr: np.ndarray, mode: str | None = None, **save) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr, mode).save(buf, "PNG", **save)
    return buf.getvalue()


def alpha_plane(data: bytes, frame: int = 0) -> np.ndarray | None:
    """The alpha channel of an output as stored, or None when the file has no transparency channel at all."""
    a = canonical_frames(data)[frame]
    if a.ndim == 3 and a.shape[2] in (2, 4):
        return a[..., -1]
    return None


def seen_alpha(data: bytes, frame: int = 0) -> np.ndarray:
    """The alpha a viewer sees: the stored channel, or fully opaque."""
    plane = alpha_plane(data, frame)
    if plane is not None:
        return plane
    a = canonical_frames(data)[frame]
    return np.full(a.shape[:2], 255 if a.dtype == np.uint8 else 65535, a.dtype)


def covered(kind: str, d: int = 1, seed: int = 3, size: tuple[int, int] = (H, W)):
    """An RGBA PNG whose alpha channel carries one bit per pixel, plus the bits.

    opaque   255 for a 1, 255-d for a 0 (solid areas)
    clear    d for a 1, 0 for a 0 (invisible areas)
    partial  a random in-between level whose lowest bit is the message
    """
    h, w = size
    rng = np.random.default_rng(seed)
    bits = rng.integers(0, 2, (h, w)).astype(np.uint8)
    rgb = rng.integers(1, 256, (h, w, 3), dtype=np.uint8)
    if kind == "opaque":
        alpha = np.where(bits == 1, 255, 255 - d)
    elif kind == "clear":
        alpha = np.where(bits == 1, d, 0)
    elif kind == "partial":
        alpha = rng.integers(20, 120, (h, w)) * 2 + bits
    else:
        raise AssertionError(kind)
    alpha = alpha.astype(np.uint8)
    rgb[alpha == 0] = 0
    return png_of(np.dstack([rgb, alpha]), "RGBA"), bits


def message_read(kind: str, alpha: np.ndarray, d: int = 1) -> np.ndarray:
    """The bits a receiver who knows the scheme reads back out of ``alpha``."""
    if kind == "opaque":
        return alpha == 255
    if kind == "clear":
        return alpha == d
    return (alpha & 1) == 1


def agreement(kind: str, data: bytes, bits: np.ndarray, d: int = 1) -> float:
    return float((message_read(kind, seen_alpha(data), d) == (bits == 1)).mean())


def gradient_rgba(h: int = 64, w: int = 80, seed: int = 4) -> np.ndarray:
    """A photo-like RGBA array whose alpha runs through every level, with clear and solid margins."""
    rng = np.random.default_rng(seed)
    rgb = rng.integers(1, 256, (h, w, 3), dtype=np.uint8)
    alpha = np.tile(np.linspace(0, 255, w).round().astype(np.uint8), (h, 1))
    alpha[:, :4] = 0
    alpha[:, -4:] = 255
    rgb[alpha == 0] = 0
    return np.dstack([rgb, alpha])


# --------------------------------------------------------------------------
# The pure pieces
# --------------------------------------------------------------------------


def test_the_option_is_off_by_default_and_the_defaults_are_documented_values():
    o = Options()
    assert o.scrub_alpha is False and o.alpha_tolerance == stego.ALPHA_TOLERANCE_DEFAULT == 5
    assert (stego.ALPHA_TOLERANCE_MIN, stego.ALPHA_TOLERANCE_MAX) == (1, 32)


@pytest.mark.parametrize(("given", "used"), [(1, 1), (2, 3), (3, 3), (4, 5), (5, 5), (31, 31), (32, 33)])
def test_an_even_tolerance_is_rounded_up_to_the_next_odd_one(given, used):
    assert stego.effective_alpha_tolerance(given) == used


@pytest.mark.parametrize("bad", [0, -1, 33, 100, True, False, 5.0, 5.5, "5", None, [5]])
def test_the_tolerance_check_rejects_everything_but_a_whole_number_from_1_to_32(bad):
    with pytest.raises(ValueError, match="whole number from 1 to 32"):
        stego.check_alpha_tolerance(bad)


@pytest.mark.parametrize("good", [1, 2, 5, 31, 32])
def test_the_tolerance_check_accepts_the_whole_range(good):
    assert stego.check_alpha_tolerance(good) == good


@pytest.mark.parametrize(
    ("dtype", "full", "scale", "channels"),
    [(np.uint8, 255, 1, 4), (np.uint8, 255, 1, 2), (np.uint16, 65535, 257, 4), (np.uint16, 65535, 257, 2)],
)
@pytest.mark.parametrize("tol", [1, 3, 5, 11, 31])
def test_clean_alpha_snaps_exactly_the_ends_and_keeps_the_rest_inside_its_pair(dtype, full, scale, channels, tol):
    rng = np.random.default_rng(tol * 7 + channels)
    t = tol * scale
    alpha = rng.integers(0, full + 1, (48, 48))
    edges = [0, 1, t - 1, t, t + 1, t + 2, full - t - 2, full - t - 1, full - t, full - t + 1, full - 1, full]
    alpha.flat[: len(edges)] = edges  # every value on either side of both thresholds is present
    arr = rng.integers(1, full, (48, 48, channels)).astype(dtype)
    arr[..., -1] = alpha
    arr[alpha == 0, :-1] = 0  # the engine clears the colour under clear pixels before this stage
    a_in = alpha.astype(np.int64)
    clear, solid = a_in <= t, a_in >= full - t
    partial = ~(clear | solid)
    for seed in range(5):  # random: one lucky run proves nothing
        out, changed = _Job._clean_alpha(arr, np.random.default_rng(seed), t, full)
        a_out = out[..., -1].astype(np.int64)
        assert (a_out[clear] == 0).all() and (a_out[solid] == full).all()
        assert (a_out[partial] >> 1 == a_in[partial] >> 1).all(), "a partial level left its (even, odd) pair"
        assert (a_out[partial] > t).all() and (a_out[partial] < full - t).all(), "a level fell back into a snapped end"
        assert (out[clear][..., :-1] == 0).all(), "colour was left under a clear pixel"
        assert np.array_equal(out[~clear][..., :-1], arr[~clear][..., :-1]), "colour changed where it still shows"
        assert changed == int((a_out != a_in).sum())
        assert out.dtype == arr.dtype and out.shape == arr.shape


def test_clean_alpha_returns_its_input_when_nothing_needs_changing():
    arr = np.zeros((8, 8, 4), np.uint8)
    arr[..., 3] = 255
    out, changed = _Job._clean_alpha(arr, np.random.default_rng(0), 5, 255)
    assert changed == 0 and out is arr


def test_the_partial_levels_are_randomised_with_no_memory_of_the_message():
    alpha = np.full((200, 200), 128, np.uint8)
    arr = np.dstack([np.full((200, 200, 3), 9, np.uint8), alpha])
    out, _ = _Job._clean_alpha(arr, np.random.default_rng(1), 5, 255)
    ones = float((out[..., 3] == 129).mean())
    assert 0.48 < ones < 0.52 and set(np.unique(out[..., 3])) == {128, 129}


def test_alpha_report_in_plain_words():
    assert stego.alpha_report(stego.ALPHA_OFF) is None and stego.alpha_report("nonsense") is None
    none = stego.alpha_report(stego.ALPHA_NO_ALPHA)
    assert none["headline"] == "nothing to clean" and "no transparency channel" in none["summary"] and none["pixels"] == 0
    got = stego.alpha_report(stego.ALPHA_APPLIED, 5, 1234)
    assert got["headline"] == "cleaned" and "within 5 levels" in got["summary"] and "1,234 pixels changed" in got["summary"]
    assert "not proof that nothing is hidden" in got["caveat"] and "5 levels" in got["caveat"]
    assert "1 pixel changed" in stego.alpha_report(stego.ALPHA_APPLIED, 5, 1)["summary"]
    assert "no pixel needed changing" in stego.alpha_report(stego.ALPHA_APPLIED, 5, 0)["summary"]


# --------------------------------------------------------------------------
# What it destroys, and what it honestly does not
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "d"),
    [("opaque", 1), ("opaque", 2), ("opaque", 4), ("clear", 1), ("clear", 3), ("clear", 5), ("partial", 1)],
)
def test_a_message_in_the_alpha_channel_is_gone_after_the_scrub(kind, d):
    data, bits = covered(kind, d)
    assert agreement(kind, strip_image(data).data, bits, d) == 1.0, "a plain clean must not touch alpha"
    result = strip_image(data, Options(scrub_alpha=True))
    assert result.alpha_scrub == stego.ALPHA_APPLIED and result.alpha_pixels > 0
    assert 0.42 < agreement(kind, result.data, bits, d) < 0.58, "the message must read back as chance"


def test_the_old_hole_is_what_the_new_option_closes():
    """255-versus-254 survives the colour scrub in part; with the alpha scrub on it does not survive at all."""
    data, bits = covered("opaque", 1)
    only_colour = strip_image(data, Options(scrub_lsb=True))
    assert only_colour.alpha_scrub == stego.ALPHA_OFF
    assert agreement("opaque", only_colour.data, bits) > 0.65  # the documented gap of --scrub-lsb on its own
    both = strip_image(data, Options(scrub_lsb=True, scrub_alpha=True))
    assert 0.42 < agreement("opaque", both.data, bits) < 0.58


def test_a_message_hidden_with_a_bigger_difference_survives_the_default_tolerance_and_a_wider_one_closes_it():
    """The honest residual: only differences within the tolerance are closed."""
    data, bits = covered("opaque", 11)
    assert agreement("opaque", strip_image(data, Options(scrub_alpha=True)).data, bits, 11) == 1.0
    assert 0.42 < agreement("opaque", strip_image(data, Options(scrub_alpha=True, alpha_tolerance=11)).data, bits, 11) < 0.58


@pytest.mark.parametrize("tolerance", [1, 2, 3, 4, 5, 9, 31, 32])
def test_the_boundary_of_the_tolerance_is_exact(tolerance):
    used = stego.effective_alpha_tolerance(tolerance)
    d = used  # the largest difference from fully opaque that is still snapped
    data, bits = covered("opaque", d)
    got = strip_image(data, Options(scrub_alpha=True, alpha_tolerance=tolerance))
    assert got.alpha_tolerance == used
    assert 0.42 < agreement("opaque", got.data, bits, d) < 0.58
    if used + 1 <= 40:  # one level beyond is outside the tolerance and is not snapped
        data, bits = covered("opaque", used + 1)
        got = strip_image(data, Options(scrub_alpha=True, alpha_tolerance=tolerance))
        assert agreement("opaque", got.data, bits, used + 1) == 1.0


def test_each_tolerance_changes_pixels_by_no_more_than_the_tolerance():
    arr = gradient_rgba()
    data = png_of(arr, "RGBA")
    for tolerance in (1, 5, 11, 32):
        used = stego.effective_alpha_tolerance(tolerance)
        out = strip_image(data, Options(scrub_alpha=True, alpha_tolerance=tolerance))
        a_in = arr[..., 3].astype(int)
        a_out = seen_alpha(out.data).astype(int)
        snapped = (a_in <= used) | (a_in >= 255 - used)
        assert (np.abs(a_out - a_in)[snapped] <= used).all()
        assert (np.abs(a_out - a_in)[~snapped] <= 1).all()  # a partial level moves by one at most
        assert (a_out[a_in <= used] == 0).all() and (a_out[a_in >= 255 - used] == 255).all()


def test_colour_is_untouched_wherever_the_pixel_still_shows():
    arr = gradient_rgba()
    out = strip_image(png_of(arr, "RGBA"), Options(scrub_alpha=True))
    rgba = canonical_frames(out.data)[0]
    shown = rgba[..., 3] > 0
    assert np.array_equal(rgba[..., :3][shown], arr[..., :3][shown])
    assert (rgba[..., :3][~shown] == 0).all()


def test_the_scrub_is_idempotent_on_the_snapped_ends():
    data, _bits = covered("opaque", 3)
    first = strip_image(data, Options(scrub_alpha=True))
    second = strip_image(first.data, Options(scrub_alpha=True))
    a1, a2 = seen_alpha(first.data), seen_alpha(second.data)
    assert np.array_equal(a1 == 0, a2 == 0) and np.array_equal(a1 == 255, a2 == 255)
    assert second.alpha_pixels == 0, "nothing was left to snap, and no level was partial"


def test_faint_pixels_become_clear_and_leave_no_colour_behind():
    rng = np.random.default_rng(8)
    alpha = rng.choice([1, 2, 3, 4, 5], size=(40, 40)).astype(np.uint8)
    arr = np.dstack([rng.integers(1, 256, (40, 40, 3), dtype=np.uint8), alpha])
    for _ in range(3):
        out = strip_image(png_of(arr, "RGBA"), Options(scrub_alpha=True))  # the verifier accepts it
        rgba = canonical_frames(out.data)[0]
        assert (rgba == 0).all() and out.alpha_pixels == 40 * 40


# --------------------------------------------------------------------------
# Off by default: nothing changes
# --------------------------------------------------------------------------


def test_without_the_option_the_alpha_channel_and_the_report_are_untouched():
    data, bits = covered("opaque", 1)
    r = strip_image(data)
    assert r.alpha_scrub == stego.ALPHA_OFF and r.alpha_pixels == 0 and r.alpha_tolerance is None
    assert agreement("opaque", r.data, bits) == 1.0
    assert stego.alpha_report(r.alpha_scrub, r.alpha_tolerance, r.alpha_pixels) is None
    assert not any("transparency" in a for a in r.actions)


def test_the_tolerance_alone_does_nothing_and_is_not_validated_when_the_scrub_is_off():
    data, bits = covered("opaque", 1)
    r = strip_image(data, Options(alpha_tolerance=99))
    assert r.alpha_scrub == stego.ALPHA_OFF and agreement("opaque", r.data, bits) == 1.0


def test_existing_scrubs_give_the_same_result_whether_or_not_the_new_fields_are_set():
    arr = gradient_rgba()
    data = png_of(arr, "RGBA")
    for options in (Options(), Options(scrub_lsb=True)):
        r = strip_image(data, options)
        assert r.alpha_scrub == stego.ALPHA_OFF
        assert np.array_equal(seen_alpha(r.data) == 0, arr[..., 3] == 0)  # exact 0 and 255 stay, as in 1.2.7
        assert np.array_equal(seen_alpha(r.data) == 255, arr[..., 3] == 255)


@pytest.mark.parametrize("bad", [0, -3, 33, True, 2.5, "7", None])
def test_a_bad_tolerance_is_refused_before_anything_is_decoded(bad):
    data, _ = covered("opaque", 1)
    with pytest.raises(StripError, match="whole number from 1 to 32"):
        strip_image(data, Options(scrub_alpha=True, alpha_tolerance=bad))
    with pytest.raises(StripError, match="whole number from 1 to 32"):
        strip_image(data, Options(scrub_alpha=True, alpha_tolerance=bad, sandbox=False))


# --------------------------------------------------------------------------
# Every kind of image that can carry transparency
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["RGBA", "LA"])
def test_rgba_and_la_pngs(mode):
    rng = np.random.default_rng(6)
    bits = rng.integers(0, 2, (H, W)).astype(np.uint8)
    alpha = np.where(bits == 1, 255, 254).astype(np.uint8)
    if mode == "RGBA":
        arr = np.dstack([rng.integers(1, 256, (H, W, 3), dtype=np.uint8), alpha])
    else:
        arr = np.dstack([rng.integers(1, 256, (H, W), dtype=np.uint8), alpha])
    data = png_of(arr, mode)
    assert agreement("opaque", strip_image(data).data, bits) == 1.0
    r = strip_image(data, Options(scrub_alpha=True))
    assert r.alpha_scrub == stego.ALPHA_APPLIED and 0.42 < agreement("opaque", r.data, bits) < 0.58


@pytest.mark.parametrize("channels", [2, 4])
def test_16_bit_alpha(channels):
    rng = np.random.default_rng(9)
    bits = rng.integers(0, 2, (64, 64)).astype(np.uint8)
    arr = rng.integers(1, 65535, (64, 64, channels), dtype=np.uint16)
    arr[..., -1] = np.where(bits == 1, 65535, 65535 - 257)  # 255 against 254 in 8-bit units
    data = codecs.encode_png(arr, 16, 6 if channels == 4 else 4, None)
    plain = strip_image(data)
    assert plain.kept["depth"] == 16 and (alpha_plane(plain.data) == arr[..., -1]).all()
    r = strip_image(data, Options(scrub_alpha=True))
    assert r.alpha_scrub == stego.ALPHA_APPLIED and r.kept["depth"] == 16
    plane = alpha_plane(r.data)
    assert plane is None or (plane == 65535).all(), "the 16-bit levels within the tolerance must become exactly opaque"


def test_16_bit_partial_alpha_is_randomised_and_far_levels_are_kept():
    rng = np.random.default_rng(10)
    arr = rng.integers(1, 65535, (64, 64, 4), dtype=np.uint16)
    arr[..., 3] = rng.integers(5000, 60000, (64, 64))
    data = codecs.encode_png(arr, 16, 6, None)
    plane = alpha_plane(strip_image(data, Options(scrub_alpha=True)).data)
    delta = np.abs(plane.astype(np.int64) - arr[..., 3].astype(np.int64))
    assert delta.max() <= 1 and 0.4 < float(((plane & 1) == (arr[..., 3] & 1)).mean()) < 0.6


def test_palette_png_with_transparency():
    rng = np.random.default_rng(2)
    bits = rng.integers(0, 2, (H, W)).astype(np.uint8)
    im = Image.fromarray(bits, "P")
    im.putpalette([200, 10, 10, 200, 10, 10] + [0, 0, 0] * 254)  # two entries, one colour
    buf = io.BytesIO()
    im.save(buf, "PNG", transparency=bytes([254, 255]))  # alpha 254 / 255 tells them apart
    data = buf.getvalue()
    assert agreement("opaque", strip_image(data).data, bits) == 1.0
    r = strip_image(data, Options(scrub_alpha=True))
    assert r.alpha_scrub == stego.ALPHA_APPLIED and r.alpha_pixels == H * W // 1 - int((bits == 1).sum())
    assert 0.42 < agreement("opaque", r.data, bits) < 0.58


def test_palette_png_with_partial_alpha_is_randomised_per_pixel_not_per_entry():
    """Entries sharing a colour that differ only in the low bit of alpha must not stay distinguishable."""
    rng = np.random.default_rng(3)
    bits = rng.integers(0, 2, (H, W)).astype(np.uint8)
    im = Image.fromarray(bits, "P")
    im.putpalette([40, 90, 200, 40, 90, 200] + [0, 0, 0] * 254)
    buf = io.BytesIO()
    im.save(buf, "PNG", transparency=bytes([100, 101]))
    r = strip_image(buf.getvalue(), Options(scrub_alpha=True))
    plane = seen_alpha(r.data)
    assert set(np.unique(plane)) <= {100, 101} and 0.42 < float(((plane & 1) == bits).mean()) < 0.58


def test_gif_transparency_is_kept_exactly():
    frames = []
    for seed in (1, 2, 3):
        rng = np.random.default_rng(seed)
        im = Image.fromarray(rng.integers(0, 6, (48, 48)).astype(np.uint8), "P")
        im.putpalette([v for c in range(6) for v in (c * 40, 255 - c * 40, c * 20)] + [0] * (3 * 250))
        frames.append(im)
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=[40, 60, 80], loop=0, transparency=0, disposal=2)
    data = buf.getvalue()
    plain, cleaned = strip_image(data), strip_image(data, Options(scrub_alpha=True))
    assert cleaned.frames == plain.frames == 3 and cleaned.output_format == "GIF"
    assert cleaned.kept["durations"] == plain.kept["durations"]
    for i in range(3):
        assert np.array_equal(seen_alpha(cleaned.data, i) == 0, seen_alpha(plain.data, i) == 0)


def test_every_frame_of_an_animation_is_cleaned():
    frames, all_bits = [], []
    for seed in (11, 12, 13):
        data, bits = covered("opaque", 1, seed=seed, size=(48, 48))
        frames.append(Image.open(io.BytesIO(data)).convert("RGBA"))
        all_bits.append(bits)
    buf = io.BytesIO()
    frames[0].save(buf, "PNG", save_all=True, append_images=frames[1:], duration=50, loop=0)
    source = buf.getvalue()
    r = strip_image(source, Options(scrub_alpha=True))
    assert r.frames == 3 and r.alpha_scrub == stego.ALPHA_APPLIED
    for i, bits in enumerate(all_bits):
        assert 0.4 < float((message_read("opaque", seen_alpha(r.data, i)) == (bits == 1)).mean()) < 0.6, f"frame {i}"


def test_webp_lossless_alpha_fixture(fx):
    src = canonical_frames(fx["webp_lossless_alpha.webp"])[0][..., 3]
    r = strip_image(fx["webp_lossless_alpha.webp"], Options(scrub_alpha=True))
    out = seen_alpha(r.data)
    assert ((src == 255) <= (out == 255)).all() and ((src == 0) <= (out == 0)).all()  # ends only ever grow
    assert r.alpha_scrub == stego.ALPHA_APPLIED


@pytest.mark.parametrize("fmt", ["PNG", "WEBP", "TIFF", "GIF", "BMP", "JPEG", "AVIF"])
def test_every_output_format_accepts_a_cleaned_transparent_picture(fmt):
    if fmt not in web.available_formats():
        pytest.skip(f"{fmt} output is not available here")
    data = png_of(gradient_rgba(), "RGBA")
    r = strip_image(data, Options(scrub_alpha=True, format=fmt))
    assert r.output_format == fmt and r.alpha_scrub == stego.ALPHA_APPLIED
    assert Image.open(io.BytesIO(r.data)).size == (80, 64)


@pytest.mark.parametrize("fmt", ["WEBP", "TIFF", "AVIF"])
def test_native_rgba_files_in_other_formats(fmt):
    if fmt not in web.available_formats():
        pytest.skip(f"{fmt} is not available here")
    buf = io.BytesIO()
    Image.fromarray(gradient_rgba(), "RGBA").save(buf, fmt, **({"lossless": True} if fmt == "WEBP" else {}))
    r = strip_image(buf.getvalue(), Options(scrub_alpha=True))
    assert r.output_format == fmt and r.alpha_scrub == stego.ALPHA_APPLIED


def test_alpha_hidden_message_does_not_leak_into_a_flattened_jpeg():
    """Saving as JPEG blends alpha into the colour, so a 254-versus-255 message used to reach the JPEG's pixels."""
    with_message, _bits = covered("opaque", 1, seed=21)
    flat = png_of(np.dstack([canonical_frames(with_message)[0][..., :3], np.full((H, W), 255, np.uint8)]), "RGBA")
    options = Options(scrub_alpha=True, format="JPEG")
    assert strip_image(with_message, options).data == strip_image(flat, options).data
    assert strip_image(with_message, Options(format="JPEG")).data != strip_image(flat, Options(format="JPEG")).data


# --------------------------------------------------------------------------
# Images with nothing to clean
# --------------------------------------------------------------------------


def _same_pixels(a: bytes, b: bytes) -> bool:
    fa, fb = canonical_frames(a), canonical_frames(b)
    return len(fa) == len(fb) and all(np.array_equal(x, y) for x, y in zip(fa, fb, strict=True))


def _no_alpha_images():
    rng = np.random.default_rng(12)
    rgb = rng.integers(0, 256, (48, 48, 3), dtype=np.uint8)
    pal = Image.fromarray((rgb[..., 0] // 64).astype(np.uint8), "P")
    pal.putpalette([v for c in range(4) for v in (c * 60, 10, 200 - c * 40)] + [0] * (3 * 252))
    pal_buf = io.BytesIO()
    pal.save(pal_buf, "PNG")
    return {
        "rgb": png_of(rgb, "RGB"),
        "gray": png_of(rgb[..., 0], "L"),
        "palette": pal_buf.getvalue(),
        "one-bit": png_of(rng.integers(0, 2, (48, 48)).astype(bool)),
        "gray-16": codecs.encode_png(rng.integers(0, 65535, (48, 48), dtype=np.uint16), 16, 0, None),
        "rgb-16": codecs.encode_png(rng.integers(0, 65535, (48, 48, 3), dtype=np.uint16), 16, 2, None),
    }


@pytest.mark.parametrize("name", list(_no_alpha_images()))
def test_an_image_without_transparency_is_reported_as_having_nothing_to_clean(name):
    data = _no_alpha_images()[name]
    r = strip_image(data, Options(scrub_alpha=True))
    assert r.alpha_scrub == stego.ALPHA_NO_ALPHA and r.alpha_pixels == 0 and r.alpha_tolerance is None
    assert _same_pixels(r.data, strip_image(data).data)
    assert not any("transparency" in a for a in r.actions) and r.pixels_exact == strip_image(data).pixels_exact
    assert stego.alpha_report(r.alpha_scrub, r.alpha_tolerance, r.alpha_pixels)["headline"] == "nothing to clean"


def test_jpeg_files_have_no_transparency_whatever_the_mode(fx):
    for options in (
        Options(),
        Options(jpeg_lossless=True),
        Options(scrub_dct="canonical"),
        Options(scrub_dct="full", dct_min_psnr=0),
    ):
        asked = Options(**{**options.__dict__, "scrub_alpha": True})
        r = strip_image(fx["jpeg_everything.jpg"], asked)
        assert r.alpha_scrub == stego.ALPHA_NO_ALPHA, options
    for options in (Options(jpeg_lossless=True), Options(scrub_dct="canonical")):  # deterministic: bytes must match
        asked = Options(**{**options.__dict__, "scrub_alpha": True})
        assert strip_image(fx["jpeg_everything.jpg"], asked).data == strip_image(fx["jpeg_everything.jpg"], options).data


def test_an_opaque_rgba_picture_needs_no_change():
    arr = np.dstack([np.random.default_rng(1).integers(0, 256, (40, 40, 3), dtype=np.uint8), np.full((40, 40), 255, np.uint8)])
    data = png_of(arr, "RGBA")
    r = strip_image(data, Options(scrub_alpha=True))
    assert r.alpha_scrub == stego.ALPHA_APPLIED and r.alpha_pixels == 0 and r.pixels_exact
    assert r.data == strip_image(data).data


# --------------------------------------------------------------------------
# Odd sizes and values
# --------------------------------------------------------------------------


@pytest.mark.parametrize("size", [(1, 1), (1, 37), (37, 1), (2, 2), (3, 5)])
def test_tiny_images(size):
    h, w = size
    rng = np.random.default_rng(h * 100 + w)
    arr = np.dstack([rng.integers(1, 256, (h, w, 3), dtype=np.uint8), rng.choice([1, 128, 254, 255], (h, w)).astype(np.uint8)])
    r = strip_image(png_of(arr, "RGBA"), Options(scrub_alpha=True))
    assert r.size == (w, h) and r.alpha_scrub == stego.ALPHA_APPLIED
    a_in = arr[..., 3].astype(int)
    a_out = seen_alpha(r.data).astype(int)
    assert (a_out[a_in == 1] == 0).all() and (a_out[a_in == 254] == 255).all() and (a_out[a_in == 255] == 255).all()


@pytest.mark.parametrize("level", [0, 1, 5, 6, 128, 249, 250, 254, 255])
def test_a_picture_with_one_alpha_level_everywhere(level):
    arr = np.dstack([np.full((24, 24, 3), 0 if level == 0 else 77, np.uint8), np.full((24, 24), level, np.uint8)])
    r = strip_image(png_of(arr, "RGBA"), Options(scrub_alpha=True))
    out = seen_alpha(r.data)
    if level <= 5:
        assert (out == 0).all()
    elif level >= 250:
        assert (out == 255).all()
    else:
        assert set(np.unique(out)) <= {level & ~1, level | 1} and (np.abs(out.astype(int) - level) <= 1).all()


def test_the_sandbox_and_in_process_decoding_agree():
    data, bits = covered("opaque", 2)
    for sandbox in (True, False):
        r = strip_image(data, Options(scrub_alpha=True, sandbox=sandbox))
        assert r.alpha_scrub == stego.ALPHA_APPLIED and 0.42 < agreement("opaque", r.data, bits, 2) < 0.58


def test_strips_of_a_large_image_join_without_a_seam():
    """Alpha is cleaned in strips of rows: values must come out right on both sides of every strip edge."""
    h = w = 1600  # 10 MB of RGBA: three strips
    y, x = np.mgrid[0:h, 0:w]
    alpha = ((x + 3 * y) % 256).astype(np.uint8)
    arr = np.dstack([np.full((h, w, 3), 90, np.uint8), alpha])
    arr[alpha == 0, :3] = 0
    job = _Job(b"", Options(scrub_alpha=True), Report("PNG"))
    frame = Frame(image=Image.fromarray(arr, "RGBA"))
    job._scrub_alpha([frame])
    out = np.asarray(frame.image)
    a_in = alpha.astype(int)
    a_out = out[..., 3].astype(int)
    assert job.alpha_scrub == stego.ALPHA_APPLIED
    assert (a_out[a_in <= 5] == 0).all() and (a_out[a_in >= 250] == 255).all()
    mid = (a_in > 5) & (a_in < 250)
    assert (a_out[mid] >> 1 == a_in[mid] >> 1).all()
    assert (out[..., :3][a_out == 0] == 0).all() and (out[..., :3][a_out > 0] == 90).all()
    assert job.alpha_pixels == int((a_out != a_in).sum())


# --------------------------------------------------------------------------
# With the other scrubs, and after them
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"scrub_lsb": True},
        {"scrub_prnu": True},
        {"scrub_prnu": True, "scrub_lsb": True},
        {"icc": "sanitize"},
        {"optimize": True},
    ],
)
def test_together_with_the_other_options_the_result_verifies_and_keeps_no_colour_under_clear_pixels(extra):
    rng = np.random.default_rng(31)
    h, w = 96, 128
    base = (np.linspace(0, 255, w)[None, :, None] * np.ones((h, 1, 3)) + rng.normal(0, 8, (h, w, 3))).clip(1, 255)
    alpha = np.tile(np.linspace(0, 255, h)[:, None], (1, w)).round().astype(np.uint8)
    arr = np.dstack([base.astype(np.uint8), alpha])
    arr[alpha == 0, :3] = 0
    for _ in range(3):
        r = strip_image(png_of(arr, "RGBA"), Options(scrub_alpha=True, **extra))
        rgba = canonical_frames(r.data)[0]
        assert r.alpha_scrub == stego.ALPHA_APPLIED and r.verified_by
        assert (rgba[..., :3][rgba[..., 3] == 0] == 0).all()
        if "scrub_lsb" not in extra and "scrub_prnu" not in extra:
            assert (np.abs(rgba[..., 3].astype(int) - alpha.astype(int)) <= 5).all()


def test_the_camera_fingerprint_warp_cannot_reintroduce_near_solid_alpha():
    """The warp resamples alpha into new in-between values, which is why the alpha scrub runs after it."""
    rng = np.random.default_rng(32)
    arr = np.dstack([rng.integers(1, 256, (96, 96, 3), dtype=np.uint8), np.full((96, 96), 255, np.uint8)])
    arr[20:60, 20:60, 3] = 0
    arr[20:60, 20:60, :3] = 0
    r = strip_image(png_of(arr, "RGBA"), Options(scrub_prnu=True, scrub_alpha=True))
    plane = seen_alpha(r.data)
    assert r.prnu_applied
    assert not ((plane > 0) & (plane <= 5)).any() and not ((plane >= 250) & (plane < 255)).any()


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


@pytest.fixture()
def files(tmp_path):
    data, bits = covered("opaque", 1)
    (tmp_path / "logo.png").write_bytes(data)
    (tmp_path / "photo.png").write_bytes(png_of(np.random.default_rng(1).integers(0, 256, (40, 40, 3), dtype=np.uint8), "RGB"))
    return tmp_path, bits


def run_json(capsys, *argv):
    code = main(["--json", *argv])
    return code, json.loads(capsys.readouterr().out)["files"]


def test_the_command_line_flag_cleans_alpha_and_reports_it(files, capsys):
    root, bits = files
    code, got = run_json(capsys, "--scrub-alpha", "-o", str(root / "cleaned.png"), str(root / "logo.png"))
    assert code == 0
    a = got[0]["alpha"]
    assert a["state"] == "applied" and a["tolerance"] == 5 and a["pixels"] > 0 and "within 5 levels" in a["summary"]
    assert any("transparency channel" in act for act in got[0]["actions"])
    assert 0.42 < agreement("opaque", (root / "cleaned.png").read_bytes(), bits) < 0.58


def test_the_text_report_says_what_was_done_and_what_it_does_not_cover(files, capsys):
    root, _ = files
    assert main(["--scrub-alpha", "-o", str(root / "out"), str(root / "logo.png")]) == 0
    out = capsys.readouterr().out
    assert "transparency: cleaned:" in out and "not proof that nothing is hidden" in out


def test_the_tolerance_flag_is_used_and_even_values_are_reported_as_rounded(files, capsys):
    root, _ = files
    _code, got = run_json(capsys, "--scrub-alpha", "--alpha-tolerance", "10", "-o", str(root / "o"), str(root / "logo.png"))
    assert got[0]["alpha"]["tolerance"] == 11


def test_an_image_with_no_transparency_says_so_on_the_command_line(files, capsys):
    root, _ = files
    _code, got = run_json(capsys, "--scrub-alpha", "-o", str(root / "out"), str(root / "photo.png"))
    assert got[0]["alpha"]["state"] == "no_alpha" and got[0]["alpha"]["headline"] == "nothing to clean"
    assert main(["--scrub-alpha", "-o", str(root / "out2"), str(root / "photo.png")]) == 0
    assert "transparency: nothing to clean" in capsys.readouterr().out


def test_without_the_flag_the_report_has_no_alpha_block_and_prints_no_line(files, capsys):
    root, _ = files
    _code, got = run_json(capsys, "-o", str(root / "out"), str(root / "logo.png"))
    assert "alpha" not in got[0]
    assert main(["-v", "-o", str(root / "out2"), str(root / "logo.png")]) == 0
    assert "transparency" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["--alpha-tolerance", "5"],  # without --scrub-alpha it would silently do nothing
        ["--scrub-alpha", "--alpha-tolerance", "0"],
        ["--scrub-alpha", "--alpha-tolerance", "33"],
        ["--scrub-alpha", "--alpha-tolerance", "-2"],
        ["--scrub-alpha", "--alpha-tolerance", "abc"],
        ["--scrub-alpha", "--alpha-tolerance", "2.5"],
    ],
)
def test_a_tolerance_that_cannot_be_honoured_is_a_usage_error(files, argv, capsys):
    root, _ = files
    with pytest.raises(SystemExit) as exc:
        main([*argv, str(root / "logo.png")])
    assert exc.value.code == 2
    assert not (root / "logo.clean.png").exists(), "nothing may be written after a usage error"


def test_the_flags_are_in_the_help(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "--scrub-alpha" in out and "--alpha-tolerance" in out


def test_check_mode_is_unchanged(files, capsys):
    root, _ = files
    assert main(["--check", str(root / "logo.png")]) == 0
    assert "alpha" not in capsys.readouterr().out.lower().replace("alpha channel", "")


# --------------------------------------------------------------------------
# Web API and the page's rules
# --------------------------------------------------------------------------


def test_the_web_clean_result_carries_the_alpha_report(app):  # noqa: F811
    data, bits = covered("opaque", 1)
    off = app.clean(data, "logo.png")
    assert off["alpha"] is None
    on = app.clean(data, "logo.png", {"scrub_alpha": True})
    assert on["alpha"]["state"] == "applied" and on["alpha"]["tolerance"] == 5 and on["alpha"]["pixels"] > 0
    custom = app.clean(data, "logo.png", {"scrub_alpha": True, "alpha_tolerance": 8})
    assert custom["alpha"]["tolerance"] == 9


def test_the_web_reports_an_image_with_nothing_to_clean(app):  # noqa: F811
    got = app.clean(png_of(np.zeros((16, 16, 3), np.uint8), "RGB"), "plain.png", {"scrub_alpha": True})
    assert got["alpha"]["state"] == "no_alpha"


@pytest.mark.parametrize(
    "options",
    [{"scrub_alpha": "yes"}, {"scrub_alpha": 1}, {"alpha_tolerance": 0}, {"alpha_tolerance": 33}, {"alpha_tolerance": True},
     {"alpha_tolerance": 2.5}, {"alpha_tolerance": "5"}, {"alpha_tolerance": None}],
)
def test_the_web_refuses_malformed_alpha_options(app, options):  # noqa: F811
    data, _ = covered("opaque", 1)
    res, body = app.upload("/api/clean", data, "logo.png", options)
    assert res.status == 400, body


def _facts(app, data, name="x.png"):  # noqa: F811
    res, body = app.upload("/api/inspect", data, name)
    assert res.status == 200, body
    return json.loads(body)["facts"]


def test_the_page_is_told_which_images_can_carry_transparency(app):  # noqa: F811
    rgba, _ = covered("opaque", 1)
    assert _facts(app, rgba)["alpha"] is True
    assert _facts(app, png_of(np.zeros((8, 8, 3), np.uint8), "RGB"))["alpha"] is False
    assert _facts(app, png_of(np.zeros((8, 8), np.uint8), "L"))["alpha"] is False
    buf = io.BytesIO()
    Image.fromarray(np.zeros((8, 8, 3), np.uint8)).save(buf, "JPEG")
    assert _facts(app, buf.getvalue(), "x.jpg")["alpha"] is False
    pal = Image.fromarray(np.zeros((8, 8), np.uint8), "P")
    pal.putpalette([1, 2, 3] * 256)
    with_t, without = io.BytesIO(), io.BytesIO()
    pal.save(with_t, "PNG", transparency=0)
    pal.save(without, "PNG")
    assert _facts(app, with_t.getvalue())["alpha"] is True and _facts(app, without.getvalue())["alpha"] is False


SUPPORT_JS = Path(web.__file__).parent / "static" / "support.js"
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


def _rule(facts, **prefs):
    script = (
        "const S=require(process.argv[1]);const c=JSON.parse(require('fs').readFileSync(0,'utf8'));"
        "process.stdout.write(JSON.stringify(S.evaluate(c.facts,c.prefs,{limits:{}}).scrub_alpha));"
    )
    base = {"format": "", "icc": "convert", "auto_orient": True}
    proc = subprocess.run(
        ["node", "-e", script, str(SUPPORT_JS)], input=json.dumps({"facts": facts, "prefs": {**base, **prefs}}),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _facts_of(**over):
    return {"format": "PNG", "default_output": "PNG", "size": [64, 64], "megapixels": 0.0, "bytes": 1, "mode": "RGBA",
            "frames": 1, "orientation": 1, "profile": False, "alpha": True, **over}


@needs_node
def test_the_page_greys_the_switch_out_only_when_there_is_nothing_to_clean_or_it_cannot_run():
    assert _rule(None)["state"] == "ok"  # unknown image: nothing is ruled out
    assert _rule(_facts_of())["state"] == "ok"
    assert _rule(_facts_of(alpha=False, mode="RGB"))["state"] == "na"
    assert "no transparency channel" in _rule(_facts_of(alpha=False, mode="RGB"))["reason"]
    assert _rule({k: v for k, v in _facts_of().items() if k != "alpha"})["state"] == "ok"  # an older server: assume yes
    jpeg = _facts_of(format="JPEG", default_output="JPEG", mode="RGB", alpha=False)
    assert _rule(jpeg, jpeg_lossless=True)["state"] == "unsupported"  # the JPEG-data switches take precedence
    assert _rule(jpeg)["state"] == "na"


@needs_node
def test_the_page_sends_nothing_for_a_switch_that_cannot_apply():
    script = (
        "const S=require(process.argv[1]);const f=JSON.parse(process.argv[2]);"
        "const p={format:'',icc:'convert',auto_orient:true,scrub_alpha:true};"
        "process.stdout.write(JSON.stringify(S.effective(p,S.evaluate(f,p,{limits:{}})).scrub_alpha));"
    )
    run = lambda facts: subprocess.run(["node", "-e", script, str(SUPPORT_JS), json.dumps(facts)], capture_output=True, text=True).stdout  # noqa: E731
    assert run(_facts_of()) == "true" and run(_facts_of(alpha=False, mode="RGB")) == "false"


def test_the_page_has_the_switch_and_sends_it():
    static = Path(web.__file__).parent / "static"
    html, js = (static / "index.html").read_text(), (static / "app.js").read_text()
    assert 'name="scrub_alpha"' in html and "scrub_alpha: !!o.scrub_alpha" in js and "scrub_alpha: false" in js
    assert "alphaBlock(res.alpha)" in js


# --------------------------------------------------------------------------
# Content Credentials, self-test and docs
# --------------------------------------------------------------------------


def test_credentials_record_the_alpha_scrub_only_when_it_changed_the_picture(signer):
    def evidence(source, options):
        return credentials.gather_evidence(source, strip_image(source, options), options)

    changed, _ = covered("opaque", 1)
    ev = evidence(changed, Options(scrub_alpha=True))
    assert "alpha-scrubbed" in ev.operations and ev.integrity == "altered"
    opaque = png_of(np.dstack([np.full((24, 24, 3), 9, np.uint8), np.full((24, 24), 255, np.uint8)]), "RGBA")
    ev = evidence(opaque, Options(scrub_alpha=True))
    assert "alpha-scrubbed" not in ev.operations and ev.integrity == "identical"
    ev = evidence(png_of(np.full((24, 24, 3), 9, np.uint8), "RGB"), Options(scrub_alpha=True))
    assert "alpha-scrubbed" not in ev.operations and ev.integrity == "identical"
    ev = evidence(changed, Options())
    assert "alpha-scrubbed" not in ev.operations


def test_the_credential_vocabulary_knows_the_new_operation(signer):
    assert "alpha-scrubbed" in credentials.OPERATIONS
    data, _ = covered("opaque", 1)
    options = Options(scrub_alpha=True)
    result = strip_image(data, options)
    evidence = credentials.gather_evidence(data, result, options)
    signed = credentials.sign(result.data, evidence, signer.identity, credentials.Config(detail="full"))
    assert "alpha-scrubbed" in signed.credential.sanitization["operations"]
    assert "deliberately altered" in signed.credential.sanitization["statement"]


def test_the_self_test_matrix_includes_the_alpha_scrub():
    names = [name for name, _ in selftest._variants("x.png", [])]
    assert "scrub-alpha" in names and "scrub-alpha+lsb" in names
    options = dict(selftest._variants("x.png", []))
    assert options["scrub-alpha"].scrub_alpha and options["scrub-alpha+lsb"].scrub_lsb


def test_the_documents_describe_the_option():
    root = Path(__file__).resolve().parent.parent
    for doc in ("README.md", "docs/REFERENCE.md", "CHANGELOG.md"):
        text = (root / doc).read_text()
        assert "--scrub-alpha" in text, doc
    assert "--alpha-tolerance" in (root / "docs" / "REFERENCE.md").read_text()
