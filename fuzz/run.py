"""Coverage-guided fuzzing of imgstrip with Atheris (review item 8).

    pip install atheris                          # Python 3.11 to 3.13 on Linux or macOS
    python fuzz/run.py --list                    # the targets (fuzz/targets.py)
    python fuzz/run.py --seeds corpus            # write a seed corpus per target into corpus/<target>/
    python fuzz/run.py inspect corpus/inspect -max_total_time=60

Arguments after the target are libFuzzer's (-max_total_time, -runs, -dict, -jobs, ...).  A crash is written as
crash-<hash> in the current directory; reproduce it with `python fuzz/run.py TARGET crash-<hash>`.  With a Pillow
built under AddressSanitizer (see .github/workflows/fuzz.yml), set LD_PRELOAD to Atheris's sanitiser runtime
(`python -c "import atheris; print(atheris.path())"`/asan_with_fuzzer.so) so that memory errors in the C decoders
are reported, not only Python exceptions.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in ("--list", "--seeds"):
        ap = argparse.ArgumentParser()
        ap.add_argument("--list", action="store_true")
        ap.add_argument("--seeds", metavar="DIR")
        args = ap.parse_args()
        import targets

        if args.list:
            print("\n".join(targets.TARGETS))
            return 0
        for name in targets.TARGETS:
            directory = Path(args.seeds) / name
            directory.mkdir(parents=True, exist_ok=True)
            for data in targets.seeds(name).values():
                (directory / hashlib.sha1(data).hexdigest()).write_bytes(data)
        return 0
    if len(sys.argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    target = sys.argv.pop(1)
    import atheris

    # Pillow's format plugins are Python: instrumenting them guides the decoder target through their header parsing.
    with atheris.instrument_imports(include=["imgstrip", "PIL"]):
        import targets
    if target not in targets.TARGETS:
        print(f"unknown target {target!r}; one of: {', '.join(targets.TARGETS)}", file=sys.stderr)
        return 2
    atheris.Setup(sys.argv, targets.TARGETS[target])
    atheris.Fuzz()
    return 0


if __name__ == "__main__":
    sys.exit(main())
