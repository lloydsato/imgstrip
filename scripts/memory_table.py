"""Peak memory per megapixel of cleaning one picture (review item 12), for docs/REFERENCE.md.

    python scripts/memory_table.py            # 6 and 24 MP pictures of each kind
    python scripts/memory_table.py --mp 6 48

Each case runs in a fresh interpreter and reports the main process's peak resident size above what it held before
(Linux: /proc/self/status) and the decoder worker's peak above what it started with (sandbox.last_peak), in bytes
per pixel.  The two peaks do not overlap in time: the worker has exited before the main process encodes.  The
automatic worker count of a batch (cli._worker_count) is based on the larger of the two columns.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

_CASE = r"""
import io, json, sys
import numpy as np
from PIL import Image
from imgstrip import codecs, sandbox
from imgstrip.core import Options, strip_image

kind, mp = sys.argv[1], float(sys.argv[2])
w = int((mp * 1e6 * 4 / 3) ** 0.5) // 8 * 8
h = int(w * 3 / 4) // 8 * 8
rng = np.random.default_rng(0)
small = rng.integers(0, 256, (h // 40 + 2, w // 40 + 2, 3), dtype=np.uint8)
photo = np.asarray(Image.fromarray(small).resize((w, h), Image.BICUBIC))
buf = io.BytesIO()
if kind == "JPEG":
    exif = Image.Exif()
    exif[0x0112] = 6
    Image.fromarray(photo).save(buf, "JPEG", quality=90, exif=exif)
elif kind == "PNG":
    Image.fromarray(photo).save(buf, "PNG", compress_level=1)
elif kind == "PNG 16-bit RGBA":
    rgba = np.dstack([photo, np.full((h, w), 255, np.uint8)]).astype(np.uint16) * 257 + 3
    buf.write(codecs.encode_png(rgba, 16, 6, None))
elif kind == "GIF, 4 frames":
    frames = [Image.fromarray(np.roll(photo, 7 * k, axis=1)).quantize(256) for k in range(4)]
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=100)
data = buf.getvalue()
sandbox.probe_limits("/nonexistent", "/nonexistent")  # the zygote starts before the measurement
with open("/proc/self/clear_refs", "w") as fh:
    fh.write("5")
status = lambda key: int(open("/proc/self/status").read().split(key + ":")[1].split()[0]) * 1024
base = status("VmRSS")
strip_image(data, Options())
pixels = w * h * (4 if kind.startswith("GIF") else 1)
print(json.dumps({"main": (status("VmHWM") - base) / pixels, "worker": (sandbox.last_peak() or 0) / pixels}))
"""

KINDS = ("JPEG", "PNG", "PNG 16-bit RGBA", "GIF, 4 frames")


def measure(kind: str, mp: float) -> dict:
    out = subprocess.run([sys.executable, "-c", _CASE, kind, str(mp)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mp", type=float, nargs="+", default=[6, 24])
    args = ap.parse_args()
    print(f"{'picture':<18} {'MP':>5}   {'main B/px':>9}   {'worker B/px':>11}")
    for kind in KINDS:
        for mp in args.mp:
            m = measure(kind, mp)
            print(f"{kind:<18} {mp:5.0f}   {m['main']:9.1f}   {m['worker']:11.1f}")


if __name__ == "__main__":
    main()
