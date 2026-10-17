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

# The Content Credentials tests need the optional C2PA support; without it they are not even collected
# (several import `cryptography` or `c2pa` at module level).
try:
    import c2pa  # noqa: F401
    import cryptography  # noqa: F401
except ImportError:
    collect_ignore = [
        "test_credentials.py",
        "test_credentials_identity.py",
        "test_credentials_cli.py",
        "test_credentials_web.py",
        "test_credentials_ui.py",
    ]

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


def assert_clean(data: bytes, suffix: str = ".bin", allow=(), allow_tags=()):
    """The output must be clean for both our inspector and exiftool, and canary-free.

    ``allow_tags`` names exiftool tags (as ``Group:Name``) that are expected, such as the one-tag orientation block
    the bit-exact JPEG modes keep because they cannot rotate pixels.
    """
    assert fixtures.CANARY not in data, "canary leaked into output bytes"
    report = inspect_bytes(data)
    assert report.error is None, report.error
    leftovers = [f for f in report.findings if f.label not in allow]
    assert not leftovers, leftovers
    if HAS_EXIFTOOL:
        bad = exiftool_findings(data, suffix, allow_icc="ICC color profile" in allow)
        bad = [b for b in bad or [] if not any(b.startswith(tag + "=") for tag in allow_tags)]
        assert not bad, bad


# --------------------------------------------------------------------------
# Content Credentials (skipped when the optional C2PA support is not installed)
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def creds():
    pytest.importorskip("c2pa")
    pytest.importorskip("cryptography")
    from imgstrip import credentials

    return credentials


@pytest.fixture(scope="session")
def pki(creds):
    from c2pa_support import Pki

    return Pki()


@pytest.fixture(scope="session")
def signer(creds):
    """A test identity made by imgstrip itself (a throw-away CA plus signing certificate)."""
    return creds.make_test_identity("Test Newsroom", "Jane Reporter")


@pytest.fixture(scope="session")
def plain_signer(pki):
    """An identity from a private CA that is *not* marked as a test one."""
    return pki.issue()


@pytest.fixture(scope="session")
def signed_samples(creds, signer):
    """{format: (clean bytes, signed bytes, evidence)} for every format that can carry a credential."""
    from PIL import features

    from c2pa_support import photo_bytes
    from imgstrip.core import Options, heif_available, strip_image

    source = photo_bytes("PNG")
    out = {}
    for fmt in creds.SIGNABLE:
        if fmt == "HEIF" and not heif_available():
            continue
        if fmt == "AVIF" and not features.check("avif"):
            continue
        options = Options(format=fmt)
        result = strip_image(source, options)
        evidence = creds.gather_evidence(source, result, options)
        signed = creds.sign(result.data, evidence, signer.identity)
        out[fmt] = (result.data, signed.data, evidence)
    return out
