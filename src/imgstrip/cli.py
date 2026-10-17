"""Command-line interface."""

from __future__ import annotations

import argparse
import concurrent.futures
import functools
import getpass
import json
import os
import secrets
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, stego
from .core import EXTENSIONS, OUTPUT_FORMATS, Options, StripError, strip_image
from .inspect import inspect_bytes

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_FOUND = 0, 1, 2, 3
STDIO = "-"
RAW_EXTENSIONS = {
    ".cr2",
    ".cr3",
    ".crw",
    ".nef",
    ".nrw",
    ".arw",
    ".srf",
    ".sr2",
    ".dng",
    ".orf",
    ".rw2",
    ".raf",
    ".pef",
    ".ptx",
    ".srw",
    ".x3f",
    ".3fr",
    ".fff",
    ".iiq",
    ".mos",
    ".mrw",
    ".erf",
    ".kdc",
    ".dcr",
    ".rwl",
}
IMAGE_EXTENSIONS = (
    RAW_EXTENSIONS
    | {e for exts in EXTENSIONS.values() for e in exts}
    | {
        ".psd",
        ".psb",
        ".ico",
        ".cur",
        ".icns",
        ".tga",
        ".qoi",
        ".jp2",
        ".j2k",
        ".jpx",
        ".pcx",
        ".sgi",
        ".rgb",
        ".ppm",
        ".pgm",
        ".pbm",
        ".pnm",
        ".dds",
        ".jxl",
        ".svg",
        ".eps",
        ".xcf",
        ".exr",
        ".hdr",
    }
)
_IMAGE_MAGIC = (
    b"8BPS",
    b"\x00\x00\x01\x00",
    b"\x00\x00\x02\x00",
    b"qoif",
    b"\x00\x00\x00\x0cjP  ",
    b"\xff\x4f\xff\x51",
    b"\xff\x0a",
    b"\x00\x00\x00\x0cJXL ",
    b"icns",
    b"DDS ",
    b"P1",
    b"P2",
    b"P3",
    b"P4",
    b"P5",
    b"P6",
    b"FUJIFILM",
)


def looks_like_image(path: str, data: bytes) -> bool:
    """Used to decide whether a failure during a directory walk is worth reporting."""
    from .inspect import sniff_format

    if Path(path).suffix.lower() in IMAGE_EXTENSIONS or sniff_format(data):
        return True
    return any(data.startswith(m) for m in _IMAGE_MAGIC) or data[4:8] == b"ftyp"


@dataclass
class Task:
    source: str  # path or "-"
    root: Path | None  # directory the file was discovered in (for mirroring)
    from_walk: bool = False


@dataclass
class Outcome:
    task: Task
    status: str  # cleaned | checked | skipped | failed
    destination: str | None = None
    message: str = ""
    payload: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="imgstrip",
        description=(
            "Remove ALL metadata and hidden data from images by rebuilding them from their pixels. "
            "Every output is re-parsed and re-decoded to prove it is clean before it is written."
        ),
        epilog=(
            "examples:\n"
            "  imgstrip photo.jpg                 -> photo.clean.jpg\n"
            "  imgstrip -r ~/Pictures -o ~/clean  mirror a folder tree\n"
            "  imgstrip -i *.png                  replace files in place (atomically)\n"
            "  imgstrip --check --deep img.png    report metadata and run steganalysis\n"
            "  imgstrip --scrub-lsb img.png       also destroy LSB steganography\n"
            "  imgstrip --scrub-dct photo.jpg     also destroy JPEG DCT steganography (JSteg, F5, ...)\n"
            "  cat a.heic | imgstrip - --format jpeg > a.jpg\n"
            "\nexit status: 0 success/clean, 1 errors, 2 usage, 3 --check found data"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("paths", nargs="*", metavar="PATH", help="image files or directories ('-' reads stdin)")
    out = p.add_argument_group("output")
    out.add_argument("-o", "--output", metavar="PATH", help="output file (single input) or directory ('-' = stdout)")
    out.add_argument("-i", "--in-place", action="store_true", help="replace the original files (atomic rename)")
    out.add_argument("-s", "--suffix", default=".clean", help="suffix added before the extension (default: .clean)")
    out.add_argument(
        "--random-name", action="store_true", help="give outputs random file names (original names can leak dates)"
    )
    out.add_argument("-f", "--force", action="store_true", help="overwrite existing output files")
    out.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="process directories recursively (hidden directories are skipped)",
    )
    proc = p.add_argument_group("processing")
    proc.add_argument(
        "--format",
        type=str.lower,
        choices=sorted({f.lower() for f in OUTPUT_FORMATS} | {"jpg", "tif", "heic"}),
        help="output format (default: same as input)",
    )
    proc.add_argument(
        "-q",
        "--quality",
        type=int,
        metavar="1-100",
        help="quality for lossy formats (default: JPEG/WebP 95, AVIF/HEIF 90)",
    )
    proc.add_argument(
        "--jpeg-lossless",
        action="store_true",
        help="strip JPEGs without re-encoding (bit-exact image data; keeps DCT-domain content)",
    )
    proc.add_argument(
        "--icc",
        choices=("convert", "sanitize", "drop"),
        default="convert",
        help="colour profiles: convert pixels to sRGB (default), embed a rebuilt anonymous profile, or drop",
    )
    proc.add_argument("--no-auto-orient", action="store_true", help="do not bake EXIF orientation into the pixels")
    proc.add_argument(
        "--scrub-lsb",
        action="store_true",
        help="randomise least-significant bits to destroy LSB steganography (changes pixels by at most 1)",
    )
    proc.add_argument(
        "--scrub-dct",
        action="store_true",
        help="JPEG only: destroy steganography hidden in the DCT coefficients (JSteg, F5, steghide, J-UNIWARD, ...) "
        "by randomising coefficient parities and removing the smallest coefficients, with no pixel re-encode. Costs picture "
        "quality (the report gives the PSNR); off unless given",
    )
    proc.add_argument(
        "--jpeg-canonical",
        action="store_true",
        help="JPEG only: rebuild the compressed data from its DCT coefficients in one canonical form (removes "
        "data hidden in table layouts, padding, markers). Pixels are unchanged; coefficients are kept, so DCT "
        "steganography survives (use --scrub-dct for that)",
    )
    proc.add_argument(
        "--dct-min-psnr",
        type=float,
        default=25.0,
        metavar="DB",
        help="with --scrub-dct, refuse a picture the scrub would change by more than this (default: 25; 0 = never)",
    )
    proc.add_argument(
        "--scrub-prnu",
        action="store_true",
        help="disrupt the camera sensor fingerprint (PRNU) with a smooth random warp "
        "(moves pixels by a few pixels; cannot be combined with --jpeg-lossless)",
    )
    proc.add_argument("--optimize", action="store_true", help="try extra compression strategies for PNG output")
    proc.add_argument(
        "--max-pixels",
        type=int,
        default=300_000_000,
        metavar="N",
        help="refuse images larger than N pixels (default: 300M)",
    )
    cred = p.add_argument_group(
        "content credentials (C2PA, optional)",
        "Sign each cleaned image with a Content Credential that says it was sanitised by you, and how "
        "the pixels relate to the file you received. Off unless --c2pa is given. Needs: pip install 'imgstrip[c2pa]'",
    )
    cred.add_argument("--c2pa", action="store_true", help="sign each cleaned image with a Content Credential")
    cred.add_argument(
        "--c2pa-cert",
        metavar="FILE",
        help="your signing certificate chain (PEM; may also hold the key) or a PKCS#12 (.p12/.pfx) bundle",
    )
    cred.add_argument("--c2pa-key", metavar="FILE", help="the private key (PEM), if it is not in the certificate file")
    cred.add_argument(
        "--c2pa-passphrase-file",
        metavar="FILE",
        help="read the key passphrase from this file (otherwise IMGSTRIP_C2PA_PASSPHRASE, or you are asked)",
    )
    cred.add_argument("--c2pa-passphrase", metavar="TEXT", help=argparse.SUPPRESS)  # only to refuse it, see below
    cred.add_argument(
        "--c2pa-date",
        choices=("day", "minute"),
        default="day",
        help="how exactly the signing time is recorded (default: day; less is safer)",
    )
    cred.add_argument(
        "--c2pa-detail",
        choices=("minimal", "full"),
        default="minimal",
        help="full also lists what was done to the pixels and the measured fidelity",
    )
    cred.add_argument(
        "--c2pa-commit-source",
        action="store_true",
        help="add a salted commitment to the original's pixels so you can prove later which original this came "
        "from (requires --c2pa-receipt)",
    )
    cred.add_argument(
        "--c2pa-receipt",
        metavar="FILE",
        help="PRIVATE file that records the secrets needed to open each commitment (created 0600, appended to)",
    )
    cred.add_argument(
        "--c2pa-trust-anchors",
        metavar="FILE",
        help="CA certificate(s) (PEM) to treat as trusted, e.g. the C2PA Trust List or your own root",
    )
    cred.add_argument(
        "--c2pa-test-identity",
        metavar="DIR",
        help="create a throw-away TEST certificate and key in DIR and exit (credentials from it show as Unverified)",
    )
    cred.add_argument(
        "--c2pa-test-name", metavar="NAME", default="Test Newsroom", help="organisation for the test certificate"
    )
    cred.add_argument(
        "--c2pa-verify",
        action="store_true",
        help="report and verify each file's Content Credential (changes nothing); with --c2pa-original and "
        "--c2pa-receipt also audits it against the original",
    )
    cred.add_argument("--c2pa-original", metavar="FILE", help="with --c2pa-verify: the original file, for an audit")
    mode = p.add_argument_group("modes")
    mode.add_argument("--check", action="store_true", help="only report metadata/hidden data; write nothing")
    mode.add_argument(
        "--deep",
        action="store_true",
        help="also decode the pixels (steganalysis, hidden colour, palettes), with --check or when cleaning; "
        "'not detected' is never proof that nothing is hidden",
    )
    mode.add_argument("-n", "--dry-run", action="store_true", help="process everything in memory but write nothing")
    mode.add_argument("--json", action="store_true", help="machine-readable JSON output")
    mode.add_argument("-v", "--verbose", action="store_true", help="list every removed item and action")
    mode.add_argument("--quiet", action="store_true", help="only print errors")
    mode.add_argument("-j", "--jobs", type=int, default=1, metavar="N", help="process N files in parallel")
    mode.add_argument("--self-test", action="store_true", help="run the built-in end-to-end self test")
    mode.add_argument("--version", action="version", version=f"imgstrip {__version__}")
    return p


def options_from_args(args) -> Options:
    return Options(
        format=args.format,
        quality=args.quality,
        jpeg_lossless=args.jpeg_lossless,
        icc=args.icc,
        auto_orient=not args.no_auto_orient,
        scrub_lsb=args.scrub_lsb,
        scrub_dct="full" if args.scrub_dct else "canonical" if args.jpeg_canonical else "off",
        dct_min_psnr=args.dct_min_psnr,
        scrub_prnu=args.scrub_prnu,
        optimize=args.optimize,
        max_pixels=args.max_pixels,
    )


# --------------------------------------------------------------------------
# File discovery and output planning
# --------------------------------------------------------------------------


def discover(paths: list[str], recursive: bool, suffix: str, errors: list[str]) -> list[Task]:
    tasks: list[Task] = []
    seen: set[str] = set()
    for raw in paths:
        if raw == STDIO:
            tasks.append(Task(STDIO, None))
            continue
        path = Path(raw)
        if path.is_dir():
            if not recursive:
                errors.append(f"{raw}: is a directory (use -r to process directories)")
                continue
            for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
                # Skip hidden directories (.git, .thumbnails, ...) and keep a stable order.
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for name in sorted(filenames):
                    full = Path(dirpath) / name
                    if full.is_symlink() or not full.is_file():
                        continue
                    stem = Path(name).stem
                    if suffix and stem.endswith(suffix):
                        continue  # our own output from an earlier run
                    key = os.path.realpath(full)
                    if key not in seen:
                        seen.add(key)
                        tasks.append(Task(str(full), path, from_walk=True))
        elif path.exists():
            if not path.is_file():
                errors.append(f"{raw}: not a regular file")
                continue
            key = os.path.realpath(path)
            if key not in seen:
                seen.add(key)
                tasks.append(Task(str(path), None))
        else:
            errors.append(f"{raw}: no such file or directory")
    return tasks


def output_extension(source: str, fmt: str) -> str:
    ext = Path(source).suffix if source != STDIO else ""
    if ext.lower() in EXTENSIONS[fmt]:
        return ext
    return EXTENSIONS[fmt][0]


def names_output_file(args, single: bool) -> bool:
    """True when ``-o`` names the one output file (rather than a directory to write into)."""
    if not args.output or args.output == STDIO or not single or args.random_name:
        return False
    return not Path(args.output).is_dir() and not args.output.endswith(os.sep)


def format_from_name(path: str) -> str | None:
    """The output format a file name's extension asks for, if it names one."""
    ext = Path(path).suffix.lower()
    return next((fmt for fmt, exts in EXTENSIONS.items() if ext in exts), None)


def plan_destination(task: Task, fmt: str, args, single: bool, input_format: str | None = None) -> str:
    if args.output == STDIO or (task.source == STDIO and not args.output):
        return STDIO
    ext = output_extension(task.source, fmt)
    if args.in_place:
        src = Path(os.path.realpath(task.source))
        same_format = input_format == fmt
        if not same_format and Path(task.source).suffix.lower() not in EXTENSIONS.get(fmt, ()):
            raise StripError(f"cannot replace in place: output would be {fmt}; use --output")
        return str(src)
    stem = Path(task.source).stem if task.source != STDIO else "stdin"
    if args.output:
        out = Path(args.output)
        if names_output_file(args, single):
            return str(out)
        if args.random_name:
            return str(out / (secrets.token_hex(8) + ext))
        if task.root is not None:
            rel = Path(task.source).relative_to(task.root)
            return str(out / rel.with_name(rel.stem + ext))
        return str(out / (stem + ext))
    if task.source == STDIO:
        return STDIO
    name = (secrets.token_hex(8) if args.random_name else stem + args.suffix) + ext
    return str(Path(task.source).with_name(name))


_UMASK = None


def _default_mode() -> int:
    global _UMASK
    if _UMASK is None:
        _UMASK = os.umask(0)
        os.umask(_UMASK)
    return 0o666 & ~_UMASK


def atomic_write(dest: str, data: bytes, mode: int | None) -> None:
    """Write via a temporary file in the same directory, fsync, then rename."""
    directory = os.path.dirname(os.path.abspath(dest))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".imgstrip-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode if mode is not None else _default_mode())
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Per-file work (runs in worker processes when -j > 1)
# --------------------------------------------------------------------------


def _read(source: str) -> bytes:
    if source == STDIO:
        return sys.stdin.buffer.read()
    with open(source, "rb") as fh:
        return fh.read()


def _findings_payload(findings) -> list[dict]:
    return [{"category": f.category, "label": f.label, "detail": f.detail} for f in findings]


def pixel_scan(data: bytes):
    """Scan the pixels; a failure becomes a 'not scanned' verdict instead of an error."""
    from .analysis import PixelScan, scan_pixels

    try:
        return scan_pixels(data)
    except StripError as exc:
        return PixelScan([], stego.not_scanned(f"pixel analysis failed: {exc}"))
    except MemoryError:
        return PixelScan([], stego.not_scanned("pixel analysis ran out of memory"))


def check_one(task: Task, deep: bool) -> Outcome:
    try:
        data = _read(task.source)
    except OSError as exc:
        return Outcome(task, "failed", message=exc.strerror or str(exc))
    if not data:
        return Outcome(task, "failed", message="file is empty")
    report = inspect_bytes(data)
    findings = list(report.findings)
    notes = []
    if report.format is None:
        if task.from_walk:
            return Outcome(task, "skipped", message="not a recognised image container")
        notes.append("container format not inspected (only pixel analysis available)")
    verdict = stego.not_scanned("the pixels were not scanned (add --deep to test them)")
    if deep:
        scan = pixel_scan(data)
        findings += scan.findings
        verdict = scan.verdict
    payload = {
        "format": report.format,
        "size": report.info.get("size"),
        "clean": not findings and report.error is None,
        "error": report.error,
        "findings": _findings_payload(findings),
        "steganography": verdict.as_dict(),
        "notes": notes,
    }
    if any("C2PA content credentials" in f.label for f in findings):
        try:
            from . import credentials

            if credentials.available():
                payload["credential"] = credentials.read_credential(data).as_dict()
        except Exception as exc:  # noqa: BLE001 - the extra verdict is informational
            notes.append(f"credential could not be read: {exc}")
    return Outcome(task, "checked", payload=payload)


def strip_one(task: Task, options: Options, args, single: bool) -> Outcome:
    try:
        data = _read(task.source)
    except OSError as exc:
        return Outcome(task, "failed", message=exc.strerror or str(exc))
    if task.source != STDIO and Path(task.source).suffix.lower() in RAW_EXTENSIONS:
        return Outcome(
            task, "failed", message="camera RAW files are not supported; export the photo to TIFF, PNG or JPEG first"
        )
    try:
        result = strip_image(data, options)
    except StripError as exc:
        if task.from_walk and not looks_like_image(task.source, data):
            return Outcome(task, "skipped", message="not an image")
        return Outcome(task, "failed", message=str(exc))
    except MemoryError:
        return Outcome(task, "failed", message="out of memory")
    scan = pixel_scan(data) if args.deep else None
    verdict = stego.for_output(scan.verdict if scan else None, result.lsb_scrub, copied=result.verified_exact)
    evidence = None
    if args.c2pa:
        from . import credentials

        reason = credentials.unsignable_reason(result.output_format)
        if reason:
            return Outcome(task, "failed", message=reason)
        try:
            evidence = credentials.gather_evidence(data, result, options)
        except StripError as exc:
            return Outcome(task, "failed", message=f"could not prepare the credential: {exc}")
    try:
        dest = plan_destination(task, result.output_format, args, single, result.input_format)
    except StripError as exc:
        return Outcome(task, "failed", message=str(exc))
    payload = {
        "input_format": result.input_format,
        "output_format": result.output_format,
        "size": list(result.size),
        "frames": result.frames,
        "pixels_exact": result.pixels_exact,
        "bytes_in": len(data),
        "bytes_out": len(result.data),
        "removed": _findings_payload(result.removed),
        "actions": result.actions,
        "warnings": list(result.warnings),
        "steganography": verdict.as_dict(),
    }
    dct_state = stego.dct_report(result.dct_scrub, result.dct_psnr)
    if dct_state:
        payload["dct"] = dct_state
    if verdict.payload_kept:
        payload["warnings"].append(
            "a pixel payload was detected and is still in the cleaned file: run again with --scrub-lsb to destroy it"
        )
    outcome = Outcome(task, "cleaned", destination=dest, payload=payload)
    outcome.payload["_data"] = result.data
    if evidence is not None:
        outcome.payload["_evidence"] = evidence
    return outcome


def _write_outcome(outcome: Outcome, args, planned: set[str]) -> None:
    data = outcome.payload.pop("_data", None)
    if outcome.status != "cleaned" or data is None:
        return
    dest = outcome.destination
    task = outcome.task
    if dest == STDIO:
        if args.dry_run:
            return
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return
    key = os.path.realpath(dest)
    if key in planned:
        outcome.status, outcome.message = "failed", f"another input already maps to {dest}"
        return
    planned.add(key)
    source_real = os.path.realpath(task.source) if task.source != STDIO else None
    mode = None
    if args.in_place:
        st = os.stat(task.source)
        mode = stat.S_IMODE(st.st_mode)
        if st.st_nlink > 1:
            outcome.payload["warnings"].append(
                f"{st.st_nlink - 1} other hard link(s) still point to the original, uncleaned data"
            )
    else:
        if source_real is not None and os.path.exists(dest) and os.path.samefile(dest, task.source):
            outcome.status, outcome.message = "failed", "output would overwrite the input (use --in-place)"
            return
        if os.path.exists(dest) and not args.force:
            outcome.status, outcome.message = "failed", f"{dest} already exists (use --force)"
            return
    if args.dry_run:
        return
    try:
        atomic_write(dest, data, mode)
    except OSError as exc:
        outcome.status, outcome.message = "failed", f"cannot write {dest}: {exc.strerror or exc}"


def _work(task: Task, options: Options, args, single: bool, anchors: bytes | None = None) -> Outcome:
    if args.c2pa_verify:
        return verify_one(task, args, anchors, None)
    if args.check:
        return check_one(task, args.deep)
    return strip_one(task, options, args, single)


# --------------------------------------------------------------------------
# Content Credentials (optional; nothing below runs without --c2pa / --c2pa-verify)
# --------------------------------------------------------------------------


class _Usage(Exception):
    """A problem with the command line itself (reported as a usage error)."""


def _read_file(path: str, what: str) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read(16 << 20)
    except OSError as exc:
        raise _Usage(f"cannot read {what} {path}: {exc.strerror or exc}") from None


def _tty_prompt(prompt: str) -> str | None:
    if not sys.stdin.isatty():
        return None
    try:
        return getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        return None


def _passphrase(args) -> bytes | None:
    """The key passphrase from a file or the environment, never from the command line."""
    if args.c2pa_passphrase_file:
        text = _read_file(args.c2pa_passphrase_file, "passphrase file").decode("utf-8", "replace")
        return text.rstrip("\r\n").encode("utf-8")
    env = os.environ.get("IMGSTRIP_C2PA_PASSPHRASE")
    return env.encode("utf-8") if env else None


def _warn_key_permissions(path: str) -> None:
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return
    if os.name == "posix" and mode & 0o077:
        print(f"warning: {path} can be read by other users (mode {mode:04o}); run: chmod 600 {path}", file=sys.stderr)


def load_signing_identity(args):
    from . import credentials

    if not args.c2pa_cert:
        raise _Usage(
            "--c2pa needs your signing certificate: --c2pa-cert FILE (and --c2pa-key FILE if the key is separate)"
        )
    cert = _read_file(args.c2pa_cert, "certificate")
    key = _read_file(args.c2pa_key, "key") if args.c2pa_key else None
    if args.c2pa_key:
        _warn_key_permissions(args.c2pa_key)
    if b"PRIVATE KEY" in cert or b"-----BEGIN" not in cert:  # a certificate file that also holds the key, or a .p12
        _warn_key_permissions(args.c2pa_cert)
    passphrase = _passphrase(args)
    try:
        try:
            return credentials.load_identity(cert, key, passphrase)
        except credentials.PassphraseRequired:
            if passphrase is not None:
                raise
            typed = _tty_prompt("Passphrase for the signing key: ")
            if typed is None:
                raise _Usage(
                    "the key is protected: set IMGSTRIP_C2PA_PASSPHRASE or use --c2pa-passphrase-file "
                    "(a passphrase is never accepted on the command line)"
                ) from None
            return credentials.load_identity(cert, key, typed.encode("utf-8"))
    except credentials.CredentialError as exc:
        raise _Usage(str(exc)) from None


def load_anchors(args) -> bytes | None:
    if not args.c2pa_trust_anchors:
        return None
    from . import credentials

    try:
        return credentials.load_anchors(_read_file(args.c2pa_trust_anchors, "trust-anchor file"))
    except credentials.CredentialError as exc:
        raise _Usage(str(exc)) from None


class ReceiptFile:
    """The signer's private receipts, rewritten atomically after every signed file."""

    def __init__(self, path: str):
        from . import credentials

        self.path = path
        self.items: list[dict] = []
        if os.path.exists(path):
            try:
                doc = json.loads(_read_file(path, "receipt file"))
                items = doc["receipts"]
                if doc["imgstrip_receipts"] != credentials.RECEIPT_VERSION or not isinstance(items, list):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise _Usage(f"{path} exists and is not an imgstrip receipt file; choose another name") from None
            self.items = items

    def add(self, receipt: dict) -> None:
        from . import credentials

        self.items.append(receipt)
        doc = json.dumps(credentials.receipts_document(self.items), indent=2).encode("utf-8")
        atomic_write(self.path, doc, 0o600)


@dataclass
class Signing:
    identity: object
    config: object
    receipts: ReceiptFile | None
    assessment: object


def setup_signing(args) -> Signing:
    from . import credentials

    if not credentials.available():
        raise _Usage(credentials.INSTALL_HINT)
    if args.c2pa_commit_source and not args.c2pa_receipt:
        raise _Usage(
            "--c2pa-commit-source needs --c2pa-receipt FILE: without the receipt the commitment can never be opened"
        )
    if args.c2pa_receipt and not args.c2pa_commit_source:
        raise _Usage("--c2pa-receipt only makes sense with --c2pa-commit-source")
    anchors = load_anchors(args)
    identity = load_signing_identity(args)
    try:
        assessment = credentials.assess(identity, anchors)
    except credentials.CredentialError as exc:
        raise _Usage(str(exc)) from None
    errors = [n for n in assessment.notices if n.severity == "error"]
    if errors:
        raise _Usage(errors[0].message)
    receipts = ReceiptFile(args.c2pa_receipt) if args.c2pa_receipt else None
    config = credentials.Config(
        date_precision=args.c2pa_date, detail=args.c2pa_detail, commit_source=args.c2pa_commit_source, anchors=anchors
    )
    return Signing(identity, config, receipts, assessment)


def _credential_summary(signed) -> dict:
    c = signed.credential
    s = c.sanitization or {}
    return {
        "signed": True,
        "signer": c.signer,
        "signer_name": c.signer_name,
        "state": c.state,
        "label": c.label,
        "date": s.get("date"),
        "pixel_integrity": s.get("pixel_integrity"),
        "committed": "source_commitment" in s,
    }


def seal_outcome(outcome: Outcome, signing: Signing, args) -> None:
    """Sign a cleaned outcome in this (parent) process; on any failure nothing is written."""
    from . import credentials

    evidence = outcome.payload.pop("_evidence", None)
    if outcome.status != "cleaned" or evidence is None:
        return
    try:
        signed = credentials.sign(outcome.payload["_data"], evidence, signing.identity, signing.config)
    except credentials.CredentialError as exc:
        outcome.status, outcome.message = "failed", f"could not sign: {exc}"
        outcome.payload.pop("_data", None)
        return
    outcome.payload["_data"] = signed.data
    outcome.payload["bytes_out"] = len(signed.data)
    outcome.payload["credentials"] = _credential_summary(signed)
    if signed.receipt:
        outcome.payload["_receipt"] = signed.receipt
    outcome.payload["warnings"].extend(credentials.source_warnings(evidence))


def make_test_identity_files(args) -> int:
    from . import credentials

    if not credentials.available():
        raise _Usage(credentials.INSTALL_HINT)
    folder = Path(args.c2pa_test_identity)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _Usage(f"cannot create {folder}: {exc.strerror or exc}") from None
    names = {
        "chain": folder / "test-signer-chain.pem",
        "key": folder / "test-signer-key.pem",
        "root": folder / "test-root-ca.pem",
    }
    clash = [str(p) for p in names.values() if p.exists()]
    if clash:
        raise _Usage("will not overwrite: " + ", ".join(clash))
    test = credentials.make_test_identity(args.c2pa_test_name)
    atomic_write(str(names["chain"]), test.chain_pem, None)
    atomic_write(str(names["key"]), test.key_pem, 0o600)
    atomic_write(str(names["root"]), test.root_pem, None)
    print(f"created a TEST identity for '{args.c2pa_test_name}' in {folder}:")
    print(f"  {names['chain'].name}   signing certificate   (--c2pa-cert)")
    print(f"  {names['key'].name}     private key           (--c2pa-key)")
    print(f"  {names['root'].name}        test root CA          (--c2pa-trust-anchors)")
    print("Credentials made with it are cryptographically valid but UNTRUSTED: public viewers show 'Unverified'.")
    print("Use it to try the feature. For publication, use a certificate from an authority on the C2PA Trust List.")
    return EXIT_OK


def verify_one(task: Task, args, anchors: bytes | None, originals: dict | None) -> Outcome:
    from . import credentials

    try:
        data = _read(task.source)
    except OSError as exc:
        return Outcome(task, "failed", message=exc.strerror or str(exc))
    if not data:
        return Outcome(task, "failed", message="file is empty")
    try:
        cred = credentials.read_credential(data, anchors)
    except credentials.CredentialError as exc:
        return Outcome(task, "failed", message=str(exc))
    if not cred.present and task.from_walk and credentials.sniff_format(data) is None:
        return Outcome(task, "skipped", message="not a recognised image container")
    payload = {"format": cred.format, "credential": cred.as_dict()}
    ok = cred.present and cred.state in ("Valid", "Trusted") and cred.imgstrip and cred.pixels_only
    if args.c2pa_original:
        try:
            original = _read(args.c2pa_original)
            receipts = json.loads(_read_file(args.c2pa_receipt, "receipt file")) if args.c2pa_receipt else {}
            audit = credentials.audit(data, original, receipts, anchors, args.max_pixels)
        except (OSError, _Usage, ValueError, StripError) as exc:
            return Outcome(task, "failed", message=f"audit failed: {exc}")
        payload["audit"] = {
            "receipt_found": audit.receipt_found,
            "commitment_ok": audit.commitment_ok,
            "claimed": audit.claimed,
            "measured": audit.measured,
            "consistent": audit.consistent,
            "psnr_db": audit.psnr_db,
            "ok": audit.ok,
            "problems": audit.problems,
        }
        ok = ok and audit.ok
    return Outcome(task, "verified" if ok else "unverified", payload=payload)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _credential_lines(cred: dict) -> list[str]:
    """Plain-language lines about one credential, as a public viewer would see it."""
    if not cred.get("present"):
        return ["no Content Credentials"]
    lines = []
    who = cred.get("signer") or "an unknown signer"
    if cred.get("signer_name") and cred["signer_name"] != cred.get("signer"):
        who += f" ({cred['signer_name']})"
    lines.append(f"credential: {cred['label']}; signed by {who}")
    s = cred.get("sanitization")
    if s:
        lines.append(f"sanitised {s['date']}; pixels: {_PIXEL_WORDS[s['pixel_integrity']]}")
        if s.get("operations"):
            lines.append("operations: " + ", ".join(s["operations"]))
        if s.get("psnr_db") is not None:
            lines.append(f"measured fidelity: {s['psnr_db']} dB PSNR")
        if s.get("source_declared_ai_generated"):
            lines.append("the original's credentials declared the image AI-generated")
        if "source_commitment" in s:
            lines.append("holds a sealed commitment to the original (auditable with its receipt)")
        lines.append("this credential vouches for the processing only, not for the image's origin or authenticity")
    if cred.get("declared_ai") and not s:
        lines.append("declares the image AI-generated")
    if cred.get("pixels_only"):
        lines.append("the file holds nothing but the picture and this credential")
    for item in cred.get("other", []):
        lines.append(f"also in the file: [{item['category']}] {item['label']}")
    for p in cred.get("problems", []):
        lines.append(f"problem: {p}")
    for n in cred.get("notes", []):
        lines.append(f"note: {n}")
    return lines


def _stego_line(v: dict) -> str:
    """One report line for a steganography verdict (see stego.py for the four states)."""
    text = f"steganography: {v['headline']}: {v['summary']}"
    if v["state"] != stego.NOT_SCANNED:  # the reason already says nothing was tested
        text += f". {v['caveat']}"
    return text


_PIXEL_WORDS = {
    "identical": "identical to the file received",
    "equivalent": "same picture, re-encoded (not bit-identical)",
    "altered": "deliberately altered to protect the source",
}


class Reporter:
    def __init__(self, args):
        self.args = args
        self.stream = sys.stderr if (args.output == STDIO or STDIO in args.paths) and not args.check else sys.stdout
        self.records: list[dict] = []
        self.extra: dict = {}

    def emit(self, o: Outcome) -> None:
        name = "<stdin>" if o.task.source == STDIO else o.task.source
        record = {"file": name, "status": o.status}
        if o.destination:
            record["output"] = "<stdout>" if o.destination == STDIO else o.destination
        if o.message:
            record["message"] = o.message
        record.update({k: v for k, v in o.payload.items() if not k.startswith("_")})
        self.records.append(record)
        if self.args.json:
            return
        a = self.args
        p = self.stream
        if o.status == "failed":
            print(f"FAILED   {name}: {o.message}", file=sys.stderr)
            return
        if a.quiet:
            return
        if o.status == "skipped":
            if a.verbose:
                print(f"skipped  {name}: {o.message}", file=p)
            return
        if o.status in ("verified", "unverified"):
            pl = o.payload
            word = "ok" if o.status == "verified" else "PROBLEM"
            print(f"{word:<8} {name} ({pl.get('format') or 'unknown'})", file=p)
            for line in _credential_lines(pl["credential"]):
                print(f"           {line}", file=p)
            audit = pl.get("audit")
            if audit:
                if audit["ok"]:
                    print(
                        f"           audit: the original matches; pixels {_PIXEL_WORDS.get(audit['measured'], audit['measured'])}",
                        file=p,
                    )
                else:
                    for problem in audit["problems"] or ["the audit did not pass"]:
                        print(f"           audit problem: {problem}", file=p)
            return
        if o.status == "checked":
            pl = o.payload
            size = f" {pl['size'][0]}x{pl['size'][1]}" if pl.get("size") else ""
            fmt = pl.get("format") or "unknown"
            if pl["clean"]:
                print(f"clean    {name} ({fmt}{size})", file=p)
            else:
                n = len(pl["findings"])
                print(f"FOUND    {name} ({fmt}{size}): {n} item(s)", file=p)
                for f in pl["findings"]:
                    detail = f": {f['detail']}" if f["detail"] else ""
                    print(f"           [{f['category']}] {f['label']}{detail}", file=p)
            if pl.get("error"):
                print(f"           [structure] {pl['error']}", file=p)
            for note in pl.get("notes", []):
                print(f"           note: {note}", file=p)
            print(f"           {_stego_line(pl['steganography'])}", file=p)
            if pl.get("credential"):
                for line in _credential_lines(pl["credential"]):
                    print(f"           {line}", file=p)
            return
        pl = o.payload
        dest = "<stdout>" if o.destination == STDIO else ("(in place)" if a.in_place else o.destination)
        verb = "would clean" if a.dry_run else "cleaned"
        sizes = f"{_human_size(pl['bytes_in'])} -> {_human_size(pl['bytes_out'])}"
        print(f"{verb:<8} {name} -> {dest}  ({pl['output_format']} {pl['size'][0]}x{pl['size'][1]}, {sizes})", file=p)
        removed = pl["removed"]
        if a.verbose:
            for f in removed:
                detail = f": {f['detail']}" if f["detail"] else ""
                print(f"           - removed [{f['category']}] {f['label']}{detail}", file=p)
            for act in pl["actions"]:
                print(f"           * {act}", file=p)
        elif removed:
            labels = []
            for f in removed:
                if f["label"] not in labels:
                    labels.append(f["label"])
            print(f"           removed: {', '.join(labels)}", file=p)
        verdict = pl["steganography"]
        if a.deep or a.verbose or verdict["state"] in (stego.SCRUBBED, stego.DETECTED):
            print(f"           {_stego_line(verdict)}", file=p)
        if pl.get("dct"):
            d = pl["dct"]
            print(f"           jpeg coefficients: {d['headline']}: {d['summary']}. {d['caveat']}", file=p)
        for w in pl["warnings"]:
            print(f"           warning: {w}", file=p)
        cred = pl.get("credentials")
        if cred:
            who = cred["signer"] or "unknown signer"
            print(
                f"           signed: {who}, {cred['date']}, pixels {_PIXEL_WORDS[cred['pixel_integrity']]} [{cred['label']}]",
                file=p,
            )

    def summary(self, counts: dict[str, int]) -> None:
        if self.args.json:
            json.dump(
                {"files": self.records, "summary": counts, **self.extra},
                sys.stdout if self.stream is sys.stdout else sys.stderr,
                indent=2,
                default=str,
            )
            print(file=sys.stdout if self.stream is sys.stdout else sys.stderr)
            return
        if self.args.quiet or sum(counts.values()) <= 1:
            return
        parts = [f"{v} {k}" for k, v in counts.items() if v]
        print("summary: " + ", ".join(parts), file=self.stream)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


_C2PA_SIGNING_FLAGS = ("c2pa_cert", "c2pa_key", "c2pa_passphrase_file", "c2pa_commit_source")


def _check_credential_options(args, parser) -> None:
    if args.c2pa_passphrase is not None:
        parser.error(
            "a passphrase is never accepted on the command line (it would show in the process list and shell "
            "history): set IMGSTRIP_C2PA_PASSPHRASE or use --c2pa-passphrase-file"
        )
    if args.c2pa and (args.check or args.c2pa_verify):
        parser.error("--c2pa signs files; it cannot be combined with --check or --c2pa-verify")
    if args.c2pa_verify and args.check:
        parser.error("--c2pa-verify and --check are separate modes")
    if not args.c2pa:
        used = [f"--{f.replace('_', '-')}" for f in _C2PA_SIGNING_FLAGS if getattr(args, f)]
        if args.c2pa_date != "day":
            used.append("--c2pa-date")
        if args.c2pa_detail != "minimal":
            used.append("--c2pa-detail")
        if args.c2pa_receipt and not args.c2pa_verify:
            used.append("--c2pa-receipt")
        if used:
            parser.error(f"{', '.join(used)} only work with --c2pa")
    if args.c2pa_original and not args.c2pa_verify:
        parser.error("--c2pa-original only applies with --c2pa-verify")
    if args.c2pa_trust_anchors and not (args.c2pa or args.c2pa_verify):
        parser.error("--c2pa-trust-anchors only applies with --c2pa or --c2pa-verify")
    if args.c2pa_verify:
        if args.c2pa_original and not args.c2pa_receipt:
            parser.error("--c2pa-original needs --c2pa-receipt: the receipt holds the secret that opens the commitment")
        if args.c2pa_receipt and not args.c2pa_original:
            parser.error("--c2pa-receipt with --c2pa-verify needs --c2pa-original")
        if args.c2pa_original and len(args.paths) != 1:
            parser.error("an audit compares one signed file with its original: give exactly one signed file")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        from .selftest import run_self_test

        return run_self_test(verbose=args.verbose)
    if args.c2pa_test_identity:
        try:
            return make_test_identity_files(args)
        except _Usage as exc:
            parser.error(str(exc))
    if not args.paths:
        parser.error("no input files given")
    _check_credential_options(args, parser)
    if args.in_place and args.output:
        parser.error("--in-place and --output are mutually exclusive")
    if args.in_place and (args.random_name or STDIO in args.paths):
        parser.error("--in-place cannot be combined with --random-name or stdin")
    if args.deep and args.c2pa_verify:
        parser.error("--deep applies to --check and to cleaning, not to --c2pa-verify")
    if args.quality is not None and not 1 <= args.quality <= 100:
        parser.error("--quality must be between 1 and 100")
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if args.paths.count(STDIO) > 1:
        parser.error("stdin can only be given once")

    errors: list[str] = []
    tasks = discover(args.paths, args.recursive, args.suffix if not args.in_place else "", errors)
    for e in errors:
        print(f"FAILED   {e}", file=sys.stderr)
    single = len(tasks) == 1 and not any(Path(p).is_dir() for p in args.paths if p != STDIO)
    if args.output == STDIO and len(tasks) > 1:
        parser.error("--output - (stdout) needs exactly one input")
    writes_stdout = args.output == STDIO or (STDIO in args.paths and not args.output)
    if writes_stdout and not args.check and not args.dry_run and sys.stdout.isatty():
        parser.error("refusing to write binary image data to a terminal; redirect stdout or use --output")
    if (
        args.output
        and args.output != STDIO
        and not single
        and Path(args.output).exists()
        and not Path(args.output).is_dir()
    ):
        parser.error("--output must be a directory when processing several files")

    if names_output_file(args, single) and not args.check and not args.c2pa_verify:
        # "-o clean.png" must produce a PNG, not JPEG bytes under a .png name.
        named = format_from_name(args.output)
        wanted = {"jpg": "JPEG", "tif": "TIFF", "heic": "HEIF"}.get(args.format, (args.format or "").upper())
        if named and wanted and named != wanted:
            parser.error(f"--output {args.output} names a {named} file, but --format asks for {wanted}")
        if named and not wanted:
            args.format = named.lower()

    options = options_from_args(args)
    reporter = Reporter(args)
    counts = {
        "cleaned": 0,
        "checked": 0,
        "clean": 0,
        "with findings": 0,
        "verified": 0,
        "unverified": 0,
        "skipped": 0,
        "failed": len(errors),
    }
    planned: set[str] = set()
    signing: Signing | None = None
    anchors = None
    try:
        if args.c2pa:
            signing = setup_signing(args)
            anchors = signing.config.anchors
            reporter.extra["credentials"] = {"identity": signing.assessment.as_dict()}
            if not args.json and not args.quiet:
                for n in signing.assessment.notices:
                    if n.severity == "warning" or (n.severity == "info" and args.verbose):
                        print(f"{'warning' if n.severity == 'warning' else 'note'}: {n.message}", file=sys.stderr)
        elif args.c2pa_verify:
            anchors = load_anchors(args)
    except _Usage as exc:
        parser.error(str(exc))

    work = functools.partial(_work, options=options, args=args, single=single, anchors=anchors)

    def handle(outcome: Outcome) -> None:
        if signing is not None:
            seal_outcome(outcome, signing, args)
        receipt = outcome.payload.pop("_receipt", None)
        if not args.check and not args.c2pa_verify:
            _write_outcome(outcome, args, planned)
        if receipt and outcome.status == "cleaned" and not args.dry_run and signing is not None and signing.receipts:
            signing.receipts.add(receipt)
        reporter.emit(outcome)
        if outcome.status == "checked":
            counts["checked"] += 1
            counts["clean" if outcome.payload["clean"] else "with findings"] += 1
        else:
            counts[outcome.status] += 1

    parallel = args.jobs > 1 and len(tasks) > 1 and STDIO not in args.paths
    if parallel:
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs) as pool:
            futures = [pool.submit(work, t) for t in tasks]
            for fut, task in zip(futures, tasks, strict=True):
                try:
                    handle(fut.result())
                except Exception as exc:  # noqa: BLE001 - a crashed worker must not stop the batch
                    handle(Outcome(task, "failed", message=f"internal error: {exc}"))
    else:
        for task in tasks:
            try:
                handle(work(task))
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                handle(Outcome(task, "failed", message=f"internal error: {type(exc).__name__}: {exc}"))

    if args.c2pa_verify:
        summary = {k: counts[k] for k in ("verified", "unverified", "skipped", "failed")}
    elif args.check:
        summary = {k: counts[k] for k in ("checked", "clean", "with findings", "skipped", "failed")}
    else:
        summary = {k: counts[k] for k in ("cleaned", "skipped", "failed")}
    reporter.summary(summary)
    if counts["failed"]:
        return EXIT_ERROR
    if (args.check and counts["with findings"]) or (args.c2pa_verify and counts["unverified"]):
        return EXIT_FOUND
    return EXIT_OK


def entry() -> None:
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(EXIT_ERROR)
