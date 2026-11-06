"""The self test's own helpers: the exiftool cross-check must say so when exiftool cannot vouch for a file."""

import io
import shutil
import subprocess

import pytest
from PIL import Image

from imgstrip import selftest


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (9, 9, 9)).save(buf, "PNG")
    return buf.getvalue()


needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None, reason="needs exiftool")


@needs_exiftool
def test_a_plain_picture_has_no_exiftool_findings():
    assert selftest.exiftool_findings(png(), ".png") == []


@needs_exiftool
def test_a_file_exiftool_complains_about_is_a_finding():
    assert selftest.exiftool_findings(png()[:60], ".png")


def test_an_exiftool_that_prints_no_report_is_a_finding_not_a_crash(monkeypatch):
    monkeypatch.setattr(selftest, "_exiftool", lambda: "exiftool")
    monkeypatch.setattr(
        selftest.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="no luck\n")
    )
    assert selftest.exiftool_findings(png(), ".png") == ["exiftool could not read the output: no luck"]


def test_exiftool_errors_are_findings(monkeypatch):
    report = '[{"SourceFile": "x", "ExifTool:Error": "File format error"}]'
    monkeypatch.setattr(selftest, "_exiftool", lambda: "exiftool")
    monkeypatch.setattr(
        selftest.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=report, stderr="")
    )
    assert selftest.exiftool_findings(png(), ".png") == ["ExifTool:Error=File format error"]


@pytest.mark.parametrize(
    "banner, flags",
    [
        ("ffmpeg version 4.4.2-0ubuntu0.22.04.1 Copyright (c) 2000-2021", ("-vsync", "0")),
        ("ffmpeg version 5.0.3 Copyright", ("-vsync", "0")),
        ("ffmpeg version 5.1 Copyright", ("-fps_mode", "passthrough")),
        ("ffmpeg version n6.1.1 Copyright", ("-fps_mode", "passthrough")),
        ("ffmpeg version 6.1.1-3ubuntu5 Copyright", ("-fps_mode", "passthrough")),
        ("ffmpeg version N-117000-gabcdef Copyright", ("-fps_mode", "passthrough")),  # a git build: assumed new
    ],
)
def test_ffmpeg_gets_the_frame_option_its_version_knows(monkeypatch, banner, flags):
    from imgstrip import oracles

    oracles._passthrough_flags.cache_clear()
    monkeypatch.setattr(
        oracles.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=banner, stderr="")
    )
    try:
        assert oracles._passthrough_flags("ffmpeg") == flags
    finally:
        oracles._passthrough_flags.cache_clear()


def test_an_ffmpeg_that_will_not_say_its_version_gets_the_old_option(monkeypatch):
    from imgstrip import oracles

    def fail(*args, **kwargs):
        raise OSError("no such program")

    oracles._passthrough_flags.cache_clear()
    monkeypatch.setattr(oracles.subprocess, "run", fail)
    try:
        assert oracles._passthrough_flags("ffmpeg") == ("-vsync", "0")
    finally:
        oracles._passthrough_flags.cache_clear()
