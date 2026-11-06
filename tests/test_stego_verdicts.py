"""Steganography verdicts: detected / scrubbed / not detected / not scanned.

A pixel scan can find a payload but can never show there is none, and a scrub only covers LSB replacement.
These tests pin the vocabulary, that it is derived from what the engine *did* (never from what was asked
for), and that every surface (library, CLI, JSON, web API, credentials) tells the same story.
"""

from __future__ import annotations

import io
import json
import urllib.parse

import numpy as np
import pytest
from PIL import Image

from imgstrip import fixtures, stego
from imgstrip.analysis import scan_pixels
from imgstrip.cli import main
from imgstrip.core import Frame, Options, Report, _Job, strip_image
from test_web import app  # noqa: F401  (the fixture)

NOT_PROOF = "not proof that nothing is hidden"


def png_of(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG")
    return buf.getvalue()


def apng_of(frames: list[np.ndarray]) -> bytes:
    ims = [Image.fromarray(f) for f in frames]
    buf = io.BytesIO()
    ims[0].save(buf, "PNG", save_all=True, append_images=ims[1:], duration=50)
    return buf.getvalue()


def lsb_payload(arr: np.ndarray, seed: int = 1) -> np.ndarray:
    return (arr & 0xFE) | np.random.default_rng(seed).integers(0, 2, arr.shape, dtype=np.uint8)


@pytest.fixture(scope="module")
def photo() -> np.ndarray:
    return fixtures.photo(240, 180, 2)


# --------------------------------------------------------------------------
# The vocabulary
# --------------------------------------------------------------------------


def test_only_the_four_states_exist():
    assert set(stego.STATES) == {"detected", "scrubbed", "not_detected", "not_scanned"}
    with pytest.raises(ValueError):
        stego.Verdict("clean")


@pytest.mark.parametrize("state", stego.STATES)
def test_every_state_carries_a_caveat_and_never_claims_absence(state):
    v = stego.Verdict(state)
    assert v.caveat and v.summary and v.headline
    text = (v.summary + " " + v.caveat).lower()
    for claim in ("no hidden", "nothing is hidden.", "is clean", "no steganography", "safe"):
        assert claim not in text.replace(NOT_PROOF, "")


def test_the_two_states_that_say_nothing_about_absence_say_so():
    assert NOT_PROOF in stego.Verdict(stego.NOT_DETECTED).caveat
    assert NOT_PROOF in stego.Verdict(stego.SCRUBBED).caveat
    assert "Nothing was tested" in stego.Verdict(stego.NOT_SCANNED).caveat


def test_headlines_distinguish_level_and_partial_scrubs():
    assert stego.Verdict(stego.DETECTED, stego.LIKELY).headline == "detected (likely)"
    assert stego.Verdict(stego.DETECTED, stego.POSSIBLE).headline == "detected (possible)"
    assert stego.Verdict(stego.SCRUBBED, complete=False).headline == "partly scrubbed"


@pytest.mark.parametrize(
    "scan, lsb, copied, want",
    [
        (None, stego.SCRUB_APPLIED, True, stego.SCRUBBED),
        (stego.Verdict(stego.DETECTED, stego.LIKELY), stego.SCRUB_APPLIED, True, stego.SCRUBBED),
        (stego.Verdict(stego.NOT_DETECTED), stego.SCRUB_PARTIAL, True, stego.SCRUBBED),
        (stego.Verdict(stego.DETECTED, stego.LIKELY), stego.SCRUB_OFF, True, stego.DETECTED),
        (stego.Verdict(stego.DETECTED, stego.LIKELY), stego.SCRUB_NOT_APPLIED, True, stego.DETECTED),
        (stego.Verdict(stego.NOT_DETECTED), stego.SCRUB_OFF, True, stego.NOT_DETECTED),
        (None, stego.SCRUB_OFF, True, stego.NOT_SCANNED),
        (None, stego.SCRUB_NOT_APPLIED, True, stego.NOT_SCANNED),
        (stego.not_scanned("lossy"), stego.SCRUB_OFF, False, stego.NOT_SCANNED),
    ],
)
def test_the_output_verdict_follows_what_the_engine_did(scan, lsb, copied, want):
    assert stego.for_output(scan, lsb, copied).state == want


def test_a_scrub_that_could_not_be_applied_is_never_called_scrubbed():
    v = stego.for_output(None, stego.SCRUB_NOT_APPLIED, copied=True)
    assert v.state == stego.NOT_SCANNED and "could not be applied" in v.summary


def test_a_scrub_remembers_what_the_scan_of_the_original_found():
    flagged = stego.for_output(stego.Verdict(stego.DETECTED, stego.LIKELY), stego.SCRUB_APPLIED, True)
    assert flagged.source == stego.DETECTED and "original was flagged" in flagged.summary
    unscanned = stego.for_output(stego.not_scanned("x"), stego.SCRUB_APPLIED, True)
    assert unscanned.source is None


def test_lossless_and_lossy_outputs_are_described_differently():
    lossless = stego.for_output(None, stego.SCRUB_OFF, copied=True)
    lossy = stego.for_output(None, stego.SCRUB_OFF, copied=False)
    assert "stored losslessly" in lossless.summary and "survive" in lossless.summary
    assert "re-encoded" in lossy.summary and "not a guarantee" in lossy.summary


# --------------------------------------------------------------------------
# The scan: what was and was not tested
# --------------------------------------------------------------------------


def test_a_payload_is_detected(fx):
    v = scan_pixels(fx["png_stego.png"]).verdict
    assert v.state == stego.DETECTED and v.level == stego.LIKELY and "estimated payload" in v.detail
    assert v.payload_kept


def test_a_clean_photo_is_not_detected_and_the_verdict_says_what_was_tested(photo):
    scan = scan_pixels(png_of(photo))
    assert scan.findings == []
    assert scan.verdict.state == stego.NOT_DETECTED
    assert "RGB channels" in scan.verdict.summary and NOT_PROOF in scan.verdict.caveat


@pytest.mark.parametrize(
    "name, reason",
    [
        ("jpeg_everything.jpg", "lossy"),
        ("webp_lossy.webp", "lossy"),
        ("png_palette.png", "palette"),
        ("gif_animated.gif", "palette"),
        ("png_hidden_alpha.png", "too few pixels"),
    ],
)
def test_images_the_test_cannot_read_are_not_scanned_and_say_why(fx, name, reason):
    v = scan_pixels(fx[name]).verdict
    assert v.state == stego.NOT_SCANNED and reason in v.summary


def test_other_modes_are_not_scanned():
    buf = io.BytesIO()
    Image.fromarray(np.zeros((80, 80), dtype=np.uint16) + 1000).save(buf, "PNG")
    v = scan_pixels(buf.getvalue()).verdict
    assert v.state == stego.NOT_SCANNED and "not covered" in v.summary


def test_every_frame_of_an_animation_is_scanned(photo):
    """A payload only in the second frame used to read as clean: only frame 0 was decoded."""
    clean = apng_of([photo, np.roll(photo, 5, axis=0)])
    hidden = apng_of([photo, lsb_payload(photo)])
    assert scan_pixels(clean).verdict.state == stego.NOT_DETECTED
    assert "2 of 2 frames" in scan_pixels(clean).verdict.summary
    v = scan_pixels(hidden).verdict
    assert v.state == stego.DETECTED and v.level == stego.LIKELY


def test_a_partly_scanned_animation_says_so(photo, monkeypatch):
    from imgstrip import analysis

    monkeypatch.setattr(analysis, "_FRAME_PIXEL_BUDGET", photo.shape[0] * photo.shape[1] + 1)
    v = scan_pixels(apng_of([np.roll(photo, i, axis=0) for i in range(4)])).verdict
    assert v.state == stego.NOT_DETECTED and not v.complete
    assert "of 4 frames" in v.summary and "2 of 4" in v.summary


def test_findings_keep_their_labels(fx):
    labels = [f.label for f in scan_pixels(fx["png_stego.png"]).findings]
    assert "Likely LSB steganography" in labels


# --------------------------------------------------------------------------
# The engine reports what it did
# --------------------------------------------------------------------------


def test_the_engine_reports_the_scrub_it_applied(photo):
    src = png_of(photo)
    assert strip_image(src, Options()).lsb_scrub == stego.SCRUB_OFF
    r = strip_image(src, Options(scrub_lsb=True))
    assert r.lsb_scrub == stego.SCRUB_APPLIED
    assert any("randomised the least-significant bit" in a for a in r.actions)


def test_a_scrub_that_cannot_be_applied_is_reported_and_claims_no_action():
    bilevel = io.BytesIO()
    Image.fromarray((np.random.default_rng(0).random((64, 64)) > 0.5)).save(bilevel, "PNG")
    r = strip_image(bilevel.getvalue(), Options(scrub_lsb=True))
    assert r.lsb_scrub == stego.SCRUB_NOT_APPLIED
    assert not any("randomised" in a for a in r.actions)
    assert any("not applicable" in w for w in r.warnings)
    assert r.pixels_exact, "nothing was changed, so the pixels are still exact"


def test_bit_exact_jpeg_cannot_be_scrubbed(fx):
    r = strip_image(fx["jpeg_everything.jpg"], Options(jpeg_lossless=True, scrub_lsb=True))
    assert r.lsb_scrub == stego.SCRUB_NOT_APPLIED
    assert any("no effect" in w for w in r.warnings)


def test_a_partial_scrub_is_reported_as_partial(photo):
    job = _Job(b"", Options(scrub_lsb=True), Report("PNG"))
    frames = [Frame(image=Image.fromarray(photo)), Frame(image=Image.new("F", (8, 8)))]
    job._scrub_lsb(frames)
    assert job.lsb_scrub == stego.SCRUB_PARTIAL
    assert any("not applicable to F" in w for w in job.warnings)


def test_animations_are_scrubbed_in_every_frame(photo):
    r = strip_image(apng_of([photo, lsb_payload(photo)]), Options(scrub_lsb=True))
    assert r.lsb_scrub == stego.SCRUB_APPLIED and r.frames == 2


def test_a_scrubbed_file_reads_as_a_payload_to_the_detector_which_is_why_the_state_is_separate(photo):
    r = strip_image(png_of(photo), Options(scrub_lsb=True))
    assert scan_pixels(r.data).verdict.state == stego.DETECTED


def test_credentials_record_the_scrub_that_happened_not_the_one_asked_for(creds, signer, fx):
    def operations(source, options):
        result = strip_image(source, options)
        return creds.gather_evidence(source, result, options).operations

    assert "lsb-scrubbed" in operations(png_of(fixtures.photo(120, 100, 3)), Options(scrub_lsb=True))
    assert "lsb-scrubbed" not in operations(fx["jpeg_everything.jpg"], Options(jpeg_lossless=True, scrub_lsb=True))
    bilevel = io.BytesIO()
    Image.fromarray((np.random.default_rng(0).random((64, 64)) > 0.5)).save(bilevel, "PNG")
    assert "lsb-scrubbed" not in operations(bilevel.getvalue(), Options(scrub_lsb=True))


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


@pytest.fixture()
def files(tmp_path, photo):
    (tmp_path / "holiday.png").write_bytes(png_of(photo))
    (tmp_path / "memo.png").write_bytes(fixtures.png_lsb_stego())
    buf = io.BytesIO()
    Image.fromarray(photo).save(buf, "JPEG")
    (tmp_path / "holiday.jpg").write_bytes(buf.getvalue())
    return tmp_path


def run_json(capsys, *argv):
    code = main(["--json", *argv])
    return code, json.loads(capsys.readouterr().out)["files"]


def test_check_without_deep_says_the_pixels_were_not_scanned(files, capsys):
    assert main(["--check", str(files / "holiday.png")]) == 0
    out = capsys.readouterr().out
    assert out.startswith("clean ") and "steganography: not scanned" in out and "--deep" in out


def test_check_deep_reports_all_three_findings_in_plain_words(files, capsys):
    code = main(["--check", "--deep", str(files / "holiday.png"), str(files / "memo.png"), str(files / "holiday.jpg")])
    out = capsys.readouterr().out
    assert code == 3
    assert "steganography: not detected: the LSB tests found nothing" in out and NOT_PROOF in out
    assert "steganography: detected (likely)" in out
    assert "steganography: not scanned: JPEG is lossy" in out


def test_json_carries_the_verdict_for_check(files, capsys):
    _code, got = run_json(capsys, "--check", "--deep", str(files / "holiday.png"), str(files / "memo.png"))
    by_name = {f["file"].rsplit("/", 1)[1]: f["steganography"] for f in got}
    assert by_name["holiday.png"]["state"] == "not_detected" and NOT_PROOF in by_name["holiday.png"]["caveat"]
    assert by_name["memo.png"]["state"] == "detected" and by_name["memo.png"]["level"] == "likely"
    _code, got = run_json(capsys, "--check", str(files / "holiday.png"))
    assert got[0]["steganography"]["state"] == "not_scanned"


def test_cleaning_with_deep_keeps_a_flagged_payload_and_warns(files, capsys):
    out_dir = files / "out"
    assert main(["--deep", "-o", str(out_dir), str(files / "memo.png")]) == 0
    out = capsys.readouterr().out
    assert "steganography: detected (likely)" in out and "--scrub-lsb" in out
    code, got = run_json(capsys, "--deep", "-o", str(out_dir), "--force", str(files / "memo.png"))
    assert got[0]["steganography"]["state"] == "detected"
    assert any("still in the cleaned file" in w for w in got[0]["warnings"])


def test_cleaning_with_the_scrub_says_scrubbed_and_what_it_does_not_cover(files, capsys):
    out_dir = files / "out"
    assert main(["--deep", "--scrub-lsb", "-o", str(out_dir), str(files / "memo.png")]) == 0
    out = capsys.readouterr().out
    assert "steganography: scrubbed" in out and "original was flagged" in out
    assert "does not cover other ways of hiding data" in out


def test_cleaning_without_scrub_or_scan_is_quiet_but_machine_readable(files, capsys):
    out_dir = files / "out"
    assert main(["-o", str(out_dir), str(files / "holiday.png")]) == 0
    assert "steganography" not in capsys.readouterr().out
    _code, got = run_json(capsys, "-o", str(out_dir), "--force", str(files / "holiday.png"))
    v = got[0]["steganography"]
    assert v["state"] == "not_scanned" and "stored losslessly" in v["summary"]


def test_cleaning_verbose_always_says_where_the_pixels_stand(files, capsys):
    assert main(["-v", "-o", str(files / "out"), str(files / "holiday.png")]) == 0
    assert "steganography: not scanned" in capsys.readouterr().out


def test_a_requested_scrub_that_could_not_happen_is_not_reported_as_scrubbed(files, capsys):
    _code, got = run_json(
        capsys, "--jpeg-lossless", "--scrub-lsb", "-o", str(files / "out"), str(files / "holiday.jpg")
    )
    v = got[0]["steganography"]
    assert v["state"] == "not_scanned" and "could not be applied" in v["summary"]


def test_deep_does_not_apply_to_credential_verification(files):
    with pytest.raises(SystemExit) as exc:
        main(["--deep", "--c2pa-verify", str(files / "holiday.png")])
    assert exc.value.code == 2


def test_a_pixel_scan_that_fails_is_not_scanned_not_an_error(files, capsys, monkeypatch, in_process):
    from imgstrip import analysis
    from imgstrip.core import StripError

    def boom(_data):
        raise StripError("cannot decode image: boom")

    monkeypatch.setattr(analysis, "scan_pixels", boom)
    assert main(["--check", "--deep", str(files / "holiday.png")]) == 0
    assert "steganography: not scanned: pixel analysis failed: cannot decode image: boom" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Web API
# --------------------------------------------------------------------------


def test_the_web_scan_returns_a_verdict(app):  # noqa: F811
    data = fixtures.png_lsb_stego()
    _res, body = app.upload("/api/inspect", data, "memo.png")
    shallow = json.loads(body)["steganography"]
    assert shallow["state"] == "not_scanned" and "Deep scan" in shallow["summary"]
    _res, body = app.upload("/api/inspect?deep=1", data, "memo.png")
    deep = json.loads(body)["steganography"]
    assert deep["state"] == "detected" and deep["level"] == "likely"


def test_the_web_scan_of_a_clean_photo_is_not_detected_with_its_caveat(app, photo):  # noqa: F811
    _res, body = app.upload("/api/inspect?deep=1", png_of(photo), "holiday.png")
    v = json.loads(body)["steganography"]
    assert v["state"] == "not_detected" and NOT_PROOF in v["caveat"]


def test_a_huge_image_and_an_unreadable_file_are_not_scanned(app, monkeypatch):  # noqa: F811
    monkeypatch.setattr("imgstrip.web.server.DEEP_SCAN_MAX_PIXELS", 100)
    _res, body = app.upload("/api/inspect?deep=1", fixtures.png_lsb_stego(), "memo.png")
    assert json.loads(body)["steganography"]["state"] == "not_scanned"
    _res, body = app.upload("/api/inspect?deep=1", b"not an image" * 20, "x.png")
    v = json.loads(body)["steganography"]
    assert v["state"] == "not_scanned" and "cannot be decoded" in v["summary"]


def test_the_web_clean_result_reports_the_scrub_that_happened(app):  # noqa: F811
    off = app.clean(fixtures.png_lsb_stego(), "memo.png")
    assert off["scrubbed"] is False and off["steganography"]["state"] == "not_scanned"
    on = app.clean(fixtures.png_lsb_stego(), "memo.png", {"scrub_lsb": True})
    assert on["scrubbed"] is True and on["steganography"]["state"] == "scrubbed"


def test_the_web_never_calls_an_impossible_scrub_scrubbed(app, fx):  # noqa: F811
    got = app.clean(fx["jpeg_everything.jpg"], "photo.jpg", {"scrub_lsb": True, "jpeg_lossless": True})
    assert got["scrubbed"] is False
    assert got["steganography"]["state"] == "not_scanned" and "could not be applied" in got["steganography"]["summary"]


def test_the_page_can_pass_on_what_its_scan_found(app):  # noqa: F811
    def clean(scan):
        res, body = app.upload(
            "/api/clean",
            fixtures.png_lsb_stego(),
            "memo.png",
            {"scrub_lsb": True},
            headers={"X-Scan": scan} if scan else None,
        )
        return res.status, json.loads(body)

    status, got = clean("detected")
    assert status == 200 and got["steganography"]["source"] == "detected"
    assert "original was flagged" in got["steganography"]["summary"]
    status, got = clean("not_detected")
    assert status == 200 and got["steganography"]["source"] == "not_detected"
    status, got = clean("clean")
    assert status == 400 and "X-Scan" in got["error"]
    status, got = clean(urllib.parse.quote("detected\r\nX: y"))
    assert status == 400


def test_a_scan_state_is_ignored_when_nothing_was_scrubbed(app):  # noqa: F811
    res, body = app.upload("/api/clean", fixtures.png_lsb_stego(), "memo.png", {}, headers={"X-Scan": "detected"})
    got = json.loads(body)
    assert res.status == 200 and got["steganography"]["state"] == "not_scanned"
