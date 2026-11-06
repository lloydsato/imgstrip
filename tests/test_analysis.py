"""Pixel-level hidden data detection (--check --deep)."""

import io

import numpy as np
from PIL import Image

from imgstrip import fixtures
from imgstrip.analysis import analyze_pixels, lsb_payload_estimate, spa_estimate, ws_estimate


def _png(arr):
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG")
    return buf.getvalue()


def test_clean_photo_not_flagged():
    assert analyze_pixels(_png(fixtures.photo(240, 180, 2))) == []


def test_full_payload_detected(fx):
    labels = [f.label for f in analyze_pixels(fx["png_stego.png"])]
    assert "Likely LSB steganography" in labels


def test_estimators_track_payload():
    rng = np.random.default_rng(7)
    base = fixtures.photo(300, 220, 3)[..., 1]
    for rate in (0.3, 0.6, 1.0):
        a = base.copy()
        mask = rng.random(a.shape) < rate
        a[mask] = (a[mask] & 0xFE) | rng.integers(0, 2, mask.sum(), dtype=np.uint8)
        for est in (spa_estimate(a), ws_estimate(a), lsb_payload_estimate(a)):
            assert abs(est - rate) < 0.15, (rate, est)


def test_hidden_alpha_and_palette_reported(fx):
    assert "Colour data under fully transparent pixels" in {f.label for f in analyze_pixels(fx["png_hidden_alpha.png"])}
    found = {f.label for f in analyze_pixels(fx["png_palette.png"])}
    assert {"Unused palette entries with colour data", "Duplicate palette colours in use"} <= found


def test_jpeg_not_steganalysed():
    buf = io.BytesIO()
    Image.fromarray(fixtures.photo()).save(buf, "JPEG")
    assert not any("steganography" in f.label for f in analyze_pixels(buf.getvalue()))
