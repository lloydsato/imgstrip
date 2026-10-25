"""``imgstrip --self-test``: prove the installation works end to end.

Builds synthetic images stuffed with metadata and hidden data (every item
carrying a canary string), strips them in every supported mode and output
format, and checks that

* no canary survives anywhere in the output bytes,
* the strict container inspector finds nothing,
* the output decodes to the expected picture,
* (when installed) exiftool reports no metadata tags either, and
* readers that share no code with imgstrip or Pillow (a strict PNG reader written for this purpose, plus
  ImageMagick and FFmpeg when installed) read the output and see the same pixels.
"""

from __future__ import annotations

import concurrent.futures
import functools
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from . import fixtures, oracles, sandbox
from .core import OUTPUT_FORMATS, Options, StripError, dct_mode, heif_available, strip_image
from .inspect import inspect_bytes

# exiftool groups that only describe file structure.
EXIFTOOL_STRUCTURAL_GROUPS = {
    "ExifTool",
    "System",
    "File",
    "Composite",
    "JFIF",
    "PNG",
    "RIFF",
    "GIF",
    "QuickTime",
    "Meta",
    "BMP",
    "Adobe",
}
_STRUCTURAL_GROUP_RE = re.compile(r"^(IFD\d+|Track\d+)$")  # TIFF pages, AVIF sequence tracks
_ZERO_VALUES = {"0000:00:00 00:00:00", "0 s"}
EXIFTOOL_FORBIDDEN_TAGS = re.compile(
    r"Make|Model|Software|Artist|Copyright|Comment|Creator|Author|Description|Serial|GPS|DateTime|"
    r"ModifyDate|CreateDate|Title|Keywords|Thumbnail|Preview|Owner|Warning|Orientation|[XY]Resolution",
)
_JFIF_DEFAULTS = {"JFIF:XResolution", "JFIF:YResolution", "JFIF:ResolutionUnit"}


@functools.lru_cache(maxsize=1)
def _exiftool() -> str | None:
    return shutil.which("exiftool")


def exiftool_findings(data: bytes, suffix: str, allow_icc: bool = False) -> list[str] | None:
    """Tags exiftool reports beyond pure structure, or None if exiftool is unavailable."""
    exe = _exiftool()
    if exe is None:
        return None
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
        fh.write(data)
        path = fh.name
    try:
        proc = subprocess.run([exe, "-j", "-a", "-G1", "-u", path], capture_output=True, text=True, timeout=60)
        tags = json.loads(proc.stdout)[0]
    finally:
        os.unlink(path)
    bad = []
    for key, value in tags.items():
        if ":" not in key:
            continue
        group, name = key.split(":", 1)
        if group == "System" or key in _JFIF_DEFAULTS:
            continue
        if allow_icc and group.startswith("ICC"):
            continue  # the rebuilt profile is checked byte-for-byte by the verifier
        structural = group in EXIFTOOL_STRUCTURAL_GROUPS or _STRUCTURAL_GROUP_RE.match(group)
        if structural and str(value) in _ZERO_VALUES:
            continue  # e.g. zeroed creation dates in AVIF sequences
        if not structural or EXIFTOOL_FORBIDDEN_TAGS.search(name):
            bad.append(f"{key}={str(value)[:40]}")
    return bad


def _check(
    name: str,
    data: bytes,
    options: Options,
    label: str,
    verbose: bool,
    readers: dict[str, int] | None = None,
    log: list[str] | None = None,
) -> list[str]:
    problems: list[str] = []
    try:
        result = strip_image(data, options)
    except StripError as exc:
        return [f"{label}: {exc}"]
    out = result.data
    if fixtures.CANARY in out:
        problems.append(f"{label}: canary string survived")
    report = inspect_bytes(out)
    rebuilt_jpeg = options.jpeg_lossless or dct_mode(options) != "off"
    allowed_icc = options.icc == "sanitize" or rebuilt_jpeg
    leftovers = [f for f in report.findings if not (allowed_icc and f.label == "ICC color profile")]
    if rebuilt_jpeg:
        leftovers = [f for f in leftovers if f.label != "EXIF"]
    clap = report.info.get("clap")
    if result.output_format == "HEIF" and clap and (round(clap[0] / clap[1]), round(clap[2] / clap[3])) == result.size:
        leftovers = [f for f in leftovers if not f.label.startswith("Pixels outside the visible crop")]
    if report.error or leftovers:
        problems.append(f"{label}: inspector found {report.error or leftovers}")
    ext = {
        "JPEG": ".jpg",
        "PNG": ".png",
        "WEBP": ".webp",
        "GIF": ".gif",
        "TIFF": ".tif",
        "BMP": ".bmp",
        "AVIF": ".avif",
        "HEIF": ".heic",
    }[result.output_format]
    tool = exiftool_findings(out, ext, allow_icc=allowed_icc)
    if tool:
        tool = [t for t in tool if not (rebuilt_jpeg and "Orientation" in t)]
        if tool:
            problems.append(f"{label}: exiftool reports {tool}")
    second = oracles.differential(out, result.output_format, result.verified_exact, allow_icc=options.icc == "sanitize")
    problems += [f"{label}: {p}" for p in second.problems]
    if readers is not None:
        for reader in second.readers:
            readers[reader] = readers.get(reader, 0) + 1
    if verbose:
        status = "ok " if not problems else "BAD"
        line = f"  {status} {name:28} {label:22} -> {result.output_format:4} {result.size[0]}x{result.size[1]} x{result.frames}"
        if log is None:
            print(line)
        else:
            log.append(line)
    return problems


def _variants(name: str, formats: list[str]) -> list[tuple[str, Options]]:
    variants = [
        ("default", Options()),
        ("scrub-lsb", Options(scrub_lsb=True)),
        ("scrub-prnu", Options(scrub_prnu=True)),
        ("icc=sanitize", Options(icc="sanitize")),
    ]
    if name.endswith(".jpg") and "mpo" not in name:
        variants.append(("jpeg-lossless", Options(jpeg_lossless=True)))
        variants.append(("jpeg-canonical", Options(scrub_dct="canonical")))
        variants.append(("scrub-dct", Options(scrub_dct="full", dct_min_psnr=0)))
    for fmt in formats:
        variants.append((f"to {fmt.lower()}", Options(format=fmt)))
    return variants


def _run_variant(job: tuple[str, bytes, Options, str, bool]) -> tuple[list[str], dict[str, int], list[str]]:
    """One (fixture, variant) check, safe to run in a worker process: returns its problems, readers and log lines."""
    name, data, options, label, verbose = job
    readers: dict[str, int] = {}
    log: list[str] = []
    problems = _check(name, data, options, label, verbose, readers, log)
    return [f"{name}: {p}" for p in problems], readers, log


def _worker_count(jobs: int) -> int:
    return max(1, min(os.cpu_count() or 1, 8, jobs))


def _credential_self_test(fx: dict, verbose: bool) -> tuple[int, list[str]]:
    """With the optional C2PA support installed: sign every fixture that can carry a credential.

    Each signed file must hold exactly one credential and nothing else, still contain no canary, read back as a
    valid imgstrip credential, and be reported as invalid after a single flipped pixel byte.
    """
    from . import credentials

    if not credentials.available():
        print("  Content Credentials: optional C2PA support not installed (skipped)")
        return 0, []
    test = credentials.make_test_identity("Self Test")
    problems: list[str] = []
    runs = 0
    for name, data in fx.items():
        options = Options()
        try:
            result = strip_image(data, options)
        except StripError:
            continue
        if result.output_format not in credentials.SIGNABLE:
            continue
        runs += 1
        label = f"{name}: credentials"
        before = len(problems)
        try:
            evidence = credentials.gather_evidence(data, result, options)
            signed = credentials.sign(result.data, evidence, test.identity).data
            cred = credentials.read_credential(signed)
        except StripError as exc:
            problems.append(f"{label}: {exc}")
            continue
        if fixtures.CANARY in signed:
            problems.append(f"{label}: canary string survived")
        if not (cred.imgstrip and cred.pixels_only and cred.state == "Valid"):
            problems.append(f"{label}: the credential did not read back as valid ({cred.problems or cred.notes})")
        # Change one byte of the picture data (ISO-BMFF files keep it in 'mdat', the others near the end).
        broken = bytearray(signed)
        spot = signed.find(b"mdat") + 16 if result.output_format in ("AVIF", "HEIF") else len(signed) - 6
        broken[spot] ^= 1
        if credentials.read_credential(bytes(broken)).state != "Invalid":
            problems.append(f"{label}: a changed picture byte was not detected")
        if verbose:
            print(f"  {'ok ' if len(problems) == before else 'BAD'} {name:28} signed, verified, tamper detected")
    return runs, problems


def _sandbox_self_test() -> list[str]:
    """With the decoder sandbox on: a worker must not be able to write, create or delete a file."""
    if not sandbox.enabled():
        print("  decoder sandbox: off (--no-sandbox); inputs are decoded in this process")
        return []
    with tempfile.TemporaryDirectory(prefix="imgstrip-selftest-") as tmp:
        victim = os.path.join(tmp, "victim")
        with open(victim, "wb") as fh:
            fh.write(b"keep")
        try:
            limits = sandbox.probe_limits(tmp, victim)
        except StripError as exc:
            return [f"decoder sandbox: {exc}"]
        problems = []
        if limits["fsize"] is None:  # Windows
            print("  decoder sandbox: on (a separate process; Windows offers no further limits here)")
            return []
        if not limits["write"].startswith("no") or os.path.exists(os.path.join(tmp, "written")):
            problems.append("decoder sandbox: a worker could write a file")
        if limits["landlock"] and not (limits["create"].startswith("no") and limits["remove"].startswith("no")):
            problems.append("decoder sandbox: a worker could create or delete a file despite Landlock")
        if not os.path.exists(victim):
            problems.append("decoder sandbox: a worker deleted a file")
    extra = (
        f"Landlock ABI {limits['landlock']}: no file changes, no programs, no access to other processes"
        + (", no TCP" if limits["landlock"] >= 4 else "")
        if limits["landlock"]
        else "no Landlock on this system: a worker cannot write to files, but could still create empty files or "
        "delete them"
    )
    print(f"  decoder sandbox: on ({extra})")
    return problems


def run_self_test(verbose: bool = False) -> int:
    start = time.time()
    print("imgstrip self test: building fixtures ...")
    fx = fixtures.all_fixtures()
    exiftool = shutil.which("exiftool") is not None
    print(
        f"  {len(fx)} fixtures; exiftool cross-check {'enabled' if exiftool else 'not available'}; "
        f"HEIF {'enabled' if heif_available() else 'not available'}"
    )
    print(f"  independent readers: {', '.join(oracles.available())}")
    problems: list[str] = _sandbox_self_test()
    readers: dict[str, int] = {}
    formats = [f for f in OUTPUT_FORMATS if f != "HEIF" or heif_available()]
    jobs = [
        (name, data, opts, label, verbose)
        for name, data in fx.items()
        for label, opts in _variants(name, formats)
    ]
    runs = len(jobs)
    workers = _worker_count(len(jobs))
    # Each check strips, inspects and spawns external readers; run them side by side, reported in a fixed order.
    if workers > 1:
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=workers, initializer=sandbox.set_default, initargs=(sandbox.enabled(),)
        ) as pool:
            results = list(pool.map(_run_variant, jobs, chunksize=4))
    else:
        results = [_run_variant(job) for job in jobs]
    for found, seen, log in results:
        problems += found
        for reader, count in seen.items():
            readers[reader] = readers.get(reader, 0) + count
        for line in log:
            print(line)
    credential_runs, credential_problems = _credential_self_test(fx, verbose)
    problems += credential_problems
    elapsed = time.time() - start
    if problems:
        print(f"FAILED: {len(problems)} problem(s) in {runs + credential_runs} runs ({elapsed:.1f}s)")
        for p in problems:
            print("  - " + p)
        return 1
    extra = f" and {credential_runs} signing runs (Content Credentials)" if credential_runs else ""
    agreed = (
        "; independent readers saw the same pixels ("
        + ", ".join(f"{name} on {count} outputs" for name, count in sorted(readers.items()))
        + ")"
        if readers
        else ""
    )
    print(f"PASSED: {runs} strip runs{extra}, no metadata or canaries survived{agreed} ({elapsed:.1f}s)")
    return 0
