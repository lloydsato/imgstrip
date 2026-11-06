"""The Pillow floor in pyproject.toml is real: a test run must never use an older Pillow than the package declares.

Pillow up to 12.3.0 returns uninitialised memory as pixels for some damaged TIFFs (GHSA-q45j-58j2-45rg, fixed in 13.0.0).
The verifier cannot see that (stage 2 compares the output with what the decoder returned), so the floor is the fix.
"""

import re
from pathlib import Path

import PIL

ROOT = Path(__file__).resolve().parent.parent


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def test_pyproject_requires_a_pillow_with_the_tiff_fix():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'"Pillow>=([0-9.]+)"', text)
    assert match, "pyproject.toml has no Pillow floor"
    assert _version(match.group(1)) >= (13, 0, 0)


def test_requires_python_is_no_lower_than_the_pillow_floor_needs():
    # Pillow 13.0.0 needs Python 3.11; a lower requires-python would make `pip install imgstrip` fail on that Python.
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'requires-python\s*=\s*">=([0-9.]+)"', text)
    assert match, "pyproject.toml has no lower bound for requires-python"
    assert _version(match.group(1)) >= (3, 11)


def test_the_installed_pillow_meets_the_declared_floor():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    floor = re.search(r'"Pillow>=([0-9.]+)"', text).group(1)
    assert _version(PIL.__version__) >= _version(floor)
