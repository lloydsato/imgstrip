"""Corrupted inputs must either produce a verified-clean file or a StripError.

Anything else (an unexpected exception, or output that is not clean) is a bug.
"""

import random

import pytest

from imgstrip import StripError, fixtures, inspect_bytes, strip_image
from imgstrip.core import Options

NAMES = list(fixtures.all_fixtures())


def mutate(data: bytes, rng: random.Random) -> bytes:
    buf = bytearray(data)
    kind = rng.choice(["flip", "truncate", "splice", "zero", "dup"])
    if kind == "flip":
        for _ in range(rng.randint(1, 8)):
            i = rng.randrange(len(buf))
            buf[i] ^= 1 << rng.randrange(8)
    elif kind == "truncate":
        del buf[rng.randrange(1, len(buf)) :]
    elif kind == "splice":
        i = rng.randrange(len(buf))
        buf[i:i] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 64)))
    elif kind == "zero":
        i = rng.randrange(len(buf))
        buf[i : i + rng.randint(1, 32)] = b"\x00" * len(buf[i : i + 32])
    else:
        i = rng.randrange(len(buf))
        j = rng.randrange(len(buf))
        buf[i:i] = buf[j : j + rng.randint(1, 128)]
    return bytes(buf)


@pytest.mark.parametrize("name", NAMES)
def test_mutations_never_crash_or_leak(fx, name):
    rng = random.Random(f"imgstrip-{name}")
    data = fx[name]
    for _ in range(40):
        corrupt = mutate(data, rng)
        inspect_bytes(corrupt)  # must never raise
        try:
            result = strip_image(corrupt, Options(max_pixels=50_000_000))
        except StripError:
            continue
        report = inspect_bytes(result.data)
        assert report.error is None
        assert all(f.label.startswith("Pixels outside the visible crop") for f in report.findings), report.findings
        # Canaries inside metadata must never survive.  (A mutation can at most
        # move canary bytes into pixel data, which the decoder would show.)
        assert fixtures.CANARY not in result.data


def test_gif_frame_outside_canvas_is_a_bomb_not_an_oom():
    """A frame descriptor far outside the logical screen makes Pillow grow the canvas."""
    import io
    import struct

    from PIL import Image

    im = Image.new("P", (8, 8), 1)
    im.putpalette([0, 0, 0, 255, 255, 255])
    buf = io.BytesIO()
    other = Image.new("P", (8, 8), 0)
    other.putpalette([0, 0, 0, 255, 255, 255])
    im.save(buf, "GIF", save_all=True, append_images=[other], duration=10)
    data = bytearray(buf.getvalue())
    second = data.index(b"\x2c", data.index(b"\x2c") + 1)
    struct.pack_into("<HH", data, second + 1, 30000, 30000)  # left/top of frame 2
    with pytest.raises(StripError, match="limit"):
        strip_image(bytes(data), Options(max_pixels=10_000_000))
