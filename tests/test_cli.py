"""Command-line behaviour: naming, safety, exit codes, in-place, stdin/stdout."""

import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from imgstrip import fixtures, inspect_bytes
from imgstrip.cli import main


@pytest.fixture
def jpg(tmp_path, fx):
    p = tmp_path / "photo.jpg"
    p.write_bytes(fx["jpeg_everything.jpg"])
    return p


def run_cli(*args, stdin=None):
    return subprocess.run([sys.executable, "-m", "imgstrip", *map(str, args)], input=stdin, capture_output=True)


def test_default_output_name_and_no_clobber(jpg, capsys):
    assert main([str(jpg)]) == 0
    out = jpg.with_name("photo.clean.jpg")
    assert out.exists() and inspect_bytes(out.read_bytes()).clean
    assert main([str(jpg)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert main([str(jpg), "--force"]) == 0


def test_paths_that_cannot_be_used_are_listed_in_the_json_report(tmp_path, jpg, capsys):
    assert main(["--check", "--json", str(jpg), str(tmp_path / "missing.png"), str(tmp_path)]) == 1
    report = json.loads(capsys.readouterr().out)
    failed = {Path(f["file"]).name: f["message"] for f in report["files"] if f["status"] == "failed"}
    assert failed == {
        "missing.png": "no such file or directory",
        tmp_path.name: "is a directory (use -r to process directories)",
    }
    assert report["summary"]["failed"] == 2 and report["summary"]["checked"] == 1


def test_original_is_never_modified(jpg, fx):
    main([str(jpg)])
    assert jpg.read_bytes() == fx["jpeg_everything.jpg"]


def test_output_file_and_directory(tmp_path, jpg, fx):
    assert main([str(jpg), "-o", str(tmp_path / "x.jpg")]) == 0
    assert (tmp_path / "x.jpg").exists()
    png = tmp_path / "b.png"
    png.write_bytes(fx["png_everything.png"])
    outdir = tmp_path / "out"
    assert main([str(jpg), str(png), "-o", str(outdir)]) == 0
    assert sorted(p.name for p in outdir.iterdir()) == ["b.png", "photo.jpg"]


def test_output_file_name_picks_the_format(tmp_path, jpg):
    out = tmp_path / "x.png"
    assert main([str(jpg), "-o", str(out)]) == 0
    assert inspect_bytes(out.read_bytes()).format == "PNG"
    assert main([str(jpg), "-o", str(tmp_path / "y.JPG")]) == 0
    assert inspect_bytes((tmp_path / "y.JPG").read_bytes()).format == "JPEG"
    assert main([str(jpg), "-o", str(tmp_path / "z.tif"), "--format", "tif"]) == 0
    assert inspect_bytes((tmp_path / "z.tif").read_bytes()).format == "TIFF"
    with pytest.raises(SystemExit):
        main([str(jpg), "-o", str(tmp_path / "w.png"), "--format", "webp"])
    assert not (tmp_path / "w.png").exists()
    assert main([str(jpg), "-o", str(tmp_path / "noext")]) == 0  # no extension: the input's format
    assert inspect_bytes((tmp_path / "noext").read_bytes()).format == "JPEG"


def test_output_must_be_directory_for_many(tmp_path, jpg, fx):
    png = tmp_path / "b.png"
    png.write_bytes(fx["png_everything.png"])
    existing = tmp_path / "file.txt"
    existing.write_text("x")
    with pytest.raises(SystemExit):
        main([str(jpg), str(png), "-o", str(existing)])


def test_recursive_mirror_skips_hidden_and_non_images(tmp_path, fx):
    src = tmp_path / "src"
    (src / "a" / "b").mkdir(parents=True)
    (src / ".git").mkdir()
    (src / "a" / "b" / "deep.png").write_bytes(fx["png_everything.png"])
    (src / "top.gif").write_bytes(fx["gif_animated.gif"])
    (src / ".git" / "obj.png").write_bytes(fx["png_everything.png"])
    (src / "readme.txt").write_text("not an image")
    (src / "old.clean.png").write_bytes(fx["png_everything.png"])  # earlier output
    out = tmp_path / "out"
    assert main(["-r", str(src), "-o", str(out)]) == 0
    produced = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
    assert produced == ["a/b/deep.png", "top.gif"]


def test_directory_without_recursive_flag(tmp_path, capsys):
    assert main([str(tmp_path)]) == 1
    assert "use -r" in capsys.readouterr().err


def test_missing_file(tmp_path, capsys):
    assert main([str(tmp_path / "nope.jpg")]) == 1
    assert "no such file" in capsys.readouterr().err


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_fifo_is_rejected_not_read(tmp_path, capsys):
    fifo = tmp_path / "pipe.jpg"
    os.mkfifo(fifo)
    assert main([str(fifo)]) == 1
    assert "not a regular file" in capsys.readouterr().err


def test_explicit_non_image_fails(tmp_path):
    p = tmp_path / "x.jpg"
    p.write_text("hello")
    assert main([str(p)]) == 1


def test_in_place_keeps_mode_and_follows_symlink(tmp_path, fx):
    real = tmp_path / "real.png"
    real.write_bytes(fx["png_everything.png"])
    os.chmod(real, 0o640)
    link = tmp_path / "link.png"
    link.symlink_to(real)
    assert main(["-i", str(link)]) == 0
    assert link.is_symlink()
    assert stat.S_IMODE(real.stat().st_mode) == 0o640
    assert inspect_bytes(real.read_bytes()).clean
    assert not list(tmp_path.glob(".imgstrip-*"))  # no temp files left behind


def test_in_place_warns_about_hard_links(tmp_path, fx, capsys):
    a = tmp_path / "a.png"
    a.write_bytes(fx["png_everything.png"])
    os.link(a, tmp_path / "b.png")
    assert main(["-i", str(a)]) == 0
    assert "hard link" in capsys.readouterr().out


def test_in_place_with_misleading_extension(tmp_path, fx):
    p = tmp_path / "really_png.jpg"
    p.write_bytes(fx["png_everything.png"])
    assert main(["-i", str(p)]) == 0
    assert Image.open(p).format == "PNG"


def test_in_place_refuses_format_change(tmp_path, fx):
    p = tmp_path / "x.png"
    p.write_bytes(fx["png_everything.png"])
    assert main(["-i", "--format", "jpeg", str(p)]) == 1
    assert p.read_bytes() == fx["png_everything.png"]


def test_collision_detected(tmp_path, fx):
    (tmp_path / "a.jpg").write_bytes(fx["jpeg_everything.jpg"])
    (tmp_path / "a.jpeg").write_bytes(fx["jpeg_everything.jpg"])
    rc = main([str(tmp_path / "a.jpg"), str(tmp_path / "a.jpeg"), "--format", "png"])
    assert rc == 1
    assert (tmp_path / "a.clean.png").exists()


def test_refuses_to_overwrite_input(tmp_path, jpg, capsys):
    assert main([str(jpg), "-o", str(tmp_path)]) == 1
    assert "overwrite the input" in capsys.readouterr().err


def test_dry_run_writes_nothing(tmp_path, jpg):
    before = set(tmp_path.iterdir())
    assert main(["-n", str(jpg)]) == 0
    assert set(tmp_path.iterdir()) == before


def test_check_exit_codes(tmp_path, jpg, capsys):
    assert main(["--check", str(jpg)]) == 3
    main([str(jpg)])
    assert main(["--check", str(jpg.with_name("photo.clean.jpg"))]) == 0
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"")
    assert main(["--check", str(bad)]) == 1


def test_json_output(jpg, capsys):
    assert main(["--json", "-n", str(jpg)]) == 0
    doc = json.loads(capsys.readouterr().out)
    record = doc["files"][0]
    assert record["status"] == "cleaned" and record["output_format"] == "JPEG"
    assert any(r["label"] == "GPS location" for r in record["removed"])
    assert doc["summary"]["cleaned"] == 1


def test_random_name(tmp_path, jpg):
    out = tmp_path / "out"
    assert main(["--random-name", "-o", str(out), str(jpg)]) == 0
    (produced,) = list(out.iterdir())
    assert produced.suffix == ".jpg" and "photo" not in produced.name and len(produced.stem) == 16


def test_new_files_respect_umask(tmp_path, jpg):
    import imgstrip.cli as cli

    cli._UMASK = None  # drop the value an earlier test may have cached
    old = os.umask(0o027)
    try:
        assert main([str(jpg)]) == 0
    finally:
        os.umask(old)
    cli._UMASK = None  # reset cache for other tests
    mode = stat.S_IMODE(jpg.with_name("photo.clean.jpg").stat().st_mode)
    assert mode in (0o640, 0o644)


def test_unicode_and_spaces(tmp_path, fx):
    p = tmp_path / "фото с пробелом 📷.png"
    p.write_bytes(fx["png_everything.png"])
    assert main([str(p)]) == 0
    assert (tmp_path / "фото с пробелом 📷.clean.png").exists()


@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0, reason="root ignores directory permissions")
def test_unwritable_directory_reports_error(tmp_path, jpg):
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o500)
    try:
        assert main([str(jpg), "-o", str(ro / "x.jpg")]) == 1
    finally:
        os.chmod(ro, 0o700)


def test_parallel_matches_sequential(tmp_path, fx):
    src = tmp_path / "src"
    src.mkdir()
    for name in ("png_everything.png", "gif_animated.gif", "tiff_multipage.tif", "bmp_trailer.bmp"):
        (src / name).write_bytes(fx[name])
    assert main(["-r", str(src), "-o", str(tmp_path / "seq")]) == 0
    assert main(["-r", str(src), "-o", str(tmp_path / "par"), "-j", "3"]) == 0
    for p in (tmp_path / "seq").iterdir():
        assert p.read_bytes() == (tmp_path / "par" / p.name).read_bytes()


def test_stdin_to_stdout(fx):
    proc = run_cli("-", "--format", "png", stdin=fx["webp_lossy.webp"])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith(b"\x89PNG")
    assert inspect_bytes(proc.stdout).clean
    assert b"cleaned" in proc.stderr  # reports go to stderr when stdout carries data


def test_stdout_option(jpg):
    proc = run_cli(jpg, "-o", "-")
    assert proc.returncode == 0
    assert Image.open(io.BytesIO(proc.stdout)).format == "JPEG"
    assert fixtures.CANARY not in proc.stdout


def test_usage_errors():
    for args in (["-i", "-o", "x", "a.jpg"], ["--deep", "--c2pa-verify", "a.jpg"], ["--quality", "0", "a.jpg"], []):
        with pytest.raises(SystemExit) as exc:
            main(args)
        assert exc.value.code == 2


def test_self_test_passes(capsys):
    assert main(["--self-test"]) == 0
    assert "PASSED" in capsys.readouterr().out


def test_raw_extension_rejected(tmp_path, fx, capsys):
    p = tmp_path / "IMG_0001.NEF"
    p.write_bytes(fx["tiff_multipage.tif"])  # NEF files are TIFF containers
    assert main([str(p)]) == 1
    assert "RAW" in capsys.readouterr().err


def test_walk_reports_unsupported_images_instead_of_skipping(tmp_path, capsys):
    (tmp_path / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
    (tmp_path / "notes.md").write_text("# hello")
    assert main(["-r", str(tmp_path), "-o", str(tmp_path / "out")]) == 1
    err = capsys.readouterr().err
    assert "broken.png" in err and "notes.md" not in err
