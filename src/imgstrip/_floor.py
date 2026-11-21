"""The oldest Pillow imgstrip runs on, checked when the package is imported.

Pillow up to 12.3.0 returns memory it did not write as the pixels of some damaged TIFFs (GHSA-q45j-58j2-45rg, fixed in
13.0.0).  Neither the verifier nor the second reader can see that: both compare the output with what the decoder
returned, so a cleaned file could carry what was in memory.  The floor in ``pyproject.toml`` is the fix, but it binds
only an installer; an older Pillow already in the environment, or a package installed without its dependencies,
would otherwise be used without a word.  A test keeps the two numbers equal.
"""

from __future__ import annotations

import re

PILLOW_FLOOR = (13, 0, 0)


def parse(text: object) -> tuple[int, int, int] | None:
    """The first three numbers of a version string, or None when it does not start with a version."""
    m = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", text) if isinstance(text, str) else None
    return (int(m[1]), int(m[2]), int(m[3] or 0)) if m else None


def check() -> None:
    """Raise ``ImportError`` unless the Pillow that is installed is at least ``PILLOW_FLOOR``."""
    import PIL

    found = getattr(PIL, "__version__", None)
    version = parse(found)
    if version is None or version < PILLOW_FLOOR:
        raise ImportError(
            f"imgstrip needs Pillow {'.'.join(map(str, PILLOW_FLOOR))} or newer, and this is "
            f"{found if isinstance(found, str) else 'a Pillow of unknown version'}: older versions return memory "
            "they did not write as the pixels of some damaged TIFF files (GHSA-q45j-58j2-45rg), which no check "
            "in imgstrip can detect.  Upgrade with: pip install --upgrade 'Pillow>=13.0.0'"
        )


check()
