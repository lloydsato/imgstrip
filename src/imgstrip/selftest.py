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

import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from . import fixtures, oracles
from .core import OUTPUT_FORMATS, Options, StripError, heif_available, strip_image
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


def exiftool_findings(data: bytes, suffix: str, allow_icc: bool = False) -> list[str] | None:
    """Tags exiftool reports beyond pure structure, or None if exiftool is unavailable."""
    exe = shutil.which("exiftool")
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
    name: str, data: bytes, options: Options, label: str, verbose: bool, readers: dict[str, int] | None = None
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
    allowed_icc = options.icc == "sanitize" or options.jpeg_lossless
    leftovers = [f for f in report.findings if not (allowed_icc and f.label == "ICC color profile")]
    if options.jpeg_lossless:
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
        tool = [t for t in tool if not (options.jpeg_lossless and "Orientation" in t)]
        if tool:
            problems.append(f"{label}: exiftool reports {tool}")
    second = oracles.differential(out, result.output_format, result.verified_exact, allow_icc=options.icc == "sanitize")
    problems += [f"{label}: {p}" for p in second.problems]
    if readers is not None:
        for reader in second.readers:
            readers[reader] = readers.get(reader, 0) + 1
    if verbose:
        status = "ok " if not problems else "BAD"
        print(
            f"  {status} {name:28} {label:22} -> {result.output_format:4} {result.size[0]}x{result.size[1]} x{result.frames}"
        )
    return problems


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
            print(f"  {'ok ' if not problems else 'BAD'} {name:28} signed, verified, tamper detected")
    return runs, problems


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
    problems: list[str] = []
    readers: dict[str, int] = {}
    runs = 0
    formats = [f for f in OUTPUT_FORMATS if f != "HEIF" or heif_available()]
    for name, data in fx.items():
        variants = [
            ("default", Options()),
            ("scrub-lsb", Options(scrub_lsb=True)),
            ("scrub-prnu", Options(scrub_prnu=True)),
            ("icc=sanitize", Options(icc="sanitize")),
        ]
        if name.endswith(".jpg") and "mpo" not in name:
            variants.append(("jpeg-lossless", Options(jpeg_lossless=True)))
        for fmt in formats:
            variants.append((f"to {fmt.lower()}", Options(format=fmt)))
        for label, opts in variants:
            runs += 1
            problems += [f"{name}: {p}" for p in _check(name, data, opts, label, verbose, readers)]
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
