"""Command-line interface."""

from __future__ import annotations

import argparse
import concurrent.futures
import functools
import json
import os
import secrets
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
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
    proc.add_argument("--optimize", action="store_true", help="try extra compression strategies for PNG output")
    proc.add_argument(
        "--max-pixels",
        type=int,
        default=300_000_000,
        metavar="N",
        help="refuse images larger than N pixels (default: 300M)",
    )
    mode = p.add_argument_group("modes")
    mode.add_argument("--check", action="store_true", help="only report metadata/hidden data; write nothing")
    mode.add_argument(
        "--deep", action="store_true", help="with --check: also decode pixels (steganalysis, hidden colour, palettes)"
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
        if single and not out.is_dir() and not args.output.endswith(os.sep) and not args.random_name:
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
    if deep:
        from .analysis import analyze_pixels

        try:
            findings += analyze_pixels(data)
        except StripError as exc:
            notes.append(f"pixel analysis failed: {exc}")
    payload = {
        "format": report.format,
        "size": report.info.get("size"),
        "clean": not findings and report.error is None,
        "error": report.error,
        "findings": _findings_payload(findings),
        "notes": notes,
    }
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
    }
    outcome = Outcome(task, "cleaned", destination=dest, payload=payload)
    outcome.payload["_data"] = result.data
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


def _work(task: Task, options: Options, args, single: bool) -> Outcome:
    if args.check:
        return check_one(task, args.deep)
    return strip_one(task, options, args, single)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


class Reporter:
    def __init__(self, args):
        self.args = args
        self.stream = sys.stderr if (args.output == STDIO or STDIO in args.paths) and not args.check else sys.stdout
        self.records: list[dict] = []

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
        for w in pl["warnings"]:
            print(f"           warning: {w}", file=p)

    def summary(self, counts: dict[str, int]) -> None:
        if self.args.json:
            json.dump(
                {"files": self.records, "summary": counts},
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        from .selftest import run_self_test

        return run_self_test(verbose=args.verbose)
    if not args.paths:
        parser.error("no input files given")
    if args.in_place and args.output:
        parser.error("--in-place and --output are mutually exclusive")
    if args.in_place and (args.random_name or STDIO in args.paths):
        parser.error("--in-place cannot be combined with --random-name or stdin")
    if args.deep and not args.check:
        parser.error("--deep requires --check")
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

    options = options_from_args(args)
    reporter = Reporter(args)
    counts = {"cleaned": 0, "checked": 0, "clean": 0, "with findings": 0, "skipped": 0, "failed": len(errors)}
    planned: set[str] = set()

    work = functools.partial(_work, options=options, args=args, single=single)

    def handle(outcome: Outcome) -> None:
        if not args.check:
            _write_outcome(outcome, args, planned)
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

    if args.check:
        summary = {k: counts[k] for k in ("checked", "clean", "with findings", "skipped", "failed")}
    else:
        summary = {k: counts[k] for k in ("cleaned", "skipped", "failed")}
    reporter.summary(summary)
    if counts["failed"]:
        return EXIT_ERROR
    if args.check and counts["with findings"]:
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
