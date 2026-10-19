"""Rough before/after timings and peak memory for imgstrip, plus a hash of every output.

    python scripts/bench.py            # prints timings, peak memory and one SHA-256 over all outputs
    python scripts/bench.py --files 24

Compare the printed digests across commits: they must not change.  "peak" is the largest resident size of the
main imgstrip process (Linux only); in the parallel batch the work happens in workers, which it does not count.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image


def picture(w: int, h: int, seed: int) -> Image.Image:
    noise = np.random.default_rng(seed).integers(0, 256, (h, w, 3), dtype=np.uint8)
    ramp = np.linspace(0, 255, w, dtype=np.uint8)[None, :, None]
    return Image.fromarray(((noise.astype(np.uint16) * 2 + ramp * 6) // 8).astype(np.uint8))


def make_corpus(root: Path, count: int) -> None:
    rng = random.Random(1234)
    for i in range(count):
        w, h = 900 + rng.randrange(300), 700 + rng.randrange(300)
        im = picture(w, h, i)
        kind = ("jpg", "png", "webp")[i % 3]
        buf = io.BytesIO()
        exif = Image.Exif()
        exif[0x010F] = "BenchCam"
        im.save(buf, {"jpg": "JPEG", "png": "PNG", "webp": "WEBP"}[kind], exif=exif)
        (root / f"img{i:03d}.{kind}").write_bytes(buf.getvalue())


def make_large(root: Path) -> dict[str, Path]:
    """One 12 MP photo-sized picture as a rotated JPEG and as a PNG."""
    im = picture(4000, 3000, 99)
    exif = Image.Exif()
    exif[0x010F] = "BenchCam"
    exif[0x0112] = 6
    paths = {"jpg": root / "large.jpg", "png": root / "large.png"}
    im.save(paths["jpg"], "JPEG", quality=90, exif=exif)
    im.save(paths["png"], "PNG", compress_level=1)
    return paths


# Runs imgstrip and reports the process's own peak resident size on exit.  (The rusage a parent gets for a child
# also counts the parent's own memory at the fork, so the child measures itself.)
_MEASURED = """
import atexit, runpy, sys
def peak():
    try:
        with open("/proc/self/status") as fh:
            kb = next(line.split()[1] for line in fh if line.startswith("VmHWM:"))
    except OSError:
        return
    sys.stderr.write(f"\\nBENCH_PEAK_KB {kb}\\n")
atexit.register(peak)
sys.argv[0] = "imgstrip"
runpy.run_module("imgstrip", run_name="__main__")
"""


def run(*args: str) -> tuple[float, int | None]:
    """Seconds and peak resident memory (MB, of the main process; Linux only) of one imgstrip run."""
    start = time.perf_counter()
    proc = subprocess.run([sys.executable, "-c", _MEASURED, *args], capture_output=True, text=True)
    elapsed = time.perf_counter() - start
    if proc.returncode not in (0, 3):  # 3: --check found something, which is expected here
        raise subprocess.CalledProcessError(proc.returncode, proc.args, stderr=proc.stderr)
    peak = [line.split()[1] for line in proc.stderr.splitlines() if line.startswith("BENCH_PEAK_KB ")]
    return elapsed, int(peak[-1]) // 1024 if peak else None


def digest(directory: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(directory.glob("*.clean.*")):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def best(runs: list[tuple[float, int | None]]) -> tuple[float, str]:
    seconds = min(r[0] for r in runs)
    peaks = [r[1] for r in runs if r[1] is not None]
    return seconds, f"{min(peaks):5d} MB" if peaks else "    n/a"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", type=int, default=18)
    ap.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args()

    startup = min(run("--version")[0] for _ in range(args.repeat * 2))
    print(f"startup (--version)          {startup * 1000:7.0f} ms")

    print(f"{'':28} {'time':>7}   {'peak':>8}   outputs")
    for label, extra in (("batch default", []), ("batch -j1", ["-j", "1"]), ("batch --optimize -j1", ["--optimize", "-j", "1"])):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_corpus(root, args.files)
            seconds, peak = best([_timed_batch(root, extra) for _ in range(args.repeat)])
            print(f"{label:<28} {seconds:7.2f} s {peak}   {digest(root)}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        large = make_large(root)
        for label, extra, output in (
            ("12 MP JPEG", [str(large["jpg"])], "jpeg.clean.jpg"),
            ("12 MP PNG", [str(large["png"])], "png.clean.png"),
            ("12 MP --jpeg-canonical", ["--jpeg-canonical", str(large["jpg"])], "canonical.clean.jpg"),
            ("12 MP PNG --check --deep", ["--check", "--deep", str(large["png"])], None),
        ):
            runs = []
            for _ in range(args.repeat):
                if output is None:
                    runs.append(run(*extra))
                    continue
                (root / output).unlink(missing_ok=True)
                runs.append(run("--quiet", "-o", str(root / output), *extra))
            seconds, peak = best(runs)
            outputs = hashlib.sha256((root / output).read_bytes()).hexdigest()[:16] if output else ""
            print(f"{label:<28} {seconds:7.2f} s {peak}   {outputs}")


def _timed_batch(root: Path, extra: list[str]) -> tuple[float, int | None]:
    for old in root.glob("*.clean.*"):
        old.unlink()
    return run("-r", str(root), "--quiet", *extra)


if __name__ == "__main__":
    main()
