"""--scrub-dct and --jpeg-canonical through the command line, the web API, the verdict text and credentials."""

import json
import sys

import numpy as np
import pytest

import dct_embedders as E
from imgstrip import Options, dct, inspect_bytes, stego, strip_image
from imgstrip.cli import main
from imgstrip.web import server as web
from test_dct_codec import make, pixels
from test_web import app  # noqa: F401  (the fixture)


@pytest.fixture
def jpg(tmp_path):
    p = tmp_path / "photo.jpg"
    p.write_bytes(make(200, 150, seed=2, quality=85))
    return p


@pytest.fixture
def stego_jpg(tmp_path):
    data, bits = E.make_stego(make(200, 150, seed=2, quality=85), "jsteg", 800)
    p = tmp_path / "secret.jpg"
    p.write_bytes(data)
    return p, bits


# -- command line ----------------------------------------------------------------------------------------------


def test_scrub_dct_flag_destroys_a_payload_and_reports_it(stego_jpg, tmp_path, capsys):
    path, bits = stego_jpg
    out = tmp_path / "clean.jpg"
    assert main([str(path), "-o", str(out), "--scrub-dct"]) == 0
    text = capsys.readouterr().out
    assert "jpeg coefficients: scrubbed" in text and "dB" in text and "not proof" in text
    assert abs(E.read_back(out.read_bytes(), "jsteg", bits) - 0.5) < 0.08
    assert inspect_bytes(out.read_bytes()).clean


def test_without_the_flag_nothing_changes(stego_jpg, tmp_path, capsys):
    path, bits = stego_jpg
    out = tmp_path / "plain.jpg"
    assert main([str(path), "-o", str(out), "--jpeg-lossless"]) == 0
    assert "jpeg coefficients" not in capsys.readouterr().out
    assert E.read_back(out.read_bytes(), "jsteg", bits) == 0.0


def test_jpeg_canonical_flag_is_lossless_and_says_what_it_leaves(stego_jpg, tmp_path, capsys):
    path, bits = stego_jpg
    out = tmp_path / "canon.jpg"
    assert main([str(path), "-o", str(out), "--jpeg-canonical"]) == 0
    text = capsys.readouterr().out
    assert "canonicalised" in text and "survives" in text
    assert np.array_equal(pixels(out.read_bytes()), pixels(path.read_bytes()))
    assert E.read_back(out.read_bytes(), "jsteg", bits) == 0.0


def test_json_output_carries_the_dct_report(jpg, tmp_path, capsys):
    assert main([str(jpg), "-o", str(tmp_path / "o.jpg"), "--scrub-dct", "--json"]) == 0
    record = json.loads(capsys.readouterr().out)["files"][0]
    assert record["dct"]["state"] == "scrubbed" and record["dct"]["psnr_db"] > 25
    assert record["pixels_exact"] is False
    assert main([str(jpg), "-o", str(tmp_path / "p.jpg"), "--jpeg-canonical", "--json"]) == 0
    record = json.loads(capsys.readouterr().out)["files"][0]
    assert record["dct"]["state"] == "canonical" and record["pixels_exact"] is True
    assert main([str(jpg), "-o", str(tmp_path / "q.jpg"), "--json"]) == 0
    assert "dct" not in json.loads(capsys.readouterr().out)["files"][0]


def test_min_psnr_flag_refuses_and_writes_nothing(jpg, tmp_path, capsys):
    out = tmp_path / "o.jpg"
    assert main([str(jpg), "-o", str(out), "--scrub-dct", "--dct-min-psnr", "90"]) == 1
    assert "dB" in capsys.readouterr().err and not out.exists()
    assert main([str(jpg), "-o", str(out), "--scrub-dct", "--dct-min-psnr", "0"]) == 0 and out.exists()


@pytest.mark.parametrize(
    "extra",
    [["--scrub-prnu"], ["--format", "png"], ["--format", "webp"]],
)
def test_incompatible_options_fail_cleanly(jpg, tmp_path, capsys, extra):
    out = tmp_path / ("o.png" if "png" in extra else "o.jpg")
    code = main([str(jpg), "-o", str(out), "--scrub-dct", *extra]) if "webp" not in extra else None
    if code is None:
        code = main([str(jpg), "--scrub-dct", *extra])
    assert code in (1, 2)
    assert not out.exists()


def test_png_input_is_refused(tmp_path, fx, capsys):
    png = tmp_path / "a.png"
    png.write_bytes(fx["png_everything.png"])
    assert main([str(png), "--scrub-dct"]) == 1
    assert "JPEG" in capsys.readouterr().err


def test_directory_run_scrubs_jpegs_and_reports_the_rest(tmp_path, fx, capsys):
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "a.jpg").write_bytes(make(80, 60, seed=1, quality=85))
    (folder / "b.jpg").write_bytes(make(90, 70, seed=2, quality=85))
    (folder / "c.png").write_bytes(fx["png_everything.png"])
    out = tmp_path / "out"
    code = main([str(folder), "-r", "-o", str(out), "--scrub-dct", "--dct-min-psnr", "0"])
    err = capsys.readouterr().err
    assert code == 1 and "c.png" in err  # the PNG fails loudly; the JPEGs are still done
    assert sorted(p.name for p in out.iterdir()) == ["a.jpg", "b.jpg"]


def test_in_place_and_stdin_stdout(jpg, tmp_path, capsys):
    before = jpg.read_bytes()
    assert main([str(jpg), "-i", "--scrub-dct", "--dct-min-psnr", "0"]) == 0
    assert jpg.read_bytes() != before and dct.read(jpg.read_bytes())
    import subprocess

    done = subprocess.run(
        [sys.executable, "-m", "imgstrip", "-", "--jpeg-canonical", "-o", "-"], input=before, capture_output=True
    )
    assert done.returncode == 0 and np.array_equal(pixels(done.stdout), pixels(before))


def test_dry_run_writes_nothing(jpg, tmp_path, capsys):
    assert main([str(jpg), "-n", "--scrub-dct", "--dct-min-psnr", "0"]) == 0
    assert not jpg.with_name("photo.clean.jpg").exists()
    assert "would clean" in capsys.readouterr().out


def test_help_documents_both_switches_and_the_default_is_off(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    text = capsys.readouterr().out
    assert "--scrub-dct" in text and "--jpeg-canonical" in text and "--dct-min-psnr" in text
    from imgstrip.cli import build_parser

    args = build_parser().parse_args(["x.jpg"])
    assert args.scrub_dct is False and args.jpeg_canonical is False and args.dct_min_psnr == 25.0


# -- web API ---------------------------------------------------------------------------------------------------


def test_web_options_are_validated_and_default_to_off():
    assert web.parse_options({}).scrub_dct == "off"
    assert web.parse_options({"scrub_dct": True}).scrub_dct == "full"
    assert web.parse_options({"jpeg_canonical": True}).scrub_dct == "canonical"
    assert web.parse_options({"scrub_dct": True, "jpeg_canonical": True}).scrub_dct == "full"
    with pytest.raises(web.ApiError):
        web.parse_options({"scrub_dct": "yes"})
    with pytest.raises(web.ApiError):
        web.parse_options({"jpeg_canonical": 1})


def test_web_clean_reports_the_dct_scrub(app, stego_jpg):  # noqa: F811
    path, bits = stego_jpg
    data = path.read_bytes()
    plain = app.clean(data, options={})
    assert plain["dct"] is None
    scrubbed = app.clean(data, options={"scrub_dct": True})
    assert scrubbed["dct"]["state"] == "scrubbed" and scrubbed["lossy"] is True
    assert scrubbed["dct"]["psnr_db"] > 25 and "not proof" in scrubbed["dct"]["caveat"]
    res, body = app.request("GET", f"/api/result/{scrubbed['id']}")
    assert res.status == 200 and abs(E.read_back(body, "jsteg", bits) - 0.5) < 0.08
    canon = app.clean(data, options={"jpeg_canonical": True})
    assert canon["dct"]["state"] == "canonical" and canon["lossy"] is False and canon["pixels_exact"] is True


def test_web_refuses_what_it_cannot_do_with_a_message(app, fx):  # noqa: F811
    res, body = app.upload("/api/clean", fx["png_everything.png"], "a.png", {"scrub_dct": True})
    assert res.status == 422 and b"JPEG" in body
    res, body = app.upload("/api/clean", make(64, 48), "a.jpg", {"scrub_dct": True, "scrub_prnu": True})
    assert res.status == 422


def test_web_page_has_the_switches_off_by_default():
    html = (web.STATIC / "index.html").read_text() if hasattr(web, "STATIC") else None
    if html is None:
        from pathlib import Path

        html = (Path(web.__file__).parent / "static" / "index.html").read_text()
    for name in ("scrub_dct", "jpeg_canonical"):
        tag = html[html.index(f'name="{name}"') - 60 : html.index(f'name="{name}"') + 60]
        assert "checked" not in tag
    js = (__import__("pathlib").Path(web.__file__).parent / "static" / "app.js").read_text()
    assert "scrub_dct: false" in js and "jpeg_canonical: false" in js


# -- verdict text ----------------------------------------------------------------------------------------------


def test_dct_report_states():
    assert stego.dct_report(stego.DCT_OFF) is None
    assert stego.dct_report("not_applied") is None
    scrub = stego.dct_report(stego.DCT_SCRUBBED, 36.2)
    assert scrub["headline"] == "scrubbed" and "36.2" in scrub["summary"]
    assert "watermarks" in scrub["caveat"] and "not proof" in scrub["caveat"]
    canon = stego.dct_report(stego.DCT_CANONICAL)
    assert canon["headline"] == "canonicalised" and canon["psnr_db"] is None
    assert "--scrub-dct" in canon["caveat"]


def test_pixel_verdicts_are_untouched_by_the_new_state():
    verdict = stego.for_output(None, stego.SCRUB_OFF, copied=False)
    assert verdict.state == stego.NOT_SCANNED


# -- credentials (needs the optional C2PA support) -------------------------------------------------------------


def test_credential_records_the_dct_scrub(creds, signer):
    data = make(200, 150, seed=2, quality=85)
    full = Options(scrub_dct="full", dct_min_psnr=0)
    result = strip_image(data, full)
    evidence = creds.gather_evidence(data, result, full)
    assert "dct-scrubbed" in evidence.operations and "re-encoded" not in evidence.operations
    assert evidence.integrity == "altered"
    assert "dct-scrubbed" in creds.OPERATIONS
    canon = Options(scrub_dct="canonical")
    result = strip_image(data, canon)
    evidence = creds.gather_evidence(data, result, canon)
    assert evidence.integrity == "identical" and "dct-scrubbed" not in evidence.operations
    signed = creds.sign(result.data, evidence, signer.identity)
    assert creds.read_credential(signed.data).state in ("Valid", "Trusted")
