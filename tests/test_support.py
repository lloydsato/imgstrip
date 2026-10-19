"""The rules that grey options out (static/support.js) are checked against what the engine really does.

The page may only grey an option out when that is true: an option marked "not applicable" must change nothing in the
result, and one marked "unsupported" must be refused or reported as not applied. Anything looser would take a working
option away from a user. The rules run in Node (skipped when it is missing), on real images through the real inspector.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageCms

from imgstrip import credentials
from imgstrip.core import Options, StripError, strip_image
from imgstrip.web import server as web

SUPPORT_JS = Path(web.__file__).parent / "static" / "support.js"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

RUNNER = r"""
const S = require(process.argv[1]);
const { cases, env } = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = cases.map((c) => ({
  states: S.evaluate(c.facts, c.prefs, env),
  formats: S.formatStates(c.facts, env),
  effective: S.effective(c.prefs, S.evaluate(c.facts, c.prefs, env)),
  blocker: S.blocker(c.facts, c.bytes, env),
}));
process.stdout.write(JSON.stringify(out));
"""

DEFAULT_PREFS = {
    "format": "", "quality": "", "icc": "convert", "deep": False, "jpeg_lossless": False, "scrub_lsb": False,
    "scrub_dct": False, "jpeg_canonical": False, "scrub_prnu": False, "auto_orient": True, "optimize": False,
    "random_name": False, "c2pa": False,
}
# The value that turns each option "on" for the comparison with it off.
ON = {
    "quality": "60", "icc": "drop", "deep": True, "jpeg_lossless": True, "scrub_lsb": True, "scrub_dct": True,
    "jpeg_canonical": True, "scrub_prnu": True, "auto_orient": True, "optimize": True, "random_name": True, "c2pa": True,
}
OFF = {
    "quality": "", "icc": "convert", "deep": False, "jpeg_lossless": False, "scrub_lsb": False, "scrub_dct": False,
    "jpeg_canonical": False, "scrub_prnu": False, "auto_orient": False, "optimize": False, "random_name": False,
    "c2pa": False,
}
RANDOMISING = ("scrub_lsb", "scrub_dct", "scrub_prnu")
ENGINE_OPTIONS = ("quality", "icc", "jpeg_lossless", "scrub_lsb", "scrub_dct", "jpeg_canonical", "scrub_prnu",
                  "auto_orient", "optimize")


# --------------------------------------------------------------------------- images


def _save(im, fmt, **kw):
    buf = io.BytesIO()
    im.save(buf, fmt, **kw)
    return buf.getvalue()


def _photo(w=96, h=80, mode="RGB", seed=1):
    rng = np.random.default_rng(seed)
    x = np.linspace(0, 255, w, dtype="float32")[None, :]
    y = np.linspace(0, 255, h, dtype="float32")[:, None]
    base = np.stack([np.broadcast_to(x, (h, w)), np.broadcast_to(y, (h, w)), np.full((h, w), 80.0)], 2)
    base = (base + rng.normal(0, 6, base.shape)).clip(0, 255).astype("uint8")
    im = Image.fromarray(base)
    return {
        "RGB": lambda: im,
        "L": lambda: im.convert("L"),
        "LA": lambda: im.convert("LA"),
        "RGBA": lambda: Image.merge("RGBA", (*im.split(), Image.new("L", im.size, 255))),
        "P": lambda: im.quantize(24),
        "1": lambda: im.convert("L").convert("1"),
        "I;16": lambda: Image.fromarray(base[..., 0].astype("uint16") * 257),
        "I": lambda: Image.fromarray(base[..., 0].astype("int32") * 1000, "I"),
        "F": lambda: Image.fromarray(base[..., 0].astype("float32"), "F"),
    }[mode]()


def _exif(orientation):
    exif = Image.Exif()
    exif[0x0112] = orientation
    return exif.tobytes()


SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _build():
    from imgstrip.core import heif_available

    images = {
        "jpeg": _save(_photo(), "JPEG", quality=88),
        "jpeg-rotated": _save(_photo(), "JPEG", quality=88, exif=_exif(6)),
        "jpeg-profile": _save(_photo(), "JPEG", quality=88, icc_profile=SRGB),
        "jpeg-gray": _save(_photo(mode="L"), "JPEG", quality=88),
        "jpeg-tiny": _save(_photo(40, 40), "JPEG", quality=88),
        "jpeg-edge-48": _save(_photo(48, 48), "JPEG", quality=88),
        "jpeg-edge-47": _save(_photo(47, 60), "JPEG", quality=88),
        "jpeg-1px": _save(Image.new("RGB", (1, 1), (9, 9, 9)), "JPEG"),
        "png": _save(_photo(), "PNG"),
        "png-rotated": _save(_photo(), "PNG", exif=_exif(3)),
        "png-profile": _save(_photo(), "PNG", icc_profile=SRGB),
        "png-gray": _save(_photo(mode="L"), "PNG"),
        "png-la": _save(_photo(mode="LA"), "PNG"),
        "png-rgba": _save(_photo(mode="RGBA"), "PNG"),
        "png-palette": _save(_photo(mode="P"), "PNG"),
        "png-1bit": _save(_photo(mode="1"), "PNG"),
        "png-16bit": _save(_photo(mode="I;16"), "PNG"),
        "png-tiny": _save(_photo(30, 30), "PNG"),
        "png-1px": _save(Image.new("RGB", (1, 1), (9, 9, 9)), "PNG"),
        "png-thin": _save(Image.fromarray(np.tile(np.arange(20000, dtype="uint8"), (4, 1))), "PNG"),
        "gif": _save(_photo(mode="P"), "GIF"),
        "gif-animated": _save(
            _photo(60, 60), "GIF", save_all=True, append_images=[_photo(60, 60, seed=2), _photo(60, 60, seed=3)]
        ),
        "webp": _save(_photo(), "WEBP", quality=80),
        "webp-profile": _save(_photo(), "WEBP", quality=80, icc_profile=SRGB),
        "webp-animated": _save(
            _photo(60, 60), "WEBP", save_all=True, append_images=[_photo(60, 60, seed=2), _photo(60, 60, seed=3)]
        ),
        "tiff": _save(_photo(), "TIFF"),
        "tiff-rotated": _save(_photo(), "TIFF", exif=_exif(8)),
        "tiff-32bit": _save(_photo(mode="I"), "TIFF"),
        "tiff-float": _save(_photo(mode="F"), "TIFF"),
        "tiff-1bit": _save(_photo(mode="1"), "TIFF"),
        "bmp": _save(_photo(), "BMP"),
        "bmp-1bit": _save(_photo(mode="1"), "BMP"),
        "avif": _save(_photo(), "AVIF", quality=70),
    }
    if heif_available():
        import pillow_heif

        heif = _photo()
        buf = io.BytesIO()
        pillow_heif.from_pillow(heif).save(buf, format="HEIF", quality=70)
        images["heic"] = buf.getvalue()
    return images


@pytest.fixture(scope="module")
def images():
    return _build()


@pytest.fixture(scope="module")
def env():
    return {
        "limits": web.limits_info(),
        "formats": web.available_formats(),
        "signable": list(credentials.SIGNABLE),
    }


def _prefs(**kw):
    return {**DEFAULT_PREFS, **kw}


def _judge(cases, env):
    """The rules' answers (run in Node) for [{facts, prefs, bytes}]."""
    proc = subprocess.run(
        ["node", "-e", RUNNER, str(SUPPORT_JS)],
        input=json.dumps({"cases": cases, "env": env}),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _options(prefs, **over):
    p = {**prefs, **over}
    return Options(
        format=p["format"] or None,
        quality=int(p["quality"]) if p["quality"] else None,
        jpeg_lossless=p["jpeg_lossless"],
        icc=p["icc"],
        auto_orient=p["auto_orient"],
        scrub_lsb=p["scrub_lsb"],
        scrub_dct="full" if p["scrub_dct"] else "canonical" if p["jpeg_canonical"] else "off",
        scrub_prnu=p["scrub_prnu"],
        optimize=p["optimize"],
        max_pixels=web.WEB_MAX_PIXELS,
    )


def _run(data, options):
    try:
        return strip_image(data, options), None
    except StripError as exc:
        return None, str(exc)


# --------------------------------------------------------------------------- contexts

FORMATS = ["", "JPEG", "PNG", "WEBP", "GIF", "TIFF", "BMP"]
JPEG_MODES = [
    {},
    {"jpeg_lossless": True},
    {"scrub_dct": True},
    {"jpeg_canonical": True},
    {"jpeg_lossless": True, "scrub_prnu": True},
    {"scrub_dct": True, "scrub_lsb": True},
]


def _contexts(name):
    """The option sets worth trying on an image: every output format, plus the JPEG bit-exact family on JPEGs."""
    out = [_prefs(format=f) for f in FORMATS]
    if name.startswith("jpeg"):
        out += [_prefs(**m) for m in JPEG_MODES]
    return out


# --------------------------------------------------------------------------- soundness


def test_every_greyed_out_option_really_changes_nothing_or_is_refused(images, env):
    """No option is greyed out unless the engine ignores it, refuses it or reports it as not applied."""
    cases, meta = [], []
    for name, data in images.items():
        facts = web.image_facts(data)
        assert facts is not None, name
        for prefs in _contexts(name):
            cases.append({"facts": facts, "prefs": prefs, "bytes": len(data)})
            meta.append((name, data, prefs))
    verdicts = _judge(cases, env)

    checked = greyed = 0
    for (name, data, prefs), verdict in zip(meta, verdicts, strict=True):
        for option in ENGINE_OPTIONS:
            state = verdict["states"][option]["state"]
            if state == "ok":
                continue
            greyed += 1
            ctx = f"{name} {option} [{state}: {verdict['states'][option]['reason']}] under {prefs}"
            asked = _prefs(**{k: v for k, v in prefs.items()})
            asked[option] = ON[option]
            neutral = {**asked, option: OFF[option]}
            if option == "auto_orient":
                neutral["auto_orient"] = True  # on is the default; "greyed" means there is no tag to act on
                asked["auto_orient"] = False
            with_opt, err_with = _run(data, _options(asked))
            without, err_without = _run(data, _options(neutral))
            checked += 1
            if err_with is not None:
                continue  # the engine refuses it: the page was right to take it away
            if err_without is not None:
                continue  # the picture fails with or without the option, for another reason
            if option == "scrub_lsb":
                assert with_opt.lsb_scrub not in ("applied", "partial"), ctx
            elif option == "scrub_prnu":
                assert not with_opt.prnu_applied, ctx
            elif not any(neutral[r] for r in RANDOMISING):  # the random scrubs make two runs differ by design
                assert with_opt.data == without.data, ctx
    assert greyed > 150 and checked == greyed  # the matrix is not vacuous


def test_options_that_apply_really_do_something(images, env):
    """The other direction, on plain cases: an option offered as available has an effect."""
    def verdict(name, **prefs):
        facts = web.image_facts(images[name])
        return _judge([{"facts": facts, "prefs": _prefs(**prefs), "bytes": 1}], env)[0]["states"]

    for name, option in [("jpeg", "quality"), ("webp", "quality"), ("avif", "quality")]:
        assert verdict(name)[option]["state"] == "ok", name
        low, _ = _run(images[name], _options(_prefs(), quality="40"))
        high, _ = _run(images[name], _options(_prefs(), quality="95"))
        assert low.data != high.data, name

    assert verdict("jpeg-rotated")["auto_orient"]["state"] == "ok"
    res, _ = _run(images["jpeg-rotated"], _options(_prefs()))
    assert any("applied EXIF orientation" in a for a in res.actions)

    assert verdict("jpeg-profile")["icc"]["state"] == "ok"
    assert verdict("png-profile")["icc"]["state"] == "ok"
    assert verdict("webp-profile")["icc"]["state"] == "ok"

    for name in ("jpeg", "png", "webp", "tiff", "bmp", "png-palette", "png-16bit"):
        assert verdict(name)["scrub_lsb"]["state"] == "ok", name
        res, _ = _run(images[name], _options(_prefs(), scrub_lsb=True))
        assert res.lsb_scrub in ("applied", "partial"), name

    for name in ("jpeg", "png", "webp", "tiff", "png-16bit", "avif"):
        assert verdict(name)["scrub_prnu"]["state"] == "ok", name
        res, _ = _run(images[name], _options(_prefs(), scrub_prnu=True))
        assert res.prnu_applied, name

    for option in ("jpeg_lossless", "scrub_dct", "jpeg_canonical"):
        assert verdict("jpeg")[option]["state"] == "ok", option
    assert verdict("png")["optimize"]["state"] == "ok"
    assert verdict("jpeg", format="PNG")["optimize"]["state"] == "ok"
    assert verdict("png")["random_name"]["state"] == "ok"


# --------------------------------------------------------------------------- the rules themselves


def _states(images, env, name, **prefs):
    facts = web.image_facts(images[name])
    return _judge([{"facts": facts, "prefs": _prefs(**prefs), "bytes": len(images[name])}], env)[0]


def test_a_png_loses_the_jpeg_only_switches_and_the_quality_menu(images, env):
    s = _states(images, env, "png")["states"]
    for option in ("jpeg_lossless", "scrub_dct", "jpeg_canonical", "quality"):
        assert s[option]["state"] == "na", option
    assert "JPEG files only" in s["jpeg_lossless"]["reason"]
    assert "lossless" in s["quality"]["reason"]
    assert s["optimize"]["state"] == "ok"


def test_a_jpeg_loses_smaller_pngs_until_it_is_saved_as_png(images, env):
    assert _states(images, env, "jpeg")["states"]["optimize"]["state"] == "na"
    assert _states(images, env, "jpeg", format="PNG")["states"]["optimize"]["state"] == "ok"
    s = _states(images, env, "jpeg", format="PNG")["states"]
    assert s["scrub_dct"]["state"] == "na" and "rebuilt from its pixels" in s["scrub_dct"]["reason"]
    assert s["quality"]["state"] == "na"


def test_saving_a_png_as_jpeg_brings_back_quality_and_drops_png_only_options(images, env):
    s = _states(images, env, "png", format="JPEG")["states"]
    assert s["quality"]["state"] == "ok" and s["optimize"]["state"] == "na"
    assert s["jpeg_lossless"]["state"] == "na"  # a PNG is not a JPEG whatever it is saved as


def test_bit_exact_jpeg_greys_out_the_pixel_scrubs_with_a_warning_and_the_quality_menu(images, env):
    for mode in ({"jpeg_lossless": True}, {"scrub_dct": True}, {"jpeg_canonical": True}):
        s = _states(images, env, "jpeg", **mode)["states"]
        assert s["scrub_lsb"]["state"] == "unsupported", mode
        assert s["scrub_prnu"]["state"] == "unsupported", mode
        assert s["quality"]["state"] == "na", mode
    # and they come back the moment the JPEG is re-encoded
    s = _states(images, env, "jpeg", jpeg_lossless=True, format="PNG")["states"]
    assert s["scrub_lsb"]["state"] == "ok" and s["scrub_prnu"]["state"] == "ok"


def test_the_orientation_switch_follows_the_tag(images, env):
    assert _states(images, env, "jpeg")["states"]["auto_orient"]["state"] == "na"
    assert _states(images, env, "jpeg-rotated")["states"]["auto_orient"]["state"] == "ok"
    assert _states(images, env, "png-rotated")["states"]["auto_orient"]["state"] == "ok"
    assert _states(images, env, "tiff-rotated")["states"]["auto_orient"]["state"] == "ok"
    assert _states(images, env, "gif")["states"]["auto_orient"]["state"] == "na"
    assert _states(images, env, "bmp")["states"]["auto_orient"]["state"] == "na"


def test_the_colour_profile_menu_follows_the_profile(images, env):
    assert _states(images, env, "jpeg")["states"]["icc"]["state"] == "na"
    assert _states(images, env, "jpeg-profile")["states"]["icc"]["state"] == "ok"
    assert _states(images, env, "png")["states"]["icc"]["state"] == "na"
    assert _states(images, env, "webp-profile")["states"]["icc"]["state"] == "ok"


def test_signing_is_unsupported_for_tiff_and_bmp_output(images, env):
    for name in ("tiff", "bmp"):
        s = _states(images, env, name)["states"]["c2pa"]
        assert s["state"] == "unsupported" and "cannot carry Content Credentials" in s["reason"], name
    assert _states(images, env, "png", format="TIFF")["states"]["c2pa"]["state"] == "unsupported"
    assert _states(images, env, "tiff", format="PNG")["states"]["c2pa"]["state"] == "ok"
    for name in ("jpeg", "png", "webp", "gif", "avif"):
        assert _states(images, env, name)["states"]["c2pa"]["state"] == "ok", name


# --------------------------------------------------------------------------- edge cases


def test_tiny_images_cannot_take_the_camera_fingerprint_scrub(images, env):
    """The fingerprint scrub needs 48 pixels on a side; the engine skips anything smaller."""
    for name in ("jpeg-tiny", "jpeg-edge-47", "png-tiny", "jpeg-1px", "png-1px"):
        s = _states(images, env, name)["states"]["scrub_prnu"]
        assert s["state"] == "unsupported" and "too small" in s["reason"], name
        res, _ = _run(images[name], _options(_prefs(), scrub_prnu=True))
        assert res is None or not res.prnu_applied, name
    assert _states(images, env, "jpeg-edge-48")["states"]["scrub_prnu"]["state"] == "ok"
    res, _ = _run(images["jpeg-edge-48"], _options(_prefs(), scrub_prnu=True))
    assert res.prnu_applied


def test_one_pixel_and_tiny_images_keep_every_other_option(images, env):
    s = _states(images, env, "png-1px")["states"]
    assert s["scrub_lsb"]["state"] == "ok" and s["random_name"]["state"] == "ok" and s["optimize"]["state"] == "ok"
    res, _ = _run(images["png-1px"], _options(_prefs(), scrub_lsb=True, optimize=True))
    assert res is not None


def test_one_bit_and_32_bit_images_cannot_be_lsb_scrubbed_or_fingerprinted(images, env):
    for name in ("png-1bit", "tiff-1bit", "bmp-1bit", "tiff-32bit", "tiff-float"):
        s = _states(images, env, name)["states"]
        assert s["scrub_lsb"]["state"] == "unsupported", name
        assert s["scrub_prnu"]["state"] == "unsupported", name
        res, _ = _run(images[name], _options(_prefs(), scrub_lsb=True))
        assert res.lsb_scrub == "not_applied", name


def test_palette_images_cannot_be_fingerprinted_but_can_be_lsb_scrubbed(images, env):
    for name in ("png-palette", "gif"):
        s = _states(images, env, name)["states"]
        assert s["scrub_prnu"]["state"] == "unsupported" and "Palette" in s["scrub_prnu"]["reason"], name
        assert s["scrub_lsb"]["state"] == "ok", name


def test_animated_images_keep_the_pixel_scrubs_and_lose_png_recompression(images, env):
    for name in ("gif-animated", "webp-animated"):
        s = _states(images, env, name)["states"]
        assert s["scrub_prnu"]["state"] == "ok" and s["scrub_lsb"]["state"] == "ok", name
        res, _ = _run(images[name], _options(_prefs(), scrub_prnu=True))
        assert res.prnu_applied, name
    s = _states(images, env, "gif-animated", format="PNG")["states"]["optimize"]
    assert s["state"] == "na" and "Animated" in s["reason"]


def test_32_bit_images_can_only_be_saved_as_tiff(images, env):
    for name in ("tiff-32bit", "tiff-float"):
        formats = _states(images, env, name)["formats"]
        assert formats["TIFF"]["state"] == "ok", name
        for fmt, st in formats.items():
            if fmt != "TIFF":
                assert st["state"] == "unsupported" and "TIFF" in st["reason"], (name, fmt)
        _, err = _run(images[name], _options(_prefs(format="PNG")))
        assert err and "TIFF" in err


def test_a_long_thin_image_cannot_be_saved_in_formats_that_cap_the_side(images, env):
    """20000x4 is a tiny picture by pixel count but longer than WebP can hold."""
    formats = _states(images, env, "png-thin")["formats"]
    assert formats["WEBP"]["state"] == "unsupported" and "16,383" in formats["WEBP"]["reason"]
    for fmt in ("JPEG", "PNG", "GIF", "TIFF", "BMP", "AVIF"):
        assert formats[fmt]["state"] == "ok", fmt
    _, err = _run(images["png-thin"], _options(_prefs(format="WEBP")))
    assert err
    res, err = _run(images["png-thin"], _options(_prefs(format="JPEG")))
    assert res is not None, err


def test_the_side_limits_match_the_encoders(env):
    """Just under each limit encodes; just over is refused: the numbers the page greys out by are real."""
    limits = env["limits"]["max_side"]
    for fmt, edge in limits.items():
        if fmt == "AVIF" and fmt not in env["formats"]:
            continue
        for side, expect_ok in ((edge, True), (edge + 1, False)):
            if fmt == "AVIF" and side > 20000:
                continue  # an encoder-dependent bound; the over-the-limit direction is what matters here
            tall = Image.fromarray(np.tile(np.arange(side, dtype="uint8"), (2, 1)))
            _, err = _run(_save(tall, "PNG"), _options(_prefs(format=fmt)))
            if not expect_ok:
                assert err is not None, (fmt, side)
            elif fmt != "AVIF":
                assert err is None, (fmt, side, err)


def test_images_over_the_pixel_or_size_limit_are_blocked_before_any_upload(images, env):
    facts = web.image_facts(images["jpeg"])
    huge = {**facts, "size": [12000, 9000], "megapixels": 108.0}
    out = _judge([{"facts": huge, "prefs": _prefs(), "bytes": 1000}], env)[0]
    assert out["blocker"] and "108 megapixels" in out["blocker"] and "100 megapixel" in out["blocker"]
    exact = {**facts, "size": [10000, 10000], "megapixels": 100.0}
    assert _judge([{"facts": exact, "prefs": _prefs(), "bytes": 1}], env)[0]["blocker"] is None
    over = _judge([{"facts": facts, "prefs": _prefs(), "bytes": web.MAX_UPLOAD + 1}], env)[0]
    assert over["blocker"] and "1 GB limit" in over["blocker"]
    assert _judge([{"facts": facts, "prefs": _prefs(), "bytes": web.MAX_UPLOAD}], env)[0]["blocker"] is None
    assert _judge([{"facts": None, "prefs": _prefs(), "bytes": 5}], env)[0]["blocker"] is None


def test_deep_scan_is_unsupported_past_its_megapixel_limit(images, env):
    facts = web.image_facts(images["jpeg"])
    limit = env["limits"]["deep_scan_max_pixels"]
    big = {**facts, "size": [limit // 1000 + 1000, 1000], "megapixels": 41.0}
    small = {**facts, "size": [limit // 1000, 1000], "megapixels": 40.0}
    out = _judge([{"facts": big, "prefs": _prefs(), "bytes": 1}, {"facts": small, "prefs": _prefs(), "bytes": 1}], env)
    assert out[0]["states"]["deep"]["state"] == "unsupported" and "40 megapixels" in out[0]["states"]["deep"]["reason"]
    assert out[1]["states"]["deep"]["state"] == "ok"


def test_the_engine_agrees_that_the_pixel_scan_gives_up_past_its_limit(images, monkeypatch):
    """The page greys Deep scan out by DEEP_SCAN_MAX_PIXELS; the server must enforce the same number."""
    monkeypatch.setattr(web, "DEEP_SCAN_MAX_PIXELS", 5000)
    over = web.inspect_payload(images["png"], deep=True)  # 96x80 = 7680 pixels
    assert not over["pixels_scanned"] and any("megapixels" in n for n in over["notes"])
    monkeypatch.setattr(web, "DEEP_SCAN_MAX_PIXELS", 8000)
    assert web.inspect_payload(images["png"], deep=True)["pixels_scanned"]


def test_no_facts_rules_nothing_out(env):
    out = _judge([{"facts": None, "prefs": _prefs(scrub_prnu=True, jpeg_lossless=True), "bytes": 1}], env)[0]
    assert all(v["state"] == "ok" for v in out["states"].values())
    assert all(v["state"] == "ok" for v in out["formats"].values())


def test_effective_options_switch_off_exactly_the_options_that_cannot_apply(images, env):
    out = _states(images, env, "png", jpeg_lossless=True, scrub_dct=True, quality="80", icc="drop", scrub_lsb=True)
    eff = out["effective"]
    assert eff["jpeg_lossless"] is False and eff["scrub_dct"] is False
    assert eff["quality"] == "" and eff["icc"] == "convert"  # no profile: reset to the engine's default
    assert eff["scrub_lsb"] is True  # applies, so it is kept
    assert eff["auto_orient"] is True  # no tag: the engine's default, which neither rotates nor warns


def test_every_effective_option_set_cleans_without_a_refusal(images, env):
    """Whatever the user switched on, what the page actually sends never makes a decodable image fail on option conflicts."""
    all_on = _prefs(**{k: v for k, v in ON.items() if k not in ("c2pa", "deep", "random_name")})
    cases, meta = [], []
    for name in images:
        for fmt in ("", "PNG", "TIFF"):
            cases.append({"facts": web.image_facts(images[name]), "prefs": {**all_on, "format": fmt}, "bytes": 1})
            meta.append((name, fmt))
    verdicts = _judge(cases, env)
    refused = []
    for (name, fmt), verdict in zip(meta, verdicts, strict=True):
        _, err = _run(images[name], _options(verdict["effective"]))
        if err and any(w in err for w in ("--scrub-dct", "--jpeg-lossless", "cannot be combined", "requires a JPEG")):
            refused.append((name, fmt, err))
    assert not refused, refused


# --------------------------------------------------------------------------- the API the page reads

from test_web import Client  # noqa: E402  (a fixture-free helper shared with the web tests)


@pytest.fixture()
def server(tmp_path):
    import threading

    session = web.Session(str(tmp_path / "work"))
    httpd, session = web.make_server(0, session)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield Client(httpd.server_address[1], session)
    httpd.shutdown()
    httpd.server_close()
    session.close()


def _inspect(server, data, deep=0):
    res, body = server.upload(f"/api/inspect?deep={deep}", data)
    assert res.status == 200, body
    return json.loads(body)


def test_the_support_rules_are_served_with_the_page(server):
    res, body = server.request("GET", "/static/support.js", auth=False)
    assert res.status == 200 and res.getheader("Content-Type").startswith("text/javascript")
    assert b"ImgstripSupport" in body
    res, page = server.request("GET", "/", auth=False)
    assert b'/static/support.js' in page and page.index(b"support.js") < page.index(b"app.js")


def test_info_carries_the_limits_the_page_greys_options_out_by(server):
    status, info = server.json("GET", "/api/info")
    assert status == 200
    assert info["limits"] == {
        "max_upload": web.MAX_UPLOAD,
        "max_pixels": web.WEB_MAX_PIXELS,
        "deep_scan_max_pixels": web.DEEP_SCAN_MAX_PIXELS,
        "prnu_min_side": 48,
        "max_side": {"JPEG": 65500, "WEBP": 16383, "GIF": 65535, "AVIF": 32768},
    }


def test_inspect_reports_the_facts_of_an_image(server, images):
    facts = _inspect(server, images["jpeg-rotated"])["facts"]
    assert facts == {
        "format": "JPEG", "default_output": "JPEG", "size": [96, 80], "megapixels": 0.01,
        "bytes": len(images["jpeg-rotated"]), "mode": "RGB", "frames": 1, "orientation": 6, "profile": False,
    }
    assert _inspect(server, images["jpeg-profile"])["facts"]["profile"] is True
    animated = _inspect(server, images["gif-animated"])["facts"]
    assert animated["frames"] == 3 and animated["format"] == "GIF"
    assert _inspect(server, images["tiff-32bit"])["facts"]["mode"] == "I"


def test_facts_name_the_default_output_for_formats_that_cannot_be_written(server, tmp_path):
    ppm = tmp_path / "x.ppm"
    Image.new("RGB", (8, 8), (4, 5, 6)).save(ppm)
    facts = _inspect(server, ppm.read_bytes())["facts"]
    assert facts["format"] == "PPM" and facts["default_output"] == "PNG"


def test_unreadable_and_raw_files_have_no_facts(server):
    assert _inspect(server, b"not an image at all" * 50)["facts"] is None
    assert web.image_facts(b"") is None and web.image_facts(b"\xff\xd8\xff") is None
    raw = _inspect(server, b"FUJIFILM" + b"\x00" * 200)  # a camera RAW: recognised, never cleaned, so no facts
    assert raw["raw"] == "Fujifilm RAF" and raw["facts"] is None


def test_facts_never_decode_the_pixels(images, monkeypatch):
    """Reading the facts of a large image must stay cheap: only the header is opened."""
    loaded = []
    real_load = Image.Image.load
    monkeypatch.setattr(Image.Image, "load", lambda self, *a, **k: (loaded.append(1), real_load(self, *a, **k))[1])
    for name in ("jpeg", "png", "png-16bit", "tiff", "webp", "avif"):
        assert web.image_facts(images[name]) is not None
    assert loaded == []


def test_the_options_the_page_sends_for_a_png_survive_the_server_parser(server, images):
    """What the page sends when JPEG-only switches are greyed out is accepted and cleans the file."""
    sent = {"format": None, "quality": None, "icc": "convert", "jpeg_lossless": False, "scrub_lsb": True,
            "scrub_dct": False, "jpeg_canonical": False, "scrub_prnu": True, "auto_orient": True, "optimize": False,
            "random_name": False}
    res, body = server.upload("/api/clean", images["png"], "photo.png", sent)
    assert res.status == 200, body
    refused = {**sent, "scrub_dct": True}
    res, body = server.upload("/api/clean", images["png"], "photo.png", refused)
    assert res.status == 422 and b"--scrub-dct" in body  # what greying out spares the user from


def test_the_pixel_limit_the_page_blocks_by_is_the_one_the_server_enforces(server, images, monkeypatch):
    monkeypatch.setattr(web, "WEB_MAX_PIXELS", 5000)
    res, body = server.upload("/api/clean", images["png"], "photo.png", {})  # 96x80 = 7680 pixels
    assert res.status == 422 and b"limit" in body
    monkeypatch.setattr(web, "WEB_MAX_PIXELS", 7680)
    res, body = server.upload("/api/clean", images["png"], "photo.png", {})
    assert res.status == 200, body


def test_real_megapixel_limits_end_to_end(server, env):
    """No mocking: a 41 MP image is past the deep-scan limit, a 100.02 MP one past the cleaning limit."""
    def mask(side):
        buf = io.BytesIO()
        Image.new("1", (side, side)).save(buf, "PNG")
        return buf.getvalue()

    deep_over = mask(6400)  # 40.96 MP
    facts = _inspect(server, deep_over)["facts"]
    assert facts["megapixels"] == 40.96
    out = _judge([{"facts": facts, "prefs": _prefs(), "bytes": len(deep_over)}], env)[0]
    assert out["states"]["deep"]["state"] == "unsupported" and out["blocker"] is None
    note = _inspect(server, deep_over, deep=1)
    assert not note["pixels_scanned"] and note["notes"]  # the server really declines, as the page said it would

    cleanable = mask(6300)  # 39.69 MP: under the deep-scan limit
    facts = _inspect(server, cleanable)["facts"]
    assert _judge([{"facts": facts, "prefs": _prefs(), "bytes": 1}], env)[0]["states"]["deep"]["state"] == "ok"

    too_big = mask(10001)  # 100.02 MP
    facts = _inspect(server, too_big)["facts"]
    out = _judge([{"facts": facts, "prefs": _prefs(), "bytes": len(too_big)}], env)[0]
    assert out["blocker"] and "100 megapixel limit" in out["blocker"]
    res, body = server.upload("/api/clean", too_big, "huge.png", {})
    assert res.status == 422 and b"limit" in body  # and the server agrees it cannot be cleaned

    exactly = mask(10000)  # exactly 100 MP is allowed
    facts = _inspect(server, exactly)["facts"]
    assert _judge([{"facts": facts, "prefs": _prefs(), "bytes": len(exactly)}], env)[0]["blocker"] is None
