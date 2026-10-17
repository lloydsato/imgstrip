"""imgstrip - remove every trace of metadata and hidden data from images."""

from .core import Options, Result, StripError, VerificationError, strip_image
from .inspect import Finding, Report, inspect_bytes

# The single place the version lives: pyproject.toml reads it from here.
__version__ = "1.1.1"

__all__ = [
    "Finding",
    "Options",
    "Report",
    "Result",
    "StripError",
    "VerificationError",
    "inspect_bytes",
    "strip_image",
    "__version__",
]
