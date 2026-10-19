"""The version is written once (src/imgstrip/__init__.py); pyproject reads it from there."""

import re
from pathlib import Path

import imgstrip

ROOT = Path(__file__).resolve().parent.parent


def test_pyproject_reads_version_from_package():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in text
    assert re.search(r'(?m)^version\s*=\s*"\d', text.split("[tool.setuptools.dynamic]")[0]) is None
    assert 'version = {attr = "imgstrip.__version__"}' in text


def test_version_shape():
    assert re.fullmatch(r"\d+\.\d+\.\d+", imgstrip.__version__)


def test_credentials_use_package_version():
    source = (ROOT / "src" / "imgstrip" / "credentials.py").read_text(encoding="utf-8")
    assert '"version": __version__' in source
