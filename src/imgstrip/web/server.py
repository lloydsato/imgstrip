"""Local web interface: a loopback-only HTTP server around the stripping engine.

The server is deliberately small and built on the standard library only.  It
binds to the loopback interface, and requests have to get past these gates
before they reach the engine:

* the ``Host`` header must name this server's loopback address, which defeats
  DNS-rebinding attacks from web pages the user happens to have open;
* the page is a static shell with no secrets in it.  The link printed in the
  terminal carries a single-use launch key in its fragment; the page trades
  it for this run's API token, which it keeps in ``sessionStorage`` (scoped
  to this exact origin, port included).  Each use prints a fresh link.  No
  cookies are used, because browsers send cookies to every port of a host;
* every API call (except fetching a result by its unguessable id) must carry
  the token in a custom header, which a cross-origin page can neither read nor
  send without a CORS preflight that is never answered.

Originals are never written to disk: they arrive in the request body, are
cleaned in memory, and only the verified clean output is kept, in a private
temporary directory that is removed when the server stops.
"""

from __future__ import annotations

import argparse
import atexit
import base64
import binascii
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import signal
import sys
import tempfile
import threading
import urllib.parse
import webbrowser
import zipfile
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import __version__
from .. import credentials, stego
from ..cli import RAW_EXTENSIONS, atomic_write, output_extension
from ..core import EXTENSIONS, OUTPUT_FORMATS, Options, StripError, heif_available, strip_image
from ..inspect import HIDDEN, METADATA, Finding, inspect_bytes

STATIC_DIR = Path(__file__).with_name("static")
DEFAULT_PORT = 8765
MAX_UPLOAD = 1 << 30  # 1 GiB per request
MAX_JSON = 1 << 20
MAX_NAME = 180
DEEP_SCAN_MAX_PIXELS = 40_000_000  # the pixel scan needs about 100 bytes per pixel
PREVIEW_MAX_PIXELS = 100_000_000
WEB_MAX_PIXELS = 100_000_000  # the CLI default is 300 MP; a browser upload is buffered whole, so the web app is stricter
MAX_CONNECTIONS = 32  # simultaneous connections; extra ones are closed at once
SOCKET_TIMEOUT = 30  # seconds a connection may sit idle in the middle of a read
DEFAULT_SUFFIX = ".clean"
STATIC_FILES = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "favicon.svg": "image/svg+xml",
    "fonts/schibsted-grotesk-latin.woff2": "font/woff2",
    "fonts/schibsted-grotesk-latin-ext.woff2": "font/woff2",
}
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
    "img-src 'self' blob: data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)
_ID = re.compile(r"[A-Za-z0-9_-]{16,64}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


# --------------------------------------------------------------------------
# Plain-language summary of what a file reveals
# --------------------------------------------------------------------------

_EXIF_KEYS = (
    "make",
    "model",
    "software",
    "date",
    "artist",
    "host",
    "copyright",
    "description",
    "taken",
    "body serial",
    "owner",
    "lens",
    "lens serial",
    "unique id",
    "user comment",
)
_EXIF_PAIR = re.compile(r"(?:^|[;,] )(" + "|".join(re.escape(k) for k in _EXIF_KEYS) + r")=")
_EXIF_DATE = re.compile(r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2})")


def _exif_fields(detail: str) -> dict[str, str]:
    """Parse the ``key=value, key=value`` summary the inspector writes for EXIF blobs."""
    fields: dict[str, str] = {}
    matches = list(_EXIF_PAIR.finditer(detail))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(detail)
        value = detail[m.end() : end]
        if i + 1 == len(matches):
            value = re.sub(r"(, maker notes)?( \(unparseable\))?$", "", value)
        fields.setdefault(m.group(1), value.strip())
    return fields


def _pretty_date(value: str) -> str:
    m = _EXIF_DATE.match(value)
    if not m:
        return value
    try:
        when = datetime(*(int(g) for g in m.groups()))
    except ValueError:
        return value
    return f"{when.day} {when.strftime('%b %Y, %H:%M')}"


_HIDDEN_IMAGE_WORDS = ("thumbnail", "preview", "multi-picture", "gain-map", "sub-images", "default image", "depth")


def reveals(findings: list[Finding]) -> list[dict]:
    """The few facts a reader cares about first: where, what camera, when, who."""
    out: list[dict] = []
    seen: set[str] = set()

    def add(key: str, label: str, value: str) -> None:
        if key not in seen and value:
            seen.add(key)
            out.append({"key": key, "label": label, "value": value})

    for f in findings:
        if f.label == "GPS location":
            add("location", "Location", f.detail)
    for f in findings:
        if f.category != METADATA or "=" not in f.detail:
            continue
        x = _exif_fields(f.detail)
        make, model = x.get("make", ""), x.get("model", "")
        camera = model if make and model.lower().startswith(make.split()[0].lower()) else f"{make} {model}".strip()
        add("camera", "Camera", camera)
        if x.get("lens"):
            add("lens", "Lens", x["lens"])
        if x.get("taken") or x.get("date"):
            add("time", "Taken" if x.get("taken") else "Date", _pretty_date(x.get("taken") or x.get("date", "")))
        serial = ", ".join(v for v in (x.get("body serial"), x.get("lens serial"), x.get("unique id")) if v)
        add("serial", "Serial number", serial)
        add("person", "Name", x.get("artist") or x.get("owner") or x.get("copyright") or "")
        add("software", "Software", x.get("software") or x.get("host") or "")
    for f in findings:
        label = f.label.lower()
        if f.category == HIDDEN and (label.startswith("data after") or "trailer" in label):
            add("trailer", "Appended data", f.detail or f.label)
        elif f.category == HIDDEN and any(w in label for w in _HIDDEN_IMAGE_WORDS):
            add("preview", "Hidden image", f.label)
        elif f.label == "Comment":
            add("comment", "Comment", f.detail.strip("'"))
        elif f.label == "GPS data":
            add("gps", "GPS tags", f.detail)
        elif f.label in ("XMP", "Extended XMP") and re.search(r"\b(GPS|city|location)\b", f.detail):
            add("location", "Location", "recorded in the XMP data")
    order = (
        "location",
        "camera",
        "time",
        "person",
        "serial",
        "preview",
        "lens",
        "software",
        "trailer",
        "comment",
        "gps",
    )
    out.sort(key=lambda r: order.index(r["key"]) if r["key"] in order else len(order))
    return out


def findings_payload(findings: list[Finding]) -> list[dict]:
    return [{"category": f.category, "label": f.label, "detail": f.detail} for f in findings]


# --------------------------------------------------------------------------
# Session state
# --------------------------------------------------------------------------


@dataclass
class Stored:
    path: Path
    filename: str
    mime: str
    size: int


_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "TIFF": "image/tiff",
    "BMP": "image/bmp",
    "AVIF": "image/avif",
    "HEIF": "image/heic",
}


def safe_filename(raw: str | None, fallback: str = "image") -> str:
    """A display/download name with no directory part, control characters or reserved names."""
    name = urllib.parse.unquote(raw or "")
    name = _CONTROL.sub("", name).replace("\\", "/").rsplit("/", 1)[-1].strip().strip(".")
    name = re.sub(r'[<>:"|?*]', "_", name)
    if not name:
        name = fallback
    if len(name) > MAX_NAME:
        stem, ext = os.path.splitext(name)
        name = stem[: MAX_NAME - len(ext)] + ext
    return name


def clean_name(source: str, fmt: str, random_name: bool = False) -> str:
    if random_name:  # the original name can carry dates and places, so none of it is kept
        return secrets.token_hex(8) + output_extension(source, fmt)
    stem = Path(source).stem or "image"
    if stem.endswith(DEFAULT_SUFFIX):
        stem = stem[: -len(DEFAULT_SUFFIX)]
    return stem + DEFAULT_SUFFIX + output_extension(source, fmt)


def unique_name(name: str, taken: set[str]) -> str:
    stem, ext = os.path.splitext(name)
    candidate, n = name, 2
    while candidate.lower() in taken:
        candidate = f"{stem} ({n}){ext}"
        n += 1
    taken.add(candidate.lower())
    return candidate


class Session:
    """Everything one server run holds: its secrets and the cleaned outputs."""

    def __init__(self, workdir: str | None = None):
        self.token = secrets.token_urlsafe(32)  # API token: never part of a URL
        self.launch_key = secrets.token_urlsafe(32)  # single-use, in the printed link
        self.on_launch = None  # called with the next launch key after each use
        self.dir = Path(workdir or tempfile.mkdtemp(prefix=TEMP_PREFIX))
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self._lock_fd = _hold_lock(self.dir / PID_FILE)
        self._items: dict[str, Stored] = {}
        self._lock = threading.Lock()
        self._last_zip: str | None = None
        self._retired: set[str] = set()
        self.jobs = threading.BoundedSemaphore(max(1, min(4, (os.cpu_count() or 2) // 2)))
        # Content Credentials.  The signing key lives here, in memory only: it is never written to
        # disk, never logged and never sent back to the page.  Receipts hold the secrets that open
        # each source commitment and are likewise kept in memory until the user downloads them.
        self._identity = None
        self._assessment = None
        self._anchors: bytes | None = None
        self._receipts: list[dict] = []

    def set_identity(self, identity, assessment, anchors: bytes | None) -> None:
        with self._lock:
            self._identity, self._assessment, self._anchors = identity, assessment, anchors

    def forget_identity(self) -> None:
        with self._lock:
            self._identity = self._assessment = self._anchors = None

    def signing_state(self):
        """(identity, assessment, anchors) as one consistent snapshot."""
        with self._lock:
            return self._identity, self._assessment, self._anchors

    def add_receipt(self, receipt: dict) -> None:
        with self._lock:
            self._receipts.append(receipt)

    def receipts(self) -> list[dict]:
        with self._lock:
            return list(self._receipts)

    def redeem(self, key: str) -> str | None:
        """Trade the current launch key for a fresh API token, once; then issue the next key.

        Each use replaces the token, so only the newest tab can drive the app.  If someone
        else grabs a key first (a browser's command line is visible to other local users),
        the user's next link locks them out again.
        """
        with self._lock:
            if not _same(key, self.launch_key):
                return None
            self.launch_key = secrets.token_urlsafe(32)
            nxt = self.launch_key
            self._retired.add(hashlib.sha256(self.token.encode()).hexdigest())
            self.token = secrets.token_urlsafe(32)
            token = self.token
        if self.on_launch:
            self.on_launch(nxt)
        return token

    def was_replaced(self, token: str | None) -> bool:
        if not token:
            return False
        return hashlib.sha256(token.encode("utf-8", "surrogateescape")).hexdigest() in self._retired

    def replace_zip(self, rid: str) -> None:
        """Keep one ZIP at a time: each one is a full copy of the batch."""
        with self._lock:
            old, self._last_zip = self._last_zip, rid
        if old:
            self.delete(old)

    def put(self, data: bytes, filename: str, mime: str) -> str:
        rid = secrets.token_urlsafe(24)
        path = self.dir / rid
        with open(path, "xb") as fh:
            fh.write(data)
        with self._lock:
            self._items[rid] = Stored(path, filename, mime, len(data))
        return rid

    def get(self, rid: str) -> Stored | None:
        with self._lock:
            return self._items.get(rid)

    def delete(self, rid: str) -> bool:
        with self._lock:
            item = self._items.pop(rid, None)
        if item:
            item.path.unlink(missing_ok=True)
        return item is not None

    def reset(self) -> None:
        with self._lock:
            items, self._items = list(self._items.values()), {}
        for item in items:
            item.path.unlink(missing_ok=True)

    def close(self) -> None:
        self.reset()
        self.forget_identity()
        with self._lock:
            self._receipts = []
        shutil.rmtree(self.dir, ignore_errors=True)
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None


def _hold_lock(path: Path) -> int | None:
    """Keep an exclusive lock on the session's pid file for as long as the run lives."""
    try:
        import fcntl
    except ImportError:  # Windows: no sweeping, so just record the pid
        path.write_text(f"{os.getpid()}\n")
        return None
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # locked before the pid is even written
    except OSError:
        pass
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def _same(given: str | None, secret: str) -> bool:
    """Constant-time comparison that also copes with non-ASCII input."""
    return hmac.compare_digest((given or "").encode("utf-8", "surrogateescape"), secret.encode())


TEMP_PREFIX = "imgstrip-web-"
PID_FILE = "owner.pid"


def sweep_stale_sessions() -> None:
    """Remove work folders left by runs that were killed before they could clean up.

    A live run holds an exclusive lock on its pid file, so a folder whose lock can be
    taken is abandoned. Unlike a pid check this also holds across PID namespaces and
    containers that share the temporary folder.
    """
    try:
        import fcntl
    except ImportError:
        return
    if not hasattr(os, "getuid"):
        return
    for d in Path(tempfile.gettempdir()).glob(TEMP_PREFIX + "*"):
        try:
            if d.is_symlink() or not d.is_dir() or d.stat().st_uid != os.getuid():
                continue
            fd = os.open(d / PID_FILE, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            continue  # a live run holds it
        finally:
            os.close(fd)
        shutil.rmtree(d, ignore_errors=True)


def default_folder() -> str:
    home = Path.home()
    pictures = home / "Pictures"
    base = pictures if pictures.is_dir() else home
    return str(base / "imgstrip")


def display_path(path: str) -> str:
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def available_formats() -> list[str]:
    from PIL import features

    formats = []
    for fmt in OUTPUT_FORMATS:
        if fmt == "HEIF" and not heif_available():
            continue
        if fmt == "AVIF" and not features.check("avif"):
            continue
        formats.append(fmt)
    return formats


def parse_options(raw: dict) -> Options:
    if not isinstance(raw, dict):
        raise ApiError(400, "options must be an object")
    fmt = raw.get("format") or None
    if fmt is not None:
        fmt = {"JPG": "JPEG", "TIF": "TIFF", "HEIC": "HEIF"}.get(str(fmt).upper(), str(fmt).upper())
        if fmt not in available_formats():
            raise ApiError(400, f"output format {fmt} is not available")
    quality = raw.get("quality")
    if quality is not None:
        if not isinstance(quality, int) or isinstance(quality, bool) or not 1 <= quality <= 100:
            raise ApiError(400, "quality must be a whole number from 1 to 100")
    icc = raw.get("icc", "convert")
    if icc not in ("convert", "sanitize", "drop"):
        raise ApiError(400, "colour profile mode must be convert, sanitize or drop")

    def flag(name: str, default: bool) -> bool:
        value = raw.get(name, default)
        if not isinstance(value, bool):
            raise ApiError(400, f"{name} must be true or false")
        return value

    return Options(
        format=fmt,
        quality=quality,
        jpeg_lossless=flag("jpeg_lossless", False),
        icc=icc,
        auto_orient=flag("auto_orient", True),
        scrub_lsb=flag("scrub_lsb", False),
        scrub_dct="full" if flag("scrub_dct", False) else "canonical" if flag("jpeg_canonical", False) else "off",
        scrub_prnu=flag("scrub_prnu", False),
        optimize=flag("optimize", False),
        max_pixels=WEB_MAX_PIXELS,
    )


def parse_credentials(raw, anchors: bytes | None) -> credentials.Config | None:
    """The page's Content Credentials request, or None when the tick box is off."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ApiError(400, "credentials must be an object")
    sign = raw.get("sign", False)
    if not isinstance(sign, bool):
        raise ApiError(400, "credentials.sign must be true or false")
    if not sign:
        return None
    date = raw.get("date", "day")
    detail = raw.get("detail", "minimal")
    commit = raw.get("commit", False)
    if date not in credentials.DATE_PRECISIONS:
        raise ApiError(400, "credentials.date must be day or minute")
    if detail not in credentials.DETAILS:
        raise ApiError(400, "credentials.detail must be minimal or full")
    if not isinstance(commit, bool):
        raise ApiError(400, "credentials.commit must be true or false")
    return credentials.Config(date_precision=date, detail=detail, commit_source=commit, anchors=anchors)


def _b64(value, what: str, *, required: bool = True, limit: int = 256 << 10) -> bytes | None:
    if value in (None, ""):
        if required:
            raise ApiError(400, f"The {what} is missing.")
        return None
    if not isinstance(value, str):
        raise ApiError(400, f"The {what} must be sent as base64 text.")
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(400, f"The {what} could not be decoded.")
    if len(data) > limit:
        raise ApiError(413, f"The {what} is too large to be a certificate or key.")
    return data


def credentials_info() -> dict:
    return {
        "available": credentials.available(),
        "sdk": credentials.sdk_version(),
        "formats": list(credentials.SIGNABLE),
        "hint": credentials.INSTALL_HINT,
    }


def load_identity_payload(session: Session, payload: dict) -> dict:
    """Load a signing identity into this run's memory (never to disk) and judge it."""
    if not credentials.available():
        raise ApiError(501, credentials.INSTALL_HINT)
    cert = _b64(payload.get("cert"), "certificate")
    key = _b64(payload.get("key"), "private key", required=False)
    anchors_raw = _b64(payload.get("anchors"), "trust-anchor file", required=False)
    passphrase = payload.get("passphrase")
    if passphrase is not None and not isinstance(passphrase, str):
        raise ApiError(400, "The passphrase must be text.")
    try:
        anchors = credentials.load_anchors(anchors_raw) if anchors_raw else None
        identity = credentials.load_identity(cert, key, passphrase.encode("utf-8") if passphrase else None)
        assessment = credentials.assess(identity, anchors)
    except credentials.PassphraseRequired as exc:
        raise ApiError(422, str(exc), code="passphrase")
    except credentials.CredentialError as exc:
        raise ApiError(422, str(exc))
    except Exception:  # noqa: BLE001 - never echo a library's message back: it may quote key data
        raise ApiError(422, "The certificate or key could not be read.")
    blocking = [n for n in assessment.notices if n.severity == "error"]
    if blocking:
        raise ApiError(422, blocking[0].message)
    session.set_identity(identity, assessment, anchors)
    return {"identity": assessment.as_dict()}


def test_identity_payload(session: Session, payload: dict) -> dict:
    if not credentials.available():
        raise ApiError(501, credentials.INSTALL_HINT)
    org = payload.get("organization") or "Test Newsroom"
    if not isinstance(org, str) or not 1 <= len(org.strip()) <= 64:
        raise ApiError(400, "The organisation name must be 1 to 64 characters.")
    try:
        test = credentials.make_test_identity(org.strip())
        anchors = test.root_pem if payload.get("trust_test_ca") else None
        assessment = credentials.assess(test.identity, anchors)
    except credentials.CredentialError as exc:
        raise ApiError(422, str(exc))
    except Exception:  # noqa: BLE001
        raise ApiError(422, "The test identity could not be created.")
    session.set_identity(test.identity, assessment, anchors)
    return {"identity": assessment.as_dict(), "root_pem": test.root_pem.decode("ascii")}


# --------------------------------------------------------------------------
# Work
# --------------------------------------------------------------------------


def _probe(data: bytes) -> tuple[str | None, tuple[int, int] | None]:
    """Format and size from the image header alone, for containers the inspector does not parse."""
    from ..core import _pillow_guard, canonical_format, open_image

    try:
        with _pillow_guard([]):
            im = open_image(data)
            return canonical_format(im.format), im.size
    except Exception:  # noqa: BLE001 - anything unreadable is simply "not an image we can decode"
        return None, None


def inspect_payload(data: bytes, deep: bool) -> dict:
    from ..core import camera_raw_kind

    report = inspect_bytes(data)
    findings = list(report.findings)
    notes: list[str] = []
    size = report.info.get("size")
    decodable = report.format
    raw = camera_raw_kind(data)
    header_size = None
    if report.format is None:
        decodable, header_size = _probe(data)
        size = header_size
    pixels_scanned = False
    verdict = stego.not_scanned("the pixels were not scanned (turn on Deep scan, or choose Scan the pixels too)")
    if deep and (raw or not decodable):
        verdict = stego.not_scanned("this file cannot be decoded, so its pixels cannot be tested")
    if deep and decodable and not raw:
        # The container summary does not always know the size (simple WebP, oversized GIF
        # frames), so ask the decoder: the pixel scan costs about 100 bytes per pixel.
        if header_size is None:
            _, header_size = _probe(data)
        dims = header_size or size
        if not dims or dims[0] * dims[1] > DEEP_SCAN_MAX_PIXELS:
            notes.append(
                f"The pixels were not scanned: the image is over {DEEP_SCAN_MAX_PIXELS // 1_000_000} megapixels."
            )
            verdict = stego.not_scanned(f"the image is over {DEEP_SCAN_MAX_PIXELS // 1_000_000} megapixels")
        else:
            from ..analysis import scan_pixels

            try:
                scan = scan_pixels(data)
                findings += scan.findings
                verdict = scan.verdict
                pixels_scanned = True
            except StripError as exc:
                notes.append(f"The pixel scan failed: {exc}")
                verdict = stego.not_scanned(f"the pixel scan failed: {exc}")
            except MemoryError:
                notes.append("The pixel scan ran out of memory.")
                verdict = stego.not_scanned("the pixel scan ran out of memory")
    credential = None
    if any(credentials.is_c2pa_finding(f) for f in findings) and credentials.available():
        try:
            credential = credentials.read_credential(data).as_dict()
        except Exception:  # noqa: BLE001 - the extra verdict is informational; the scan stands without it
            credential = None
    return {
        "format": report.format,
        "decodable": decodable,
        "raw": raw,
        "credential": credential,
        "pixels_scanned": pixels_scanned,
        "size": list(size) if size else None,
        "bytes": len(data),
        "deep": deep,
        "clean": not findings and report.error is None,
        "error": report.error,
        "findings": findings_payload(findings),
        "steganography": verdict.as_dict(),
        "reveals": reveals(findings),
        "notes": notes,
    }


def preview_bytes(data: bytes, edge: int = 160) -> bytes:
    """A small PNG built from pixels only, for formats the browser cannot show."""
    from PIL import Image, ImageOps

    from ..core import _pillow_guard, fresh, open_image

    import numpy as np

    caught: list[str] = []
    with _pillow_guard(caught):
        im = open_image(data)
        if im.size[0] * im.size[1] > PREVIEW_MAX_PIXELS:
            raise StripError("too large for a preview")
        im.draft("RGB", (edge * 2, edge * 2))  # JPEG only: decode at reduced scale
        try:
            im = ImageOps.exif_transpose(im)
        except Exception:  # noqa: BLE001 - a broken orientation tag must not block a preview
            pass
        if im.mode.startswith("I") or im.mode == "F":  # 16/32-bit and float: Pillow would clip, not scale
            im = im.convert("I") if im.mode.startswith("I;16") else im
            im.thumbnail((edge, edge), Image.Resampling.LANCZOS)  # shrink before any float copy
            arr = np.asarray(im, dtype=np.float32)
            lo, hi = float(arr.min()), float(arr.max())
            arr = (arr - lo) * (255.0 / (hi - lo)) if hi > lo else np.zeros_like(arr)
            im = Image.fromarray(arr.clip(0, 255).astype(np.uint8), "L")
        im = im.convert("RGBA" if "A" in im.getbands() or im.mode == "P" else "RGB")
        im.thumbnail((edge, edge), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        fresh(im).save(out, "PNG")
        return out.getvalue()


def _credential_payload(signed, evidence, assessment, config) -> dict:
    c, s = signed.credential, signed.credential.sanitization or {}
    return {
        "signed": True,
        "signer": c.signer,
        "signer_name": c.signer_name,
        "state": c.state,
        "label": c.label,
        "date": s.get("date"),
        "pixel_integrity": s.get("pixel_integrity"),
        "detail": config.detail,  # what the credential itself records: operations and fidelity only in "full"
        "operations": evidence.operations,
        "psnr_db": evidence.psnr_db,
        "committed": "source_commitment" in s,
        "warnings": [n.message for n in assessment.notices if n.severity == "warning"] if assessment else [],
    }


def clean_payload(
    session: Session,
    data: bytes,
    filename: str,
    options: Options,
    config: credentials.Config | None = None,
    scan_state: str | None = None,
    random_name: bool = False,
) -> dict:
    """Clean one file.  ``scan_state`` is what the page's pixel scan found in the original, if it ran."""
    if Path(filename).suffix.lower() in RAW_EXTENSIONS:
        raise ApiError(422, "Camera RAW files are not supported. Export the photo to TIFF, PNG or JPEG first.")
    identity = assessment = None
    if config is not None:
        identity, assessment, anchors = session.signing_state()
        if identity is None:
            raise ApiError(409, "Load a signing identity first, or turn off Content Credentials.", code="identity")
        config.anchors = anchors
    try:
        result = strip_image(data, options)
    except MemoryError:
        raise ApiError(422, "Out of memory while cleaning this image.")
    except StripError as exc:
        message = str(exc).replace(" (raise it with --max-pixels)", "").replace("--max-pixels", "size")
        raise ApiError(422, message[:1].upper() + message[1:])
    final, cred_payload, extra_warnings = result.data, None, []
    if config is not None:
        reason = credentials.unsignable_reason(result.output_format)
        if reason:
            raise ApiError(422, reason)
        try:
            evidence = credentials.gather_evidence(data, result, options)
            signed = credentials.sign(result.data, evidence, identity, config)
        except MemoryError:
            raise ApiError(422, "Out of memory while signing this image.")
        except credentials.CredentialError as exc:
            raise ApiError(422, f"Could not sign: {exc}")
        final = signed.data
        cred_payload = _credential_payload(signed, evidence, assessment, config)
        extra_warnings = credentials.source_warnings(evidence)
        if signed.receipt:
            session.add_receipt(signed.receipt)
    name = clean_name(filename, result.output_format, random_name)
    scrubbed = result.lsb_scrub in (stego.SCRUB_APPLIED, stego.SCRUB_PARTIAL)
    source = stego.Verdict(scan_state) if scrubbed and scan_state in (stego.DETECTED, stego.NOT_DETECTED) else None
    verdict = stego.for_output(source, result.lsb_scrub, copied=result.verified_exact)
    rid = session.put(final, name, _MIME.get(result.output_format, "application/octet-stream"))
    return {
        "id": rid,
        "filename": name,
        "input_format": result.input_format,
        "output_format": result.output_format,
        "size": list(result.size),
        "frames": result.frames,
        "pixels_exact": result.pixels_exact,
        "lossy": not result.verified_exact,
        "scrubbed": scrubbed,  # what the engine did, not what was asked for
        "scrub_requested": options.scrub_lsb,
        "lsb_scrub": result.lsb_scrub,  # "not_applied": asked for, but this image could not be scrubbed
        "steganography": verdict.as_dict(),
        "dct": stego.dct_report(result.dct_scrub, result.dct_psnr),  # JPEG coefficients: null unless asked for
        "prnu": result.prnu_applied,
        "bytes_in": len(data),
        "bytes_out": len(final),
        "sha256": hashlib.sha256(final).hexdigest(),
        "removed": findings_payload(result.removed),
        "actions": result.actions,
        "warnings": list(result.warnings) + extra_warnings,
        "credentials": cred_payload,
    }


def zip_results(session: Session, ids: list[str]) -> dict:
    items = [session.get(i) for i in ids]
    if not items or any(item is None for item in items):
        raise ApiError(404, "Some cleaned files are no longer available. Clean them again.")
    buf = io.BytesIO()
    taken: set[str] = set()
    # Fixed timestamps: a ZIP's per-entry dates would otherwise record when the files were cleaned.
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for item in items:
            info = zipfile.ZipInfo(unique_name(item.filename, taken), date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.create_system = 3
            zf.writestr(info, item.path.read_bytes())
    data = buf.getvalue()
    name = "imgstrip-clean.zip"
    rid = session.put(data, name, "application/zip")
    session.replace_zip(rid)
    return {"id": rid, "filename": name, "bytes": len(data)}


def write_new(dest: Path, data: bytes) -> None:
    """Write ``dest`` completely or not at all, and never replace an existing file."""
    if dest.exists():
        raise FileExistsError(str(dest))
    tmp = dest.with_name(f".imgstrip-{secrets.token_hex(6)}.tmp")
    atomic_write(str(tmp), data, None)
    try:
        os.link(tmp, dest)  # fails if dest exists: no race with other writers
    except FileExistsError:
        tmp.unlink(missing_ok=True)
        raise
    except OSError:  # file systems without hard links (FAT, exFAT, some network shares)
        try:  # claim the name exclusively, then swap our own placeholder for the content
            os.close(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
        except FileExistsError:
            tmp.unlink(missing_ok=True)
            raise
        try:
            os.replace(tmp, dest)
        except OSError:
            dest.unlink(missing_ok=True)
            tmp.unlink(missing_ok=True)
            raise
    else:
        tmp.unlink(missing_ok=True)


def save_results(session: Session, ids: list[str], folder: str) -> dict:
    if not isinstance(folder, str) or not folder.strip():
        raise ApiError(400, "Choose a folder to save into.")
    target = Path(os.path.expanduser(folder.strip()))
    if not target.is_absolute():
        raise ApiError(400, "Use a full folder path, such as ~/Pictures/clean.")
    target = Path(os.path.abspath(target))
    if target.exists() and not target.is_dir():
        raise ApiError(400, f"{display_path(str(target))} is a file, not a folder.")
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ApiError(400, f"Cannot create {display_path(str(target))}: {exc.strerror or exc}")
    taken = {p.name.lower() for p in target.iterdir()}
    files = []
    for rid in ids:
        item = session.get(rid)
        if item is None:
            files.append({"id": rid, "ok": False, "message": "No longer available. Clean it again."})
            continue
        name = unique_name(item.filename, taken)
        dest = target / name
        try:
            write_new(dest, item.path.read_bytes())
        except FileExistsError:
            files.append({"id": rid, "ok": False, "message": f"{name} appeared in the folder meanwhile; save again."})
            continue
        except OSError as exc:
            files.append({"id": rid, "ok": False, "message": f"Cannot write {name}: {exc.strerror or exc}"})
            continue
        files.append({"id": rid, "ok": True, "name": name, "path": display_path(str(dest))})
    return {"folder": display_path(str(target)), "files": files}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "imgstrip"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = SOCKET_TIMEOUT
    session: Session  # set on the subclass made by make_server
    port: int

    # Requests are not logged: file names and paths are exactly what users want kept private.
    def log_message(self, format, *args):  # noqa: A002 - signature fixed by the base class
        return

    # ---- plumbing ---------------------------------------------------------

    def _security_headers(self) -> None:
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if status >= 400:
            # An error may leave part of the request body unread; never parse it as a new request.
            self.close_connection = True
            self.send_header("Connection", "close")
        self._security_headers()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _error(self, status: int, message: str, code: str | None = None) -> None:
        self._json(status, {"error": message, **({"code": code} if code else {})})

    def _allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").lower() in self._allowed_hosts()

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() not in {"http://" + h for h in self._allowed_hosts()}:
            return False
        site = self.headers.get("Sec-Fetch-Site")
        return site in (None, "same-origin", "none")

    def _token_ok(self) -> bool:
        return _same(self.headers.get("X-Imgstrip-Token"), self.session.token)

    def _require_token(self) -> None:
        if self._token_ok():
            return
        if self.session.was_replaced(self.headers.get("X-Imgstrip-Token")):
            raise ApiError(403, "imgstrip was opened in another tab.", code="replaced")
        raise ApiError(403, "This request did not come from the imgstrip page.", code="expired")

    def _body(self, limit: int) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(411, "Chunked uploads are not supported.")
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            raise ApiError(400, "Bad Content-Length.")
        if length < 0:
            raise ApiError(400, "Bad Content-Length.")
        if length > limit:
            raise ApiError(413, f"Too large: the limit is {limit >> 20} MB per file.")
        data = bytearray()
        while len(data) < length:
            chunk = self.rfile.read(min(1 << 20, length - len(data)))
            if not chunk:
                raise ApiError(400, "Upload was cut short.")
            data += chunk
        return bytes(data)

    def _json_body(self) -> dict:
        try:
            payload = json.loads(self._body(MAX_JSON) or b"{}")
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Request body is not valid JSON.")
        if not isinstance(payload, dict):
            raise ApiError(400, "Request body must be a JSON object.")
        return payload

    def _ids(self, payload: dict) -> list[str]:
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and _ID.fullmatch(i) for i in ids):
            raise ApiError(400, "No cleaned files were selected.")
        return ids

    def _upload(self) -> bytes:
        data = self._body(MAX_UPLOAD)
        if not data:
            raise ApiError(400, "The file is empty.")
        return data

    # ---- routing ----------------------------------------------------------

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_GET(self):  # noqa: N802
        if not self._host_ok():
            return self._send(421, b"Misdirected request", "text/plain; charset=utf-8")
        url = urllib.parse.urlsplit(self.path)
        try:
            if url.path == "/":
                return self._index()
            if url.path.startswith("/static/"):
                return self._static(url.path[len("/static/") :])
            if url.path.startswith("/api/result/"):
                # Result ids are unguessable capabilities handed only to the page, so a plain
                # link can download them; everything else needs the token.
                return self._result(url.path[len("/api/result/") :])
            if url.path == "/api/info":
                if not self._origin_ok():
                    raise ApiError(403, "This request did not come from the imgstrip page.")
                self._require_token()
                return self._json(200, self._info())
            raise ApiError(404, "Not found.")
        except ApiError as exc:
            self._error(exc.status, exc.message, exc.code)
        except Exception as exc:  # noqa: BLE001 - one bad request must never take the server down
            self._error(500, f"Internal error ({type(exc).__name__}).")

    def do_POST(self):  # noqa: N802
        self._api_write()

    def do_DELETE(self):  # noqa: N802
        self._api_write()

    def _api_write(self) -> None:
        if not self._host_ok():
            return self._send(421, b"Misdirected request", "text/plain; charset=utf-8")
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        route = (self.command, url.path)
        try:
            if not self._origin_ok():
                raise ApiError(403, "This request did not come from the imgstrip page.")
            if route == ("POST", "/api/session"):
                token = self.session.redeem(str(self._json_body().get("key") or ""))
                if token is None:
                    raise ApiError(403, "This link has already been used.")
                return self._json(200, {"token": token})
            self._require_token()
            if route == ("POST", "/api/inspect"):
                data = self._upload()
                deep = query.get("deep", ["0"])[0] == "1"
                with self.session.jobs:
                    return self._json(200, inspect_payload(data, deep))
            if route == ("POST", "/api/preview"):
                data = self._upload()
                with self.session.jobs:
                    try:
                        png = preview_bytes(data)
                    except Exception as exc:  # noqa: BLE001 - any decoder failure means "no preview"
                        raise ApiError(422, f"No preview: {exc}")
                return self._send(200, png, "image/png")
            if route == ("POST", "/api/clean"):
                data = self._upload()
                filename = safe_filename(self.headers.get("X-Filename"))
                try:
                    raw = json.loads(urllib.parse.unquote(self.headers.get("X-Options") or "{}"))
                except ValueError:
                    raise ApiError(400, "Options are not valid JSON.")
                options = parse_options(raw)
                random_name = raw.get("random_name", False)
                if not isinstance(random_name, bool):
                    raise ApiError(400, "random_name must be true or false")
                config = parse_credentials(raw.get("credentials") if isinstance(raw, dict) else None, None)
                scan_state = self.headers.get("X-Scan")
                if scan_state not in (None, stego.DETECTED, stego.NOT_DETECTED):
                    raise ApiError(400, "X-Scan must be detected or not_detected.")
                with self.session.jobs:
                    return self._json(200, clean_payload(self.session, data, filename, options, config, scan_state, random_name))
            if route == ("POST", "/api/identity"):
                return self._json(200, load_identity_payload(self.session, self._json_body()))
            if route == ("POST", "/api/identity/test"):
                return self._json(200, test_identity_payload(self.session, self._json_body()))
            if route == ("DELETE", "/api/identity"):
                self._body(MAX_JSON)
                self.session.forget_identity()
                return self._json(200, {"ok": True})
            if route == ("POST", "/api/receipts"):
                self._body(MAX_JSON)
                receipts = self.session.receipts()
                if not receipts:
                    raise ApiError(404, "There are no receipts to download.")
                body = json.dumps(credentials.receipts_document(receipts), indent=2).encode()
                return self._send(200, body, "application/json")
            if route == ("POST", "/api/zip"):
                return self._json(200, zip_results(self.session, self._ids(self._json_body())))
            if route == ("POST", "/api/save"):
                payload = self._json_body()
                return self._json(200, save_results(self.session, self._ids(payload), payload.get("folder")))
            if route == ("POST", "/api/reset"):
                self._body(MAX_JSON)
                self.session.reset()
                return self._json(200, {"ok": True})
            if self.command == "DELETE" and url.path.startswith("/api/result/"):
                rid = url.path[len("/api/result/") :]
                return self._json(200, {"ok": bool(_ID.fullmatch(rid)) and self.session.delete(rid)})
            raise ApiError(404, "Not found.")
        except ApiError as exc:
            self._error(exc.status, exc.message, exc.code)
        except Exception as exc:  # noqa: BLE001 - one bad file must never take the server down
            self._error(500, f"Internal error ({type(exc).__name__}: {exc}).")

    # ---- pages ------------------------------------------------------------

    def _index(self) -> None:
        # A static shell: it holds no secret, the launch key travels in the URL fragment.
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace("{{VERSION}}", __version__)
        self._send(200, html.encode(), "text/html; charset=utf-8")

    def _static(self, rel: str) -> None:
        ctype = STATIC_FILES.get(rel)
        if ctype is None:
            raise ApiError(404, "Not found.")
        self._send(200, (STATIC_DIR / rel).read_bytes(), ctype)

    def _info(self) -> dict:
        return {
            "version": __version__,
            "formats": available_formats(),
            "extensions": {k: list(v) for k, v in EXTENSIONS.items()},
            "heif": heif_available(),
            "folder": display_path(default_folder()),
            "max_upload": MAX_UPLOAD,
            "credentials": credentials_info(),
            "identity": self._identity_state(),
            "receipts": len(self.session.receipts()),
        }

    def _identity_state(self) -> dict | None:
        _, assessment, _ = self.session.signing_state()
        return assessment.as_dict() if assessment is not None else None

    def _result(self, rid: str) -> None:
        item = self.session.get(rid) if _ID.fullmatch(rid) else None
        if item is None:
            raise ApiError(404, "This cleaned file is no longer available.")
        body = item.path.read_bytes()
        quoted = urllib.parse.quote(item.filename, safe="")
        ascii_name = re.sub(r"[^A-Za-z0-9._ ()-]", "_", item.filename)
        disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"
        self._send(200, body, item.mime, {"Content-Disposition": disposition})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    max_connections = MAX_CONNECTIONS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(self.max_connections)

    def process_request(self, request, client_address):
        # One idle socket per thread would otherwise let a local process starve the server of threads.
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_server(port: int = DEFAULT_PORT, session: Session | None = None) -> tuple[Server, Session]:
    session = session or Session()
    try:
        httpd = Server(("127.0.0.1", port), Handler)
    except OSError:
        if port == 0:
            raise
        httpd = Server(("127.0.0.1", 0), Handler)  # preferred port is taken: any free one
    handler = type("BoundHandler", (Handler,), {"session": session, "port": httpd.server_address[1]})
    httpd.RequestHandlerClass = handler
    return httpd, session


def launch_url(httpd: Server, key: str) -> str:
    return f"http://127.0.0.1:{httpd.server_address[1]}/#k={key}"


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _stop_on_signal(signum, frame):  # noqa: ARG001 - signal handler signature
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="imgstrip-web",
        description="Open imgstrip in your browser. Runs only on this computer; images never leave it.",
    )
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port on 127.0.0.1 (default {DEFAULT_PORT})")
    p.add_argument(
        "--no-browser",
        action="store_true",
        help="print the link instead of opening a browser (recommended on computers shared with other users)",
    )
    p.add_argument("--version", action="version", version=f"imgstrip {__version__}")
    args = p.parse_args(argv)
    if not 0 <= args.port <= 65535:
        p.error("--port must be between 0 and 65535")
    sweep_stale_sessions()
    httpd, session = make_server(args.port)
    atexit.register(session.close)
    # Closing the terminal (SIGHUP) or `kill` (SIGTERM) must still delete the cleaned files.
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None and signal.getsignal(sig) is not signal.SIG_IGN:  # respect nohup
            signal.signal(sig, _stop_on_signal)

    def on_launch(key: str) -> None:
        print(
            f"Opened in a browser tab (earlier tabs stop working). Each link works once; next link:\n  {launch_url(httpd, key)}",
            flush=True,
        )

    session.on_launch = on_launch
    url = launch_url(httpd, session.launch_key)
    print(f"imgstrip {__version__} is running on this computer only.", flush=True)
    print(f"  {url}", flush=True)
    print("The link opens the app once. Press Ctrl+C to stop; cleaned files are deleted then.", flush=True)
    if not args.no_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    finally:
        httpd.server_close()
        session.close()
    return 0


def entry() -> None:
    sys.exit(main())
