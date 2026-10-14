import io
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))

from imgstrip import fixtures  # noqa: E402
from imgstrip.inspect import inspect_bytes  # noqa: E402
from imgstrip.selftest import exiftool_findings  # noqa: E402

HAS_EXIFTOOL = shutil.which("exiftool") is not None
HAS_MAGICK = shutil.which("convert") is not None


@pytest.fixture(scope="session")
def fx():
    return fixtures.all_fixtures()


def decode(data: bytes, frame: int = 0) -> Image.Image:
    im = Image.open(io.BytesIO(data))
    if frame:
        im.seek(frame)
    im.load()
    return im


def rgba(data: bytes, frame: int = 0) -> np.ndarray:
    return np.asarray(decode(data, frame).convert("RGBA"))


def visible(arr: np.ndarray) -> np.ndarray:
    """RGBA with colour of fully transparent pixels zeroed (what viewers show)."""
    out = arr.copy()
    out[out[..., 3] == 0] = 0
    return out


def assert_clean(data: bytes, suffix: str = ".bin", allow=()):
    """The output must be clean for both our inspector and exiftool, and canary-free."""
    assert fixtures.CANARY not in data, "canary leaked into output bytes"
    report = inspect_bytes(data)
    assert report.error is None, report.error
    leftovers = [f for f in report.findings if f.label not in allow]
    assert not leftovers, leftovers
    if HAS_EXIFTOOL:
        bad = exiftool_findings(data, suffix, allow_icc="ICC color profile" in allow)
        assert not bad, bad
