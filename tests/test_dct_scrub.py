"""Layer B: --scrub-dct destroys payloads stored in coefficient parities, and says what it cost."""

import io

import numpy as np
import pytest
from PIL import Image

import dct_embedders as E
import jpeg_craft
from conftest import assert_clean
from imgstrip import Options, StripError, dct, fixtures, inspect_bytes, stego, strip_image
from test_dct_canonical import QT
from test_dct_codec import make, pixels

FULL = Options(scrub_dct="full", dct_min_psnr=0)


def cover(w=256, h=192, quality=85, subsampling=2, seed=3):
    return make(w, h, seed=seed, quality=quality, subsampling=subsampling)


def scrubbed(data, **kw):
    return strip_image(data, Options(scrub_dct="full", dct_min_psnr=0, **kw))


# -- the transformation itself ---------------------------------------------------------------------------------


def _frame_with(values):
    """A one-component frame holding exactly ``values`` (N x 64 natural order) in its visible blocks."""
    n = len(values)
    data = jpeg_craft.craft(np.asarray(values, dtype=np.int16), n, 1, QT)
    return dct.read(data)


def test_rule_on_every_value_a_coefficient_can_take():
    values = np.arange(-1023, 1024, dtype=np.int16)
    blocks = np.zeros((len(values) // 63 + 1, 64), dtype=np.int16)
    blocks[:, 1:].reshape(-1)[: len(values)] = values[: blocks[:, 1:].size] if blocks[:, 1:].size >= len(values) else values
    frame = _frame_with(blocks)
    before = frame.comps[0].coef.copy()
    dct.scrub(frame, np.random.default_rng(1))
    after = frame.comps[0].coef
    small = (before >= -2) & (before <= 1)
    assert (after[small] == 0).all()
    assert ((after[~small] >> 1) == (before[~small] >> 1)).all()
    assert dct.check_scrub(frame, [before]) is None


def test_output_parity_is_independent_of_input_parity():
    rng = np.random.default_rng(4)
    blocks = np.zeros((4000, 64), dtype=np.int16)
    blocks[:, 1:] = np.rint(rng.laplace(0, 6, (4000, 63))).astype(np.int16)
    frame = _frame_with(blocks)
    before = frame.comps[0].coef.copy()
    dct.scrub(frame, np.random.default_rng(9))
    after = frame.comps[0].coef
    keep = (before >= 2) | (before <= -3)  # the re-drawn pairs
    odd_in, odd_out = (before & 1) == 1, (after & 1) == 1
    n_even, n_odd = int((keep & ~odd_in).sum()), int((keep & odd_in).sum())
    assert n_even > 20000 and n_odd > 20000
    p_even = float((keep & ~odd_in & odd_out).sum()) / n_even
    p_odd = float((keep & odd_in & odd_out).sum()) / n_odd
    # Both are 1/2: the output's low bit says nothing about the input's (5 sigma at these sample sizes)
    assert abs(p_even - 0.5) < 0.012 and abs(p_odd - 0.5) < 0.012


def test_zeros_stay_zero_and_nothing_moves_more_than_its_pair():
    blocks = jpeg_craft.random_blocks(300, seed=8)
    frame = _frame_with(blocks)
    before = frame.comps[0].coef.copy()
    dct.scrub(frame, np.random.default_rng(0))
    after = frame.comps[0].coef
    assert (after[before == 0] == 0).all()
    assert (np.abs(after.astype(int) - before) <= 2).all()


def test_two_runs_differ_and_a_seed_repeats():
    blocks = jpeg_craft.random_blocks(200, seed=1)
    outs = []
    for seed in (1, 1, 2):
        frame = _frame_with(blocks)
        dct.scrub(frame, np.random.default_rng(seed))
        outs.append(frame.comps[0].coef.copy())
    assert np.array_equal(outs[0], outs[1]) and not np.array_equal(outs[0], outs[2])


def test_the_real_pipeline_is_randomised():
    data = cover(96, 64)
    assert scrubbed(data).data != scrubbed(data).data


# -- payloads ---------------------------------------------------------------------------------------------------

QUALITIES = [50, 75, 90, 95]


@pytest.mark.parametrize("name", E.NAMES)
@pytest.mark.parametrize("quality", QUALITIES)
@pytest.mark.parametrize("subsampling", [2, 0])
def test_embedded_payloads_are_destroyed(name, quality, subsampling):
    stego_jpeg, bits = E.make_stego(cover(quality=quality, subsampling=subsampling), name, 1500)
    assert len(bits) >= 400
    assert E.read_back(stego_jpeg, name, bits) == 0.0, "the embedder must work before the cleaner is tried"
    out = scrubbed(stego_jpeg).data
    ber = E.read_back(out, name, bits)
    assert abs(ber - 0.5) < 0.08, f"{name} q{quality}: payload still readable (bit error rate {ber:.3f})"


def _attacker_accuracy(name, images=8, payload=1200):
    """(accuracy on the unscrubbed file, accuracy on the scrubbed file) of a supervised attacker.

    The attacker gets everything but the payload: the tool, the key and the position of every carrier.  It learns
    P(bit | coefficient value) on half of the samples and guesses on the other half.
    """

    def feature(v):
        v = v.astype(int)
        big = np.abs(v) > 9  # keep the parity of large values: the attacker loses nothing by binning them
        return np.where(big, np.sign(v) * (10 + (np.abs(v) & 1)), v)

    after, before, labels = [], [], []
    for seed in range(images):
        stego_jpeg, bits = E.make_stego(cover(160, 128, seed=seed + 10, quality=60 + 5 * seed), name, payload, seed=seed)
        frame = dct.read(stego_jpeg)
        pos = E.carrier_positions(frame, name, len(bits), key=seed)
        before.append(feature(E.values_at(frame, pos)))
        dct.scrub(frame, np.random.default_rng(seed))
        after.append(feature(E.values_at(frame, pos)))
        labels.append(bits)
    y = np.concatenate(labels)
    assert len(y) > 4000

    def accuracy(x):
        x = np.concatenate(x)
        half = len(y) // 2
        table = {int(v): int(y[:half][x[:half] == v].mean() > 0.5) for v in np.unique(x[:half])}
        guess = np.array([table.get(int(v), 0) for v in x[half:]])
        return float((guess == y[half:]).mean())

    return accuracy(before), accuracy(after)


@pytest.mark.parametrize("name", ["jsteg", "outguess"])
def test_supervised_attacker_with_the_true_carrier_positions_learns_nothing(name):
    """For LSB replacement the scrubbed value carries no information about the bit, even to an attacker who knows
    exactly where every bit sits: the output depends on the pair only, and replacement never leaves the pair."""
    raw, scrubbed_accuracy = _attacker_accuracy(name)
    assert raw > 0.99, "the attacker must read an unscrubbed file perfectly, or this test is blind"
    assert scrubbed_accuracy < 0.53, f"{name}: the scrubbed value still tells the attacker about the bit"


def test_matching_embedders_leak_only_to_an_attacker_who_knows_the_carriers():
    """Known limit, pinned so it cannot silently get worse.

    An embedder that changes values by +-1 (LSB matching, here steghide-like and moving +-1 away from zero) leaves
    the pair, so the pair index of the scrubbed value says a little about the bit.  Using that needs the carrier
    positions, and those were chosen among the non-zero coefficients, which the scrub changes: the attacker in
    this test is given them anyway.  The key-based extractor still reads noise (see the payload tests above).
    """
    raw, scrubbed_accuracy = _attacker_accuracy("steghide")
    assert raw > 0.99
    assert scrubbed_accuracy < 0.75


def test_default_reencode_leaves_a_zero_changing_payload_but_the_dct_scrub_does_not():
    """The case that motivates the feature: re-encoding at q95 keeps most of a J-UNIWARD-style payload."""
    stego_jpeg, bits = E.make_stego(cover(256, 192, quality=95), "syndrome", 1500)
    reencoded = strip_image(stego_jpeg, Options()).data
    assert E.read_back(reencoded, "syndrome", bits) < 0.2
    assert abs(E.read_back(scrubbed(stego_jpeg).data, "syndrome", bits) - 0.5) < 0.08


def test_jpeg_lossless_alone_keeps_every_payload():
    for name in E.NAMES:
        stego_jpeg, bits = E.make_stego(cover(160, 120), name, 600)
        out = strip_image(stego_jpeg, Options(jpeg_lossless=True)).data
        assert E.read_back(out, name, bits) == 0.0


def test_payload_hidden_in_chroma_and_dc_is_destroyed_too():
    data = cover(160, 120, subsampling=2)
    frame = dct.read(data)
    rng = np.random.default_rng(5)
    secrets = []
    for comp in frame.comps:  # LSB-replace every coefficient of every component, DC included
        c = comp.coef[: comp.blocks_h, : comp.blocks_w]
        bits = rng.integers(0, 2, c.shape, dtype=np.int16)
        c[...] = (c & np.int16(-2)) | bits
        secrets.append(bits)
    tainted = b"\xff\xd8" + dct.write(frame) + b"\xff\xd9"
    read = dct.read(tainted)
    for comp, bits in zip(read.comps, secrets, strict=True):
        assert np.array_equal(comp.coef[: comp.blocks_h, : comp.blocks_w] & 1, bits)
    out = dct.read(scrubbed(tainted).data)
    for comp, bits in zip(out.comps, secrets, strict=True):
        got = comp.coef[: comp.blocks_h, : comp.blocks_w] & 1
        assert abs(float((got != bits).mean()) - 0.5) < 0.05


# -- what it costs ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("quality", [40, 60, 75, 85, 92, 97])
@pytest.mark.parametrize("subsampling", [0, 2])
def test_reported_quality_loss_is_honest_and_bounded(quality, subsampling):
    data = cover(200, 150, quality=quality, subsampling=subsampling)
    result = scrubbed(data)
    actual = dct.psnr(pixels(data), pixels(result.data))
    assert result.dct_psnr == actual  # what the report says is what was measured on the decoded pictures
    assert actual >= 27, f"q{quality}: the scrub cost {actual} dB"
    assert len(result.data) < len(data)  # removing the small coefficients can only shrink the file


def test_the_estimate_made_before_writing_tracks_the_measured_loss():
    for quality in (50, 85, 95):
        data = cover(160, 120, quality=quality)
        frame = dct.read(data)
        stats = dct.scrub(frame, np.random.default_rng(0))
        measured = dct.psnr(pixels(data), pixels(b"\xff\xd8" + dct.write(frame) + b"\xff\xd9"))
        assert -0.5 < measured - stats.psnr < 3.0


def test_the_quality_floor_refuses_instead_of_wrecking_the_picture():
    data = cover(120, 90, quality=85)
    with pytest.raises(StripError, match="dB"):
        strip_image(data, Options(scrub_dct="full", dct_min_psnr=80))
    assert strip_image(data, Options(scrub_dct="full", dct_min_psnr=0)).data


def test_a_picture_with_nothing_to_scrub_is_left_alone():
    flat = Image.new("RGB", (64, 48), (120, 130, 140))
    buf = io.BytesIO()
    flat.save(buf, "JPEG", quality=90)
    result = scrubbed(buf.getvalue())
    assert np.array_equal(pixels(result.data), pixels(buf.getvalue()))


# -- the contract of the option ---------------------------------------------------------------------------------


def test_result_and_report_say_what_was_done():
    result = scrubbed(cover(96, 64))
    assert result.dct_scrub == stego.DCT_SCRUBBED and not result.pixels_exact and not result.verified_exact
    assert any("DCT coefficient" in a for a in result.actions)
    report = stego.dct_report(result.dct_scrub, result.dct_psnr)
    assert report["state"] == "scrubbed" and "not proof" in report["caveat"]
    assert stego.dct_report(stego.DCT_OFF) is None
    assert "canonical" in stego.dct_report(stego.DCT_CANONICAL)["summary"]
    assert "survives" in stego.dct_report(stego.DCT_CANONICAL)["caveat"]


def test_output_is_clean_and_has_no_metadata(fx):
    result = strip_image(fx["jpeg_everything.jpg"], Options(scrub_dct="full", dct_min_psnr=0))
    assert fixtures.CANARY not in result.data
    assert_clean(result.data, ".jpg", allow=("ICC color profile", "EXIF"))
    report = inspect_bytes(result.data)
    assert report.format == "JPEG" and not report.error


def test_scrub_is_off_unless_asked_for(fx):
    for name in [n for n in fx if n.endswith(".jpg")]:
        result = strip_image(fx[name], Options())
        assert result.dct_scrub == stego.DCT_OFF
        assert not any("DCT" in a for a in result.actions)


def test_options_that_cannot_work_together_are_refused(fx):
    jpg = fx["jpeg_everything.jpg"]
    with pytest.raises(StripError, match="fingerprint"):
        strip_image(jpg, Options(scrub_dct="full", scrub_prnu=True))
    with pytest.raises(StripError, match="JPEG"):
        strip_image(jpg, Options(scrub_dct="full", format="png"))
    with pytest.raises(StripError, match="JPEG"):
        strip_image(fx["png_everything.png"], Options(scrub_dct="full"))
    with pytest.raises(StripError, match="unknown"):
        strip_image(jpg, Options(scrub_dct="sideways"))
    result = strip_image(jpg, Options(scrub_dct="full", scrub_lsb=True, dct_min_psnr=0))
    assert any("--scrub-lsb has no effect" in w for w in result.warnings)


def test_true_means_full():
    data = cover(64, 48)
    assert strip_image(data, Options(scrub_dct=True, dct_min_psnr=0)).dct_scrub == "scrubbed"


def test_jpegs_the_codec_cannot_represent_are_refused_not_copied():
    data = cover(64, 48)
    sof = data.index(b"\xff\xc0")
    arithmetic = data[: sof + 1] + b"\xc9" + data[sof + 2 :]
    # Pillow cannot open it either, so the cleaner fails before the codec; both ways nothing is written.
    for mode in ("canonical", "full"):
        with pytest.raises(StripError):
            strip_image(arithmetic, Options(scrub_dct=mode))


def test_every_jpeg_fixture_can_be_scrubbed_or_is_refused_cleanly(fx):
    done = 0
    for name in [n for n in fx if n.endswith(".jpg")]:
        try:
            result = strip_image(fx[name], Options(scrub_dct="full", dct_min_psnr=0))
        except StripError:
            continue
        done += 1
        assert result.output_format == "JPEG" and result.dct_scrub == "scrubbed"
        assert fixtures.CANARY not in result.data
    assert done >= 3


def test_corrupted_jpegs_fail_with_strip_error_only():
    rng = np.random.default_rng(21)
    base = cover(64, 48, quality=80)
    for _ in range(150):
        buf = bytearray(base)
        for _ in range(int(rng.integers(1, 4))):
            buf[int(rng.integers(0, len(buf)))] ^= 1 << int(rng.integers(0, 8))
        try:
            strip_image(bytes(buf), Options(scrub_dct="full", dct_min_psnr=0))
        except StripError:
            pass


def test_scrubbing_twice_still_holds_the_promise():
    stego_jpeg, bits = E.make_stego(cover(200, 150), "jsteg", 1000)
    once = scrubbed(stego_jpeg).data
    twice = scrubbed(once).data
    assert abs(E.read_back(twice, "jsteg", bits) - 0.5) < 0.08
    assert np.abs(pixels(twice).astype(int) - pixels(once)).max() < 255
