"""The verifier's error measure: the integer fast path must equal the float reference exactly."""

import numpy as np
import pytest

from imgstrip import core
from imgstrip.core import _mean_error


def reference(got, want, alpha=False, exact=False):
    """The measure as first written: float64 differences summed in row blocks."""
    if got.ndim == 2:
        got, want = got[..., None], want[..., None]
    full = 65535 if got.dtype == np.uint16 or want.dtype == np.uint16 else 255
    rows = max(1, (1 << 22) // max(1, got.shape[1] * got.shape[2]))
    total = 0.0
    for top in range(0, got.shape[0], rows):
        a = got[top : top + rows].astype(np.float64)
        b = want[top : top + rows].astype(np.float64)
        total += float(np.abs(a - b).sum())
    return total / max(1, got.size) * (255.0 / full)


def _random(rng, dtype, shape):
    top = 255 if dtype is np.uint8 else 65535
    return rng.integers(0, top + 1, shape).astype(dtype)


@pytest.mark.parametrize("dtypes", [(np.uint8, np.uint8), (np.uint16, np.uint16), (np.uint8, np.uint16), (np.uint16, np.uint8)])
@pytest.mark.parametrize("shape", [(1, 1), (1, 1, 1), (1, 9, 3), (9, 1, 3), (7, 5, 3), (64, 64), (130, 257, 4), (1500, 1100, 3)])
def test_integer_path_equals_float_reference_exactly(dtypes, shape):
    rng = np.random.default_rng(hash((dtypes[0].__name__, dtypes[1].__name__, shape)) % 2**32)
    a, b = _random(rng, dtypes[0], shape), _random(rng, dtypes[1], shape)
    assert _mean_error(a, b, False, False) == reference(a, b)


def test_extreme_values_do_not_overflow_the_narrow_integer_type():
    # 0 vs 255 is the largest difference an int16 must hold; 0 vs 65535 for the 16-bit case.
    for dtype, top in ((np.uint8, 255), (np.uint16, 65535)):
        zeros, full = np.zeros((300, 300, 3), dtype), np.full((300, 300, 3), top, dtype)
        assert _mean_error(zeros, full, False, False) == pytest.approx(255.0)
        assert _mean_error(full, zeros, False, False) == pytest.approx(255.0)
        assert _mean_error(full, full, False, False) == 0.0


def test_a_large_block_sum_stays_exact():
    """One block holds up to ~4M values; an int16 accumulator would overflow, the int64 sum must not."""
    a = np.zeros((2048, 1024, 2), np.uint8)
    b = np.full_like(a, 255)
    assert _mean_error(a, b, False, False) == 255.0
    big = np.zeros((4000, 4000, 3), np.uint8)
    assert _mean_error(big, np.full_like(big, 255), False, False) == 255.0


def test_inputs_are_never_modified():
    rng = np.random.default_rng(5)
    a, b = _random(rng, np.uint8, (50, 60, 3)), _random(rng, np.uint8, (50, 60, 3))
    a0, b0 = a.copy(), b.copy()
    _mean_error(a, b, False, False)
    assert (a == a0).all() and (b == b0).all()


def test_non_contiguous_and_readonly_inputs():
    rng = np.random.default_rng(6)
    base = _random(rng, np.uint8, (80, 90, 3))
    a, b = base[::2, ::3], base[1::2, 1::3][: len(base[::2]), : base[::2, ::3].shape[1]]
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    b.flags.writeable = False
    assert _mean_error(a, b, False, False) == reference(a, b)


def test_float_and_signed_inputs_still_use_the_float_path():
    rng = np.random.default_rng(7)
    f1, f2 = rng.random((20, 20, 3)).astype(np.float32), rng.random((20, 20, 3)).astype(np.float32)
    assert _mean_error(f1, f2, False, False) == pytest.approx(reference(f1, f2))
    i1, i2 = rng.integers(-100, 100, (20, 20)).astype(np.int32), rng.integers(-100, 100, (20, 20)).astype(np.int32)
    assert _mean_error(i1, i2, False, False) == pytest.approx(reference(i1, i2))


def test_exact_and_alpha_branches_are_unchanged():
    rng = np.random.default_rng(8)
    a = _random(rng, np.uint8, (40, 40, 4))
    assert _mean_error(a, a.copy(), False, True) == 0.0
    a[3, 3, 3] = 255  # an opaque pixel, so a colour change there is plainly visible
    changed = a.copy()
    changed[3, 3, 0] ^= 128
    assert _mean_error(a, changed, False, True) == 1.0  # no alpha: a flag, "something differs"
    assert _mean_error(a, changed, True, True) == 128.0  # alpha: the largest difference found
    # colour under fully transparent pixels is deliberately ignored in exact mode
    hidden, shown = a.copy(), a.copy()
    hidden[5, 5] = (1, 2, 3, 0)
    shown[5, 5] = (9, 8, 7, 0)
    assert _mean_error(hidden, shown, True, True) == 0.0
    # lossy + alpha weights colour by visibility, so a change under alpha 0 costs nothing
    assert _mean_error(hidden, shown, True, False) == 0.0
    assert _mean_error(a, changed, True, False) > 0.0


def test_verification_still_rejects_a_picture_that_changed():
    """End to end: a re-encode that deviates beyond tolerance must still fail closed."""
    import io

    from PIL import Image

    from imgstrip import VerificationError, strip_image

    rng = np.random.default_rng(9)
    noise = rng.integers(0, 256, (128, 128, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(noise).save(buf, "JPEG", quality=75)
    # Re-compressing pure noise cannot stay within the tolerance (mean error about 6.8): fail closed, don't pass it.
    with pytest.raises(VerificationError, match=r"deviates too much \(mean error 6\.[0-9]\)"):
        strip_image(buf.getvalue(), core.Options())
    # ... while a smooth picture re-encodes well inside it
    smooth = np.tile(np.linspace(0, 255, 128, dtype=np.uint8)[None, :, None], (128, 1, 3))
    buf = io.BytesIO()
    Image.fromarray(smooth).save(buf, "JPEG", quality=90)
    assert strip_image(buf.getvalue(), core.Options()).verified_exact is not None


def _noise(fmt, size=96, quality=90, seed=21):
    import io

    from PIL import Image

    rng = np.random.default_rng(seed)
    buf = io.BytesIO()
    Image.fromarray(rng.integers(0, 256, (size, size, 3), dtype=np.uint8)).save(buf, fmt, quality=quality)
    return buf.getvalue()


@pytest.mark.parametrize("fmt", ["JPEG", "WEBP"])
def test_the_refusal_of_a_noisy_picture_says_what_to_do_and_that_advice_works(fmt):
    """The message points to PNG; PNG must really get the same picture through (checked, not assumed)."""
    from imgstrip import VerificationError, strip_image

    data = _noise(fmt)
    with pytest.raises(VerificationError) as refused:
        strip_image(data, core.Options())
    text = str(refused.value)
    assert "deviates too much" in text and core.LOSSY_ADVICE in text and "--format png" in text
    result = strip_image(data, core.Options(format="png"))
    assert result.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_higher_quality_does_not_get_a_noisy_picture_through():
    """Why the message does not say 'try a higher quality': the allowed error shrinks as the quality rises."""
    from imgstrip import VerificationError, strip_image

    data = _noise("JPEG")
    for quality in (95, 100):
        with pytest.raises(VerificationError, match="deviates too much"):
            strip_image(data, core.Options(quality=quality))


def test_the_block_refusal_carries_the_same_advice():
    want = np.full((16, 16, 3), 128, np.uint8)
    got = want.copy()
    got[8:16, 8:16] = 0
    blocks = core.BlockError()
    blocks.add(got, want, False, 0)
    job = core._Job(b"", core.Options(), None)
    with pytest.raises(core.VerificationError, match=r"8x8 block at x=8, y=8.*part of the picture changed\. If the picture is very noisy") as e:
        job._check_blocks(0, blocks, "JPEG")
    assert "--format png" in str(e.value)
