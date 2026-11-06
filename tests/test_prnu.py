"""Camera sensor fingerprint (PRNU) disruption.

The efficacy tests use an *oracle* attacker: a detector that knows the camera's
exact fingerprint (the strongest attacker there is; real ones must estimate it
from other photos).  It correlates the image's noise residual with
``fingerprint * image`` over every circular shift and reports the
peak-to-correlation energy (PCE); 60 is the usual threshold for "same camera".
"""

import io
import json

import numpy as np
import pytest
from conftest import decode
from PIL import Image, ImageDraw

from imgstrip import Options, StripError, fixtures, prnu, strip_image
from imgstrip.cli import main

# --------------------------------------------------------------------------
# A simulated camera and an oracle detector
# --------------------------------------------------------------------------

SIGMA_K = 0.03  # a strong fingerprint (real sensors are weaker), so the baseline is unambiguous
W, H = 512, 384


def _blur(a: np.ndarray, sigma: float) -> np.ndarray:
    ky = np.fft.fftfreq(a.shape[0])[:, None]
    kx = np.fft.fftfreq(a.shape[1])[None, :]
    return np.fft.ifft2(np.fft.fft2(a) * np.exp(-2 * (np.pi * sigma) ** 2 * (kx**2 + ky**2))).real


def fingerprint(seed: int) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0, SIGMA_K, (H, W))


def shoot(k: np.ndarray, seed: int = 0) -> bytes:
    """A photo from the camera with fingerprint ``k``: scene * (1 + k) + shot noise, as a PNG."""
    rng = np.random.default_rng(1000 + seed)
    scene = fixtures.photo(W, H, seed).astype(np.float64).mean(axis=2)
    img = scene * (1.0 + k) + rng.normal(0.0, 2.0, scene.shape)
    buf = io.BytesIO()
    Image.fromarray(np.clip(np.rint(img), 0, 255).astype(np.uint8)).save(buf, "PNG")
    return buf.getvalue()


def pce(png: bytes, k: np.ndarray) -> float:
    img = np.asarray(decode(png).convert("L"), dtype=np.float64)
    resid = img - _blur(img, 1.2)
    template = k * img
    resid, template = resid - resid.mean(), template - template.mean()
    cc = np.fft.ifft2(np.fft.fft2(resid) * np.conj(np.fft.fft2(template))).real
    cc /= np.sqrt((resid**2).sum() * (template**2).sum())
    py, px = np.unravel_index(np.argmax(cc), cc.shape)
    mask = np.ones(cc.shape, bool)
    mask[np.ix_(np.arange(py - 5, py + 6) % cc.shape[0], np.arange(px - 5, px + 6) % cc.shape[1])] = False
    return float(cc[py, px] ** 2 / (cc[mask] ** 2).mean())


@pytest.fixture
def fixed_warp(monkeypatch):
    """Make the warp reproducible (it is random per run in production)."""

    def seed(n: int = 0):
        monkeypatch.setattr(prnu, "new_rng", lambda: np.random.default_rng(n))

    return seed


# --------------------------------------------------------------------------
# Does it work?
# --------------------------------------------------------------------------


def test_the_oracle_detector_is_valid():
    k = fingerprint(1)
    assert pce(shoot(k), k) > 1000  # the right camera is found ...
    assert pce(shoot(k), fingerprint(2)) < 60  # ... and another camera's fingerprint is not


def test_cleaning_alone_leaves_the_fingerprint_intact():
    # This is why the feature exists: metadata removal does not touch the sensor pattern.
    k = fingerprint(1)
    assert pce(strip_image(shoot(k)).data, k) > 1000


def test_imperceptible_noise_does_not_help():
    # The obvious idea, adding a little noise, does not work: the detector averages over every
    # pixel.  Guard against "simplifying" the scrub to this.
    k = fingerprint(1)
    img = np.asarray(decode(shoot(k)).convert("L"), dtype=np.float64)
    noisy = np.clip(np.rint(img + np.random.default_rng(5).normal(0, 2.0, img.shape)), 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(noisy).save(buf, "PNG")
    assert pce(buf.getvalue(), k) > 1000


@pytest.mark.parametrize("seed", range(4))
def test_scrub_defeats_the_oracle_detector(fixed_warp, seed):
    k = fingerprint(1)
    fixed_warp(seed)
    result = strip_image(shoot(k, seed), Options(scrub_prnu=True))
    assert result.prnu_applied
    assert pce(result.data, k) < 60


def test_a_rigid_shift_is_not_enough():
    # The detector searches over shifts, so moving the whole picture proves nothing.  (Documents why
    # the warp has to be non-rigid.)
    k = fingerprint(1)
    img = np.asarray(decode(shoot(k)))
    shifted = np.roll(img, (7, -5), axis=(0, 1))
    buf = io.BytesIO()
    Image.fromarray(shifted).save(buf, "PNG")
    assert pce(buf.getvalue(), k) > 1000


# --------------------------------------------------------------------------
# The warp itself
# --------------------------------------------------------------------------


def test_parameters_grow_with_image_size():
    small = prnu.parameters(800, 600)
    big = prnu.parameters(4000, 3000)
    assert small[0] == prnu.BASE_RMS and small[1] == prnu.BASE_CORR  # floor at 1 MP
    assert big[0] > small[0] and big[1] > small[1]
    for w, h in [(100, 100), (800, 600), (4000, 3000), (12000, 9000)]:
        _, _, cell = prnu.parameters(w, h)
        assert cell >= 8 and cell % 2 == 0


@pytest.mark.parametrize("size", [(1237, 911), (999, 1001), (4000, 3000), (97, 53)])
def test_a_flat_image_stays_exactly_flat(size):
    # No sample may land outside the image (that would leave a black or transparent rim), and
    # resampling a constant must return the constant.
    w, h = size
    for mode, value in [("L", 128), ("RGB", (10, 200, 77))]:
        im = Image.new(mode, size, value)
        for seed in range(3):
            warp = prnu.plan(w, h, im.convert("L"), np.random.default_rng(seed))
            out = prnu.warp_image(im, warp)
            assert out.size == size and out.mode == mode
            assert out.tobytes() == im.tobytes()


def test_displacement_statistics():
    rms, _, _ = prnu.parameters(2000, 1500)
    warp = prnu.plan(2000, 1500, None, np.random.default_rng(0))
    assert 0.6 * np.sqrt(2) * rms < warp.rms < 1.05 * np.sqrt(2) * rms  # a little less: the border is pinned
    assert warp.max_shift <= np.sqrt(6) * rms * 1.001  # bounded: uniform marginals


def test_the_warp_is_continuous():
    # A smooth warp moves neighbouring pixels by nearly the same amount: the picture is bent, not torn.
    w, h = 512, 384
    ramp = np.tile(np.arange(w, dtype=np.float32), (h, 1))  # intensity == x, so the output shows the source x
    warp = prnu.plan(w, h, None, np.random.default_rng(3))
    out = np.asarray(
        Image.fromarray(ramp, "F").transform((w, h), Image.Transform.MESH, warp.mesh, Image.Resampling.BICUBIC),
        dtype=np.float64,
    )
    shift = out - ramp  # per-pixel horizontal displacement
    assert np.abs(shift).max() <= np.sqrt(6) * prnu.parameters(w, h)[0] * 1.01
    assert np.abs(np.diff(shift, axis=1)).max() < 1.0  # no jumps between neighbouring pixels


def _line_image(kind: str, size: int = 320) -> Image.Image:
    """One isolated straight line on a flat background (a crossing of two lines is a known limit: see the README)."""
    im = Image.new("L", (size, size), 60)
    ends = {"vertical": [(160, 0), (160, size)], "diagonal": [(0, 40), (size, 280)]}[kind]
    ImageDraw.Draw(im).line(ends, fill=220, width=3)
    arr = np.asarray(im).astype(np.int16) + np.random.default_rng(0).integers(0, 4, (size, size))
    return Image.fromarray(arr.clip(0, 255).astype(np.uint8))


def _line_offsets(img: np.ndarray, kind: str) -> np.ndarray:
    """Perpendicular distance of the line's centre from where it started, at many points along it."""
    offsets = []
    for x in range(30, img.shape[1] - 30) if kind == "diagonal" else range(25, img.shape[0] - 25):
        if kind == "vertical":
            band, lo, ideal, scale = img[x, 130:191], 130, 160.0, 1.0  # here x is the row
        else:
            ideal = 40 + 0.75 * x
            lo = int(ideal) - 25
            band, scale = img[lo : lo + 51, x], 0.8  # cosine of the line's angle
        weights = np.clip(band.astype(np.float64) - 60, 0, None)
        offsets.append(((weights * (lo + np.arange(len(weights)))).sum() / weights.sum() - ideal) * scale)
    return np.array(offsets)


@pytest.mark.parametrize("kind, bound", [("vertical", 0.8), ("diagonal", 1.2)])
def test_edges_slide_instead_of_bending(kind, bound):
    im = _line_image(kind)
    mean = {}
    for project in (False, True):
        runs = []
        for seed in range(6):
            warp = prnu.plan(im.width, im.height, im, np.random.default_rng(seed), project=project)
            runs.append(np.abs(_line_offsets(np.asarray(prnu.warp_image(im, warp)), kind)))
        mean[project] = np.mean([r.mean() for r in runs])
        if project:
            assert max(r.max() for r in runs) < 2.5 * bound
    assert mean[False] > 1.5  # unprotected, the line wanders by whole pixels
    assert mean[True] < bound * 0.5 and mean[True] < mean[False] / 4  # an isolated edge stays where it was


def test_edge_statistic_is_lower_when_projecting():
    luma = _line_image("vertical")
    free = prnu.plan(320, 320, luma, np.random.default_rng(1), project=False)
    proj = prnu.plan(320, 320, luma, np.random.default_rng(1), project=True)
    assert free.edge_shift > 0 and proj.edge_shift < 0.4 * free.edge_shift


# --------------------------------------------------------------------------
# Through the engine
# --------------------------------------------------------------------------


def _png(arr: np.ndarray, mode: str | None = None) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr, mode).save(buf, "PNG")
    return buf.getvalue()


def test_off_by_default():
    data = _png(fixtures.photo(200, 150))
    result = strip_image(data)
    assert not result.prnu_applied and result.pixels_exact
    assert not any("fingerprint" in a for a in result.actions)


def test_picture_is_preserved_and_the_output_is_verified(fixed_warp):
    fixed_warp(0)
    arr = fixtures.photo(300, 200)
    result = strip_image(_png(arr), Options(scrub_prnu=True))  # strip_image verifies the written file itself
    assert result.prnu_applied and not result.pixels_exact and result.verified_exact
    assert any("camera sensor fingerprint" in a for a in result.actions)
    out = np.asarray(decode(result.data))
    assert out.shape == arr.shape
    # Blurred, the two pictures are the same picture.
    a, b = (np.asarray(Image.fromarray(x).resize((60, 40), Image.Resampling.BOX), dtype=float) for x in (arr, out))
    assert np.abs(a - b).mean() < 6


def test_warp_is_random_unless_seeded(fixed_warp):
    data = _png(fixtures.photo(200, 150))
    a = strip_image(data, Options(scrub_prnu=True)).data
    b = strip_image(data, Options(scrub_prnu=True)).data
    assert a != b
    fixed_warp(7)
    assert strip_image(data, Options(scrub_prnu=True)).data == strip_image(data, Options(scrub_prnu=True)).data


@pytest.mark.parametrize("mode", ["L", "RGB", "RGBA"])
def test_8bit_modes(mode, fixed_warp):
    fixed_warp(0)
    base = fixtures.photo(160, 120)
    arr = {
        "L": base.mean(axis=2).astype(np.uint8),
        "RGB": base,
        "RGBA": np.dstack([base, np.full((120, 160), 255, np.uint8)]),
    }[mode]
    result = strip_image(_png(arr), Options(scrub_prnu=True))
    out = decode(result.data)
    assert result.prnu_applied and out.size == (160, 120)
    # (the engine already writes a fully opaque RGBA image as RGB)
    assert out.mode == ("L" if mode == "L" else "RGB")


@pytest.mark.parametrize("mode", ["LA", "RGBA"])
def test_opaque_alpha_stays_exactly_opaque(mode):
    base = Image.fromarray(fixtures.photo(160, 120)).convert(mode)
    base.putalpha(255)
    warp = prnu.plan(160, 120, base.convert("L"), np.random.default_rng(0))
    out = prnu.warp_image(base, warp)
    assert out.mode == mode and out.getchannel("A").getextrema() == (255, 255)  # edges included
    assert out.tobytes() != base.tobytes()


def test_partial_transparency_has_no_dark_fringe(fixed_warp):
    fixed_warp(0)
    arr = np.zeros((120, 160, 4), np.uint8)
    arr[..., :3] = (240, 200, 60)
    arr[..., 3] = np.linspace(0, 255, 160, dtype=np.uint8)[None, :]
    arr[..., :3][arr[..., 3] == 0] = 0
    out = np.asarray(decode(strip_image(_png(arr), Options(scrub_prnu=True)).data).convert("RGBA")).astype(int)
    visible = out[..., 3] > 40
    # Colour is not mixed with the (zeroed) colour of the transparent region.
    assert np.abs(out[..., :3][visible] - np.array([240, 200, 60])).max() <= 12


def test_cmyk_jpeg(fx, fixed_warp):
    fixed_warp(0)
    result = strip_image(fx["cmyk.jpg"], Options(scrub_prnu=True))
    assert result.prnu_applied


def test_16bit_precision_is_kept():
    rng = np.random.default_rng(0)
    y, x = np.mgrid[0:96, 0:128]
    arr = (np.stack([x * 400 + y * 100, y * 600, x * 200 + 9000], axis=-1) + rng.integers(0, 256, (96, 128, 3))).astype(
        np.uint16
    )
    warp = prnu.plan(128, 96, Image.fromarray((arr[..., 0] >> 8).astype(np.uint8)), np.random.default_rng(0))
    out = prnu.warp_array16(arr, warp)
    assert out.dtype == np.uint16 and out.shape == arr.shape
    assert (out % 257 != 0).mean() > 0.9  # genuinely 16-bit values, not 8-bit ones scaled up
    flat = np.full((96, 128, 3), 40001, np.uint16)
    assert np.abs(prnu.warp_array16(flat, warp).astype(int) - 40001).max() <= 1


def test_16bit_png_end_to_end(fx):
    result = strip_image(fx["png_16bit_adobe.png"], Options(scrub_prnu=True))
    assert result.prnu_applied
    assert "decoded at full 16-bit precision" in result.actions


@pytest.mark.parametrize("name", ["png_palette.png", "gif_animated.gif"])
def test_palette_images_are_skipped_with_a_warning(fx, name):
    result = strip_image(fx[name], Options(scrub_prnu=True))
    assert not result.prnu_applied
    assert any("sensor-fingerprint scrub skipped" in w for w in result.warnings)


def test_tiny_images_are_skipped_with_a_warning(fx):
    result = strip_image(fx["webp_animated.webp"], Options(scrub_prnu=True))  # 40x30
    assert not result.prnu_applied
    assert any("too small" in w for w in result.warnings)


def test_animation_frames_share_one_warp(fixed_warp):
    fixed_warp(0)
    base = fixtures.photo(120, 96)
    frames = []
    for i in range(3):
        f = base.copy()
        f[4:8, 4 + 20 * i : 8 + 20 * i] = 255  # a marker, so frames differ
        frames.append(Image.fromarray(f))
    buf = io.BytesIO()
    frames[0].save(buf, "PNG", save_all=True, append_images=frames[1:], duration=[100, 100, 100], loop=0)
    result = strip_image(buf.getvalue(), Options(scrub_prnu=True))
    assert result.prnu_applied and result.frames == 3
    outs = []
    for i in range(3):
        im = decode(result.data, i).convert("RGB")
        outs.append(np.asarray(im))
    # Away from the markers every frame is identical: the same warp was used, so a static scene does not shimmer.
    assert np.array_equal(outs[0][30:, :], outs[1][30:, :]) and np.array_equal(outs[1][30:, :], outs[2][30:, :])
    assert not np.array_equal(outs[0][30:, :], base[30:, :])


def test_bit_exact_jpeg_mode_fails_closed(fx):
    with pytest.raises(StripError, match="cannot be combined"):
        strip_image(fx["jpeg_everything.jpg"], Options(scrub_prnu=True, jpeg_lossless=True))
    # ... but converting to another format with the lossless switch on is fine: pixels are rebuilt anyway.
    result = strip_image(fx["jpeg_everything.jpg"], Options(scrub_prnu=True, jpeg_lossless=True, format="png"))
    assert result.prnu_applied


def test_combines_with_other_options(fx):
    result = strip_image(
        fx["png_everything.png"], Options(scrub_prnu=True, scrub_lsb=True, icc="sanitize", format="webp", quality=90)
    )
    assert result.prnu_applied and result.output_format == "WEBP"


# --------------------------------------------------------------------------
# Command line and web app
# --------------------------------------------------------------------------


def test_cli_flag(tmp_path, capsys):
    src = tmp_path / "photo.png"
    src.write_bytes(_png(fixtures.photo(200, 150)))
    assert main([str(src), "--scrub-prnu", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    text = json.dumps(report)
    assert "camera sensor fingerprint" in text
    out = tmp_path / "photo.clean.png"
    assert out.exists()
    assert not np.array_equal(np.asarray(Image.open(out)), fixtures.photo(200, 150))


def test_cli_refuses_the_lossless_jpeg_combination(tmp_path, fx, capsys):
    src = tmp_path / "a.jpg"
    src.write_bytes(fx["jpeg_everything.jpg"])
    assert main([str(src), "--scrub-prnu", "--jpeg-lossless"]) == 1
    assert not (tmp_path / "a.clean.jpg").exists()
    assert "cannot be combined" in capsys.readouterr().err
