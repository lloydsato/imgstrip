"""Decoding in a separate process (review item 4).

Image decoders are large bodies of C code (libjpeg, libpng's and Pillow's own decoders, libwebp, libtiff, libavif with
aom and dav1d, libheif with libde265 and x265, LittleCMS) that read whatever bytes an attacker puts in a file.
imgstrip runs them, and everything else that reads the input, in a process of their own.  A decoder that misbehaves on
a hostile file (a crash, a memory blow-up, or code execution through a memory-safety bug) then cannot touch the
process that encodes, verifies and writes the output, nor anything that process holds: a signing key, the web app's
session, the other files of a batch.  What comes back is pixels (or a JPEG's quantised coefficients) and a short list
of typed values, over a pipe, in a format that carries no code (no pickle in either direction), and this side checks
every field before anything uses it (``_check_decoded``).  Those typed values are the input's residual channels:
docs/REFERENCE.md lists them.

How it works:

* A *zygote* process is started with ``python -I`` (no environment variables, user site or current directory on the
  import path; the main process's own import path is handed over explicitly).  It imports numpy, Pillow with all its
  plugins, pillow-heif and imgstrip, then waits.  It is never forked from the main process, so it shares none of its
  memory (no key, no other file).
* For every job the zygote forks a fresh *worker*, so nothing survives from one file to the next.  Before it reads
  the input the worker lowers its own limits: no core dump, no file it can write to (``RLIMIT_FSIZE`` 0), a CPU-time
  limit, and on Linux no new privileges and death with its zygote.  It cannot read further requests or write to the
  main process except through the pipe of its own job, which the zygote relays in framed chunks followed by the
  worker's exit status, so a worker that dies half-way is noticed, never misread.
* There is one zygote per job running at the same time (the web app works on several at once); idle ones are kept.
  On Windows, which cannot fork, every job gets a fresh process instead, and one is kept started in advance.

What it does not do: it is not a system-call filter.  A decoder exploited to run code could still read the user's
files and use the network with the user's rights; it cannot write files or reach the main process's memory.

``set_default(False)`` (``--no-sandbox``) decodes in the main process instead.  If the sandbox cannot be started
(a Python without a usable ``sys.executable``, for example a frozen application), every job fails with a message that
names that switch: imgstrip never falls back silently.
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import struct
import subprocess
import sys
import threading
from dataclasses import fields as dataclass_fields

import numpy as np

from .core import StripError

_DEFAULT = True
_TIMEOUT = 900.0  # wall-clock seconds for one job
_CPU_SECONDS = 900  # the worker's CPU-time limit
_START_TIMEOUT = 120.0
_CHUNK = 1 << 20  # bytes per relayed chunk
_STRIP = 1 << 22  # bytes per strip of an image sent in pieces
_MAX_HEADER = 64 << 20  # the JSON part of a response
_MAX_ITEMS = 100_000  # list lengths in a response (findings, actions, frames, palette entries)
_MAX_TEXT = 1 << 16  # characters in one string


def set_default(on: bool) -> None:
    """Switch the sandbox on or off for this process (``--no-sandbox`` switches it off)."""
    global _DEFAULT
    _DEFAULT = bool(on)


def enabled(choice: bool | None = None) -> bool:
    """Whether to decode in the sandbox: ``choice`` when given (Options.sandbox), else the process-wide setting."""
    return _DEFAULT if choice is None else bool(choice)


class SandboxError(StripError):
    """The sandbox could not be started, or its worker failed or answered something it may not."""


# --------------------------------------------------------------------------
# Wire format: frames of (2-byte magic, 1-byte kind, 8-byte length, payload)
# --------------------------------------------------------------------------

_MAGIC = b"IS"
_HEAD = struct.Struct(">2scQ")


class _Writer:
    def __init__(self, fh):
        self.fh = fh

    def frame(self, kind: bytes, payload=b"") -> None:
        self.fh.write(_HEAD.pack(_MAGIC, kind, len(payload)))
        if len(payload):
            self.fh.write(payload)

    def flush(self) -> None:
        self.fh.flush()


class _Reader:
    def __init__(self, fh):
        self.fh = fh

    def readinto(self, view: memoryview) -> None:
        while len(view):
            n = self.fh.readinto(view)
            if not n:
                raise EOFError("the stream ended")
            view = view[n:]

    def read(self, n: int) -> bytes:
        buf = bytearray(n)
        self.readinto(memoryview(buf))
        return bytes(buf)

    def header(self) -> tuple[bytes, int]:
        magic, kind, length = _HEAD.unpack(self.read(_HEAD.size))
        if magic != _MAGIC:
            raise ValueError("not a sandbox frame")
        return kind, length


class _Ended(Exception):
    """The worker's stream ended (its exit status is known)."""


class _Relayed:
    """The worker's own stream, put back together from the zygote's data frames, up to its exit status."""

    def __init__(self, outer: _Reader):
        self.outer = outer
        self.left = 0
        self.status: dict | None = None

    def _next(self) -> None:
        kind, length = self.outer.header()
        if kind == b"D":
            self.left = length
        elif kind == b"S" and length <= 4096:
            self.status = json.loads(self.outer.read(length))
            raise _Ended
        else:
            raise ValueError(f"unexpected frame {kind!r}")

    def readinto(self, view: memoryview) -> None:
        while len(view):
            if not self.left:
                self._next()
            n = min(self.left, len(view))
            self.outer.readinto(view[:n])
            view = view[n:]
            self.left -= n

    def read(self, n: int) -> bytes:
        buf = bytearray(n)
        self.readinto(memoryview(buf))
        return bytes(buf)

    def finish(self) -> dict:
        """Skip to the worker's exit status (what it wrote after its answer is ignored)."""
        while self.status is None:
            try:
                if self.left:
                    self.outer.read(self.left)
                    self.left = 0
                self._next()
            except _Ended:
                break
        return self.status


# --------------------------------------------------------------------------
# Values: JSON with typed placeholders for raw blobs (bytes, arrays, images)
# --------------------------------------------------------------------------

_DTYPES = {"|u1", "<u2", "<i2", "<i4", "<f4", "|b1"}
_MODES = {"1": 1, "L": 8, "P": 8, "LA": 16, "RGB": 24, "RGBA": 32, "CMYK": 32, "I;16": 16, "I": 32, "F": 32}


def _row_bytes(mode: str, width: int) -> int:
    return (width * _MODES[mode] + 7) // 8


def _types():
    from . import analysis, dct, stego
    from .core import Decoded, Frame
    from .inspect import Finding, Report

    return {
        "Decoded": Decoded,
        "Frame": Frame,
        "Report": Report,
        "Finding": Finding,
        "DctFrame": dct.Frame,
        "DctComponent": dct.Component,
        "PixelScan": analysis.PixelScan,
        "Verdict": stego.Verdict,
    }


def _pack(value, blobs: list):
    """A JSON-able form of ``value``; raw data is appended to ``blobs`` and referenced by index."""
    from PIL import Image

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        blobs.append(bytes(value))
        return {"$bytes": len(blobs) - 1, "size": len(value)}
    if isinstance(value, np.ndarray):
        arr = np.ascontiguousarray(value)
        if arr.dtype.byteorder == ">" or arr.dtype.str not in _DTYPES:
            arr = arr.astype(arr.dtype.newbyteorder("<"))
        blobs.append(arr)
        return {"$array": len(blobs) - 1, "dtype": arr.dtype.str, "shape": list(arr.shape)}
    if isinstance(value, Image.Image):
        blobs.append(value)
        palette = value.getpalette("RGB") if value.mode in ("P", "PA") else None
        return {"$image": len(blobs) - 1, "mode": value.mode, "size": list(value.size), "palette": palette}
    if isinstance(value, (list, tuple)):
        return [_pack(v, blobs) for v in value]
    if isinstance(value, dict):
        return {"$dict": [[_pack(k, blobs), _pack(v, blobs)] for k, v in value.items()]}
    for name, cls in _types().items():
        if type(value) is cls:
            return {"$type": name, **{f.name: _pack(getattr(value, f.name), blobs) for f in dataclass_fields(cls)}}
    raise TypeError(f"cannot send a {type(value).__name__} out of the sandbox")


def _send_blobs(blobs: list, out: _Writer) -> None:
    from PIL import Image

    for blob in blobs:
        if isinstance(blob, Image.Image):
            rows = max(1, _STRIP // max(1, _row_bytes(blob.mode, blob.width)))
            for top in range(0, blob.height, rows):
                out.frame(b"B", blob.crop((0, top, blob.width, min(blob.height, top + rows))).tobytes())
        elif isinstance(blob, np.ndarray):
            view = memoryview(blob).cast("B")
            for start in range(0, len(view), _STRIP):
                out.frame(b"B", view[start : start + _STRIP])
        else:
            for start in range(0, len(blob), _STRIP):
                out.frame(b"B", blob[start : start + _STRIP])


class _Blobs:
    """Reads the blobs of a response, in order, straight into their destinations."""

    def __init__(self, stream: _Relayed, budget: int):
        self.stream = stream
        self.budget = budget
        self.pending = 0  # bytes left in the current blob frame

    def _take(self, n: int) -> None:
        if n > self.budget:
            raise ValueError("the response is larger than the job allows")
        self.budget -= n

    def readinto(self, view: memoryview) -> None:
        while len(view):
            if not self.pending:
                kind, length = _inner_header(self.stream)
                if kind != b"B":
                    raise ValueError("a blob is shorter than declared")
                self.pending = length
            n = min(self.pending, len(view))
            self.stream.readinto(view[:n])
            view = view[n:]
            self.pending -= n

    def bytes(self, size: int) -> bytes:
        self._take(size)
        buf = bytearray(size)
        self.readinto(memoryview(buf))
        return bytes(buf)

    def array(self, dtype: str, shape: list[int]) -> np.ndarray:
        if dtype not in _DTYPES or not isinstance(shape, list) or len(shape) > 4:
            raise ValueError("an array of a kind the sandbox may not send")
        if not all(isinstance(n, int) and 0 <= n <= 1 << 31 for n in shape):
            raise ValueError("an impossible array shape")
        size = np.dtype(dtype).itemsize
        for n in shape:
            size *= n
        self._take(size)  # before anything is allocated
        out = np.empty(shape, np.dtype(dtype))
        self.readinto(memoryview(out).cast("B"))
        return out

    def image(self, mode: str, size: list, palette):
        from PIL import Image

        if mode not in _MODES or not (isinstance(size, list) and len(size) == 2):
            raise ValueError("an image of a kind the sandbox may not send")
        width, height = size
        if not all(isinstance(n, int) and 1 <= n <= 1 << 20 for n in size):
            raise ValueError("an impossible image size")
        row = _row_bytes(mode, width)
        self._take(row * height)
        img = Image.new(mode, (width, height))
        rows = max(1, _STRIP // max(1, row))
        for top in range(0, height, rows):
            n = min(height, top + rows) - top
            buf = bytearray(row * n)
            self.readinto(memoryview(buf))
            img.paste(Image.frombytes(mode, (width, n), bytes(buf)), (0, top))
        if palette is not None:
            if mode not in ("P", "PA") or not _ints(palette, 0, 255) or len(palette) % 3 or len(palette) > 768:
                raise ValueError("an impossible palette")
            img.putpalette(palette, "RGB")
        return img


def _inner_header(stream: _Relayed) -> tuple[bytes, int]:
    kind, length = _HEAD.unpack(stream.read(_HEAD.size))[1:]
    return kind, length


def _ints(values, lo: int, hi: int) -> bool:
    return isinstance(values, (list, tuple)) and all(type(v) is int and lo <= v <= hi for v in values)


def _unpack(value, blobs: _Blobs, depth: int = 0):
    """Rebuild a value from its JSON form, reading its blobs in the order they were sent."""
    if depth > 32:
        raise ValueError("a value nested too deeply")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > _MAX_TEXT:
            raise ValueError("a string too long")
        return value
    if isinstance(value, list):
        if len(value) > _MAX_ITEMS:
            raise ValueError("a list too long")
        return [_unpack(v, blobs, depth + 1) for v in value]
    if not isinstance(value, dict):
        raise ValueError("an unknown kind of value")
    if "$bytes" in value:
        if type(value.get("size")) is not int or value["size"] < 0:
            raise ValueError("an impossible size")
        return blobs.bytes(value["size"])
    if "$array" in value:
        return blobs.array(value.get("dtype"), value.get("shape"))
    if "$image" in value:
        return blobs.image(value.get("mode"), value.get("size"), value.get("palette"))
    if "$dict" in value:
        pairs = value["$dict"]
        if not isinstance(pairs, list) or len(pairs) > _MAX_ITEMS:
            raise ValueError("an impossible mapping")
        out = {}
        for pair in pairs:
            if not (isinstance(pair, list) and len(pair) == 2):
                raise ValueError("an impossible mapping")
            key = _unpack(pair[0], blobs, depth + 1)
            if not isinstance(key, (str, int)):
                raise ValueError("an impossible mapping key")
            out[key] = _unpack(pair[1], blobs, depth + 1)
        return out
    cls = _types().get(value.get("$type"))
    if cls is None:
        raise ValueError("a value of a type the sandbox may not send")
    names = {f.name for f in dataclass_fields(cls)}
    if set(value) - names - {"$type"}:
        raise ValueError(f"unknown fields in a {cls.__name__}")
    return cls(**{k: _unpack(v, blobs, depth + 1) for k, v in value.items() if k != "$type"})


# --------------------------------------------------------------------------
# The worker side
# --------------------------------------------------------------------------


def _restrict() -> int:
    """Limits a worker sets on itself before it reads the input (POSIX).  Returns the Landlock ABI applied (0: none)."""
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return 0
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # a write past the limit fails with EFBIG instead of killing
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    _soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
    cpu = _CPU_SECONDS if hard == resource.RLIM_INFINITY else min(_CPU_SECONDS, hard)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, hard))
    if not sys.platform.startswith("linux"):
        return 0
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(38, 1, 0, 0, 0)  # PR_SET_NO_NEW_PRIVS (Landlock needs it too)
        libc.prctl(1, signal.SIGKILL, 0, 0, 0)  # PR_SET_PDEATHSIG: die with the zygote
        return _landlock(libc)
    except (OSError, AttributeError):
        return 0


# Landlock (Linux 5.13 and later): rights the worker gives up for good, everywhere.  Reading stays allowed (a lazy
# import must still work); everything that changes the file system, executes a program or uses a device does not.
# With ABI 4 it also gives up TCP, and with ABI 6 signals and abstract sockets outside itself.  A Landlock domain
# also stops it from attaching to (ptrace), or reading the memory of, any process outside it.
_LL_FS = {1: 0x1FF3, 2: 0x3FF3, 3: 0x7FF3, 5: 0xFFF3}  # every access right but READ_FILE (4) and READ_DIR (8)
_LL_NET = 0x3  # BIND_TCP | CONNECT_TCP
_LL_SCOPE = 0x3  # ABSTRACT_UNIX_SOCKET | SIGNAL
_SYS_LANDLOCK = (444, 446)  # landlock_create_ruleset, landlock_restrict_self (the same on every architecture)


def _landlock(libc) -> int:
    import ctypes

    libc.syscall.restype = ctypes.c_long
    abi = libc.syscall(_SYS_LANDLOCK[0], None, ctypes.c_size_t(0), ctypes.c_uint32(1))  # ..._VERSION
    if abi < 1:
        return 0
    fs = _LL_FS[max(k for k in _LL_FS if k <= abi)]
    attr = (ctypes.c_uint64 * 3)(fs, _LL_NET if abi >= 4 else 0, _LL_SCOPE if abi >= 6 else 0)
    size = 8 if abi < 4 else 16 if abi < 6 else 24
    ruleset = libc.syscall(_SYS_LANDLOCK[0], ctypes.byref(attr), ctypes.c_size_t(size), ctypes.c_uint32(0))
    if ruleset < 0:
        return 0
    try:
        if libc.syscall(_SYS_LANDLOCK[1], ctypes.c_int(ruleset), ctypes.c_uint32(0)) != 0:
            return 0
    finally:
        os.close(ruleset)
    return int(abi)


def _job(name: str, params: dict, data: bytes):
    """Run one job in this (worker) process and return the value to send back."""
    from . import core

    max_pixels = int(params.get("max_pixels", core.DEFAULT_MAX_PIXELS))
    if name == "decode":
        options = core.Options(**{**params["options"], "sandbox": False})
        decoded = core._Job(data, options, None).decode()
        report = decoded.report
        report.info = {k: v for k, v in report.info.items() if k in _REPORT_INFO}
        return decoded
    if name == "frames":
        return core.canonical_frames_here(data, max_pixels)
    if name == "pixels":
        with core._pillow_guard([], max_pixels):
            try:
                im = core.open_image(data)
                im.load()
                return {"pixels": np.asarray(im), "mode": im.mode}
            except StripError:
                raise
            except Exception as exc:  # decoders raise anything on corrupt data
                raise StripError(f"cannot decode image ({type(exc).__name__}: {exc})") from None
    if name == "scan":
        from .analysis import scan_pixels

        return scan_pixels(data)
    if name in ("facts", "preview"):
        from .web import server

        if name == "facts":
            return {"probe": list(server._probe_here(data)), "facts": server.image_facts_here(data)}
        return server.preview_pixels_here(data, int(params["edge"]), int(params["limit"]))
    if name == "_limits":  # for the tests and the self-test: what a worker may still do
        import socket

        try:
            import resource

            limit = lambda which: list(resource.getrlimit(which))
            fsize, core_, cpu = (limit(r) for r in (resource.RLIMIT_FSIZE, resource.RLIMIT_CORE, resource.RLIMIT_CPU))
        except ImportError:  # pragma: no cover - Windows: a process of its own, and nothing more
            fsize = core_ = cpu = None

        def attempt(step) -> str:
            try:
                step()
                return "yes"
            except OSError as exc:
                return f"no ({exc.strerror or exc})"

        def write():
            with open(os.path.join(params["dir"], "written"), "wb") as fh:
                fh.write(b"x" * 4096)

        def peek():  # the main process's memory, which holds a signing key or the web app's session
            with open(f"/proc/{params['target']}/mem", "rb") as fh:
                fh.read(1)

        def connect():
            with socket.create_connection(("127.0.0.1", params["port"]), timeout=5):
                pass

        return {
            "write": attempt(write),
            "create": attempt(
                lambda: os.close(os.open(os.path.join(params["dir"], "created"), os.O_CREAT | os.O_WRONLY))
            ),
            "remove": attempt(lambda: os.unlink(params["victim"])),
            "connect": attempt(connect) if params.get("port") else "not tried",
            "peek": attempt(peek) if params.get("target") else "not tried",
            "landlock": params.get("landlock", 0),
            "fsize": fsize,
            "core": core_,
            "cpu": cpu,
            "pid": os.getpid(),
            "environment": sorted(os.environ),
        }
    if name == "_crash":
        os.abort()
    if name == "_hang":
        import time

        time.sleep(3600)
    if name == "_lie":  # an answer that breaks the rules, to test this side's checks (see _answer)
        return None
    raise StripError(f"unknown sandbox job {name!r}")


_REPORT_INFO = ("lossless", "chroma", "size")


def _answer(name: str, params: dict, data: bytes, out: _Writer) -> None:
    if name == "_lie":  # the frames the test asked for, exactly
        for kind, payload in params["frames"]:
            out.frame(kind.encode("ascii"), payload.encode("latin-1"))
        return
    try:
        blobs: list = []
        value = _pack(_job(name, params, data), blobs)
        out.frame(b"H", json.dumps(value).encode())
        _send_blobs(blobs, out)
        out.frame(b"E")
    except StripError as exc:
        out.frame(b"X", json.dumps({"kind": "strip", "message": str(exc)}).encode())
    except MemoryError:
        out.frame(b"X", json.dumps({"kind": "memory", "message": "not enough memory to decode this image"}).encode())
    except BaseException as exc:  # every failure must reach the main process as a message
        out.frame(b"X", json.dumps({"kind": "error", "message": f"{type(exc).__name__}: {exc}"}).encode())


class _Chunked:
    """A file-like sink that wraps everything written into data frames (a worker that runs inside the zygote)."""

    def __init__(self, out: _Writer):
        self.out = out

    def write(self, data) -> None:
        if len(data):
            self.out.frame(b"D", data)

    def flush(self) -> None:
        self.out.flush()


def _preload() -> None:
    """Everything a job needs, imported once in the zygote so that every worker starts with it."""
    import numpy  # noqa: F401
    from PIL import Image, ImageCms  # noqa: F401

    from . import analysis, codecs, core, dct, icc, inspect, jpeg, prnu, stego  # noqa: F401

    Image.init()
    core._register_plugins()


def _kib(maxrss: int) -> int:
    """``ru_maxrss`` in bytes (Linux counts it in KiB, macOS in bytes)."""
    return maxrss if sys.platform == "darwin" else maxrss * 1024


def _own_size() -> int:
    """This process's resident size now (Linux), else its peak: what a worker starts from."""
    try:
        with open("/proc/self/statm", "rb") as fh:
            return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        import resource

        return _kib(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def _serve() -> None:
    """The zygote: wait for requests on stdin, fork a worker for each, relay its answer on stdout."""
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    out = _Writer(stdout)
    try:
        _preload()
    except BaseException as exc:
        out.frame(b"F", f"{type(exc).__name__}: {exc}".encode())
        out.flush()
        return
    fork = hasattr(os, "fork")
    out.frame(b"Y", b"fork" if fork else b"once")
    out.flush()
    requests = _Reader(stdin)
    while True:
        try:
            kind, length = requests.header()
            if kind != b"R" or length > _MAX_HEADER:
                return
            header = json.loads(requests.read(length))
            kind, length = requests.header()
            if kind != b"I":
                return
            data = requests.read(length)
        except (EOFError, ValueError, struct.error):
            return
        if not fork:
            _answer(header["job"], header, data, _Writer(_Chunked(out)))
            out.frame(b"S", json.dumps({"exit": 0}).encode())
            out.flush()
            return  # one job per process where there is no fork
        read_end, write_end = os.pipe()
        base = _own_size()
        pid = os.fork()
        if pid == 0:  # the worker
            code = 0
            try:
                os.close(read_end)
                null = os.open(os.devnull, os.O_RDWR)
                os.dup2(null, 0)  # it cannot read further requests
                os.dup2(null, 1)  # nor write to the main process except through its own pipe
                header["landlock"] = _restrict()
                with os.fdopen(write_end, "wb", buffering=_CHUNK) as fh:
                    _answer(header["job"], header, data, _Writer(fh))
            except BaseException:
                code = 70
            finally:
                os._exit(code)
        os.close(write_end)
        del data
        with os.fdopen(read_end, "rb", buffering=0) as pipe:
            while True:
                chunk = pipe.read(_CHUNK)
                if not chunk:
                    break
                out.frame(b"D", chunk)
        _pid, status, usage = os.wait4(pid, 0)
        report = {"exit": os.waitstatus_to_exitcode(status), "peak": _kib(usage.ru_maxrss), "base": base}
        out.frame(b"S", json.dumps(report).encode())
        out.flush()


# --------------------------------------------------------------------------
# The main-process side
# --------------------------------------------------------------------------

_BOOT = "import json, sys\nsys.path[:] = json.loads(sys.argv[1])\nfrom imgstrip.sandbox import _serve\n_serve()\n"
# Variables a Python and its libraries may need to start; everything else (a passphrase, a token) stays behind.
_KEEP_ENV = (
    "PATH",
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    "DYLD_FALLBACK_LIBRARY_PATH",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LIBHEIF_PLUGIN_PATH",
)


def _environment() -> dict[str, str]:
    env = {k: os.environ[k] for k in _KEEP_ENV if k in os.environ}
    # The decoders gain nothing from BLAS threads, and a single-threaded zygote forks cleanly.
    env.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
    return env


def _import_path() -> list[str]:
    return [os.path.abspath(p) for p in sys.path if p]


class _Process:
    """A zygote (or, without fork, a one-job process) and its pipes."""

    def __init__(self):
        if not sys.executable or getattr(sys, "frozen", False):
            # A frozen application's executable is the application itself, not a Python that runs -c.
            raise SandboxError(
                "the decoder sandbox cannot start (this Python has no interpreter executable to run it with); "
                "run with --no-sandbox (Options(sandbox=False)) to decode in this process"
            )
        kw: dict = {}
        if os.name == "posix":
            kw["start_new_session"] = True  # its own process group, so a stuck one can be killed with its worker
        else:  # pragma: no cover - Windows
            kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-I", "-c", _BOOT, json.dumps(_import_path())],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=_environment(),
                close_fds=True,
                **kw,
            )
        except OSError as exc:
            raise SandboxError(
                f"the decoder sandbox cannot start ({exc}); run with --no-sandbox to decode in this process"
            ) from None
        self.pid = os.getpid()  # the process that owns it
        self.writer = _Writer(self.proc.stdin)
        self.reader = _Reader(self.proc.stdout)
        timer = threading.Timer(_START_TIMEOUT, self.kill)
        timer.start()
        try:
            kind, length = self.reader.header()
            message = self.reader.read(length) if length < 4096 else b""
        except (EOFError, ValueError, OSError, struct.error):
            kind, message = b"F", b"it exited during start-up"
        finally:
            timer.cancel()
        if kind != b"Y":
            self.kill()
            raise SandboxError(
                f"the decoder sandbox cannot start ({message.decode('utf-8', 'replace')}); "
                "run with --no-sandbox to decode in this process"
            )
        self.one_shot = message != b"fork"

    def alive(self) -> bool:
        return self.proc.poll() is None

    def kill(self) -> None:
        try:
            if os.name == "posix":
                os.killpg(self.proc.pid, signal.SIGKILL)
            else:  # pragma: no cover - Windows
                self.proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def close(self) -> None:
        for fh in (self.proc.stdin, self.proc.stdout):
            try:
                fh.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.kill()
            self.proc.wait()


class _Pool:
    def __init__(self):
        self.lock = threading.Lock()
        self.idle: list[_Process] = []
        self.pid = os.getpid()

    def acquire(self) -> _Process:
        with self.lock:
            if self.pid != os.getpid():  # forked: those zygotes belong to the parent process
                self.idle, self.pid = [], os.getpid()
            while self.idle:
                proc = self.idle.pop()
                if proc.alive():
                    break
            else:
                proc = None
        proc = proc or _Process()
        if proc.one_shot:  # keep the next one starting while this one works
            threading.Thread(target=self._spare, daemon=True).start()
        return proc

    def _spare(self) -> None:
        try:
            proc = _Process()
        except StripError:
            return
        with self.lock:
            if len(self.idle) < 2:
                self.idle.append(proc)
                return
        proc.close()

    def release(self, proc: _Process) -> None:
        if proc.one_shot or proc.pid != os.getpid() or not proc.alive():
            proc.close()
            return
        with self.lock:
            if len(self.idle) < max(4, os.cpu_count() or 1):
                self.idle.append(proc)
                return
        proc.close()

    def close(self) -> None:
        with self.lock:
            idle, self.idle = self.idle, []
        for proc in idle:
            if proc.pid == os.getpid():
                proc.close()


_POOL = _Pool()
atexit.register(_POOL.close)
_LAST = threading.local()


def last_peak() -> int | None:
    """The most memory (bytes) the last worker of this thread needed beyond what it started with (the zygote's), or
    None when that is not known (no fork, or no job yet).  scripts/bench.py and the memory tests use it."""
    status = getattr(_LAST, "status", None) or {}
    peak, base = status.get("peak"), status.get("base")
    return max(0, peak - base) if isinstance(peak, int) and isinstance(base, int) else None


# What reading a malformed answer may raise; every one of them becomes a SandboxError (fuzz/targets.py checks that
# nothing else can come out of it).
_REFUSED = (EOFError, OSError, ValueError, TypeError, KeyError, RecursionError, struct.error, _Ended)


def _call(job: str, data: bytes, params: dict, budget: int, timeout: float = _TIMEOUT):
    """Run ``job`` on ``data`` in a fresh worker; return its answer, rebuilt and with every blob read."""
    proc = _POOL.acquire()
    expired = threading.Event()

    def stop():
        expired.set()
        proc.kill()

    timer = threading.Timer(timeout, stop)
    timer.start()
    reusable = False
    try:
        try:
            proc.writer.frame(b"R", json.dumps({**params, "job": job}).encode())
            proc.writer.frame(b"I", data)
            proc.writer.flush()
            stream = _Relayed(proc.reader)
            kind, length = _inner_header(stream)
            if length > _MAX_HEADER:
                raise ValueError("an answer too large")
            body = stream.read(length)
            if kind == b"X":
                failure = json.loads(body)
                stream.finish()
                reusable = True
                message = str(failure.get("message", ""))[:_MAX_TEXT]
                if failure.get("kind") == "strip":
                    raise StripError(message)
                if failure.get("kind") == "memory":
                    raise StripError("not enough memory to decode this image")
                raise StripError(f"cannot process image ({message})")
            if kind != b"H":
                raise ValueError(f"unexpected frame {kind!r}")
            blobs = _Blobs(stream, budget)
            value = _unpack(json.loads(body), blobs)
            if blobs.pending or _inner_header(stream)[0] != b"E":
                raise ValueError("more data than the answer declared")
            status = stream.finish()
            _LAST.status = status
            reusable = True
            if status.get("exit") != 0:
                raise ValueError(f"the worker exited with status {status.get('exit')}")
            return value
        except _Ended:
            reusable = True
            code = (stream.status or {}).get("exit")
            if code == 0:
                raise SandboxError("the decoder stopped before its answer was complete; nothing was written") from None
            reason = f"killed by signal {-code}" if isinstance(code, int) and code < 0 else f"exit status {code}"
            raise SandboxError(f"the decoder crashed on this file ({reason}); nothing was written") from None
        except StripError:
            raise
        except _REFUSED as exc:
            if expired.is_set():
                raise SandboxError(f"decoding took longer than {timeout:.0f} s; nothing was written") from None
            if isinstance(exc, EOFError):  # the process itself ended (a one-job process, which has no zygote)
                raise SandboxError(
                    "the decoder crashed on this file (its process ended); nothing was written"
                ) from None
            raise SandboxError(f"the decoder sandbox failed ({exc}); nothing was written") from None
    finally:
        timer.cancel()
        if reusable and not expired.is_set():
            _POOL.release(proc)
        else:
            proc.kill()
            proc.close()


# --------------------------------------------------------------------------
# Jobs, and what this side accepts from them
# --------------------------------------------------------------------------


def _limit(max_pixels: int, data: bytes) -> int:
    """The most a decode may send back: four times the pixel limit at 8 bytes a pixel, plus the input's size."""
    return 32 * max_pixels + 2 * len(data) + (64 << 20)


def decode(data: bytes, options) -> object:
    """The decode stage (core._Job.decode) of ``data``, run in a fresh worker, checked field by field."""
    from dataclasses import asdict

    params = {"options": {k: v for k, v in asdict(options).items() if k != "sandbox"}}
    decoded = _call("decode", data, params, _limit(options.max_pixels, data))
    _accept_decoded(decoded, options, data)
    return decoded


def _accept_decoded(decoded, options, data: bytes) -> None:
    """``_check_decoded``, with a field of the wrong type (which trips the checks themselves) refused too."""
    try:
        _check_decoded(decoded, options, data)
    except (TypeError, AttributeError, KeyError, ValueError, IndexError) as exc:
        raise _bad(f"malformed fields ({type(exc).__name__})") from None


def frames(data: bytes, max_pixels: int) -> list[np.ndarray]:
    value = _call("frames", data, {"max_pixels": max_pixels}, _limit(max_pixels, data))
    if not isinstance(value, list) or not value or not all(isinstance(a, np.ndarray) for a in value):
        raise _bad("something other than frames")
    _check_pixels(value, max_pixels)
    return value


def pixels(data: bytes, max_pixels: int) -> tuple[np.ndarray, str]:
    value = _call("pixels", data, {"max_pixels": max_pixels}, _limit(max_pixels, data))
    if not isinstance(value, dict) or set(value) != {"pixels", "mode"} or not isinstance(value["pixels"], np.ndarray):
        raise _bad("something other than pixels")
    _check_pixels([value["pixels"]], max_pixels)
    if value["mode"] not in _MODES:
        raise _bad("an unknown image mode")
    return value["pixels"], value["mode"]


def scan(data: bytes):
    from . import analysis, stego
    from .inspect import Finding

    value = _call("scan", data, {}, 64 << 20)
    if type(value) is not analysis.PixelScan or type(value.verdict) is not stego.Verdict:
        raise _bad("something other than a pixel scan")
    if not all(type(f) is Finding and _finding_ok(f) for f in value.findings):
        raise _bad("an impossible finding")
    v = value.verdict
    if v.state not in stego.STATES or v.level not in (None, stego.LIKELY, stego.POSSIBLE):
        raise _bad("an impossible verdict")
    return value


_FACTS = {
    "format": str,
    "default_output": str,
    "size": list,
    "megapixels": (int, float),
    "bytes": int,
    "mode": str,
    "frames": int,
    "orientation": int,
    "profile": bool,
    "alpha": bool,
}


def facts(data: bytes):
    """The web app's header probe and facts (``_probe`` and ``image_facts``), read in a worker."""
    from .core import OUTPUT_FORMATS

    value = _call("facts", data, {}, 1 << 20)
    if not isinstance(value, dict) or set(value) != {"probe", "facts"}:
        raise _bad("something other than facts")
    probe, found = value["probe"], value["facts"]
    if not (isinstance(probe, list) and len(probe) == 2):
        raise _bad("an impossible probe")
    fmt, size = probe
    if not (fmt is None or (isinstance(fmt, str) and len(fmt) <= 16)):
        raise _bad("an impossible format")
    if size is not None:
        if not (_ints(size, 0, 1 << 31) and len(size) == 2):
            raise _bad("an impossible size")
        size = tuple(size)
    if found is not None:
        if not isinstance(found, dict) or set(found) != set(_FACTS):
            raise _bad("impossible facts")
        if not all(isinstance(found[k], t) and (t is bool or type(found[k]) is not bool) for k, t in _FACTS.items()):
            raise _bad("impossible facts")
        if found["default_output"] not in OUTPUT_FORMATS or not 1 <= found["orientation"] <= 8:
            raise _bad("impossible facts")
        if not (_ints(found["size"], 0, 1 << 31) and len(found["size"]) == 2) or found["bytes"] != len(data):
            raise _bad("impossible facts")
        if max(len(found["format"]), len(found["mode"])) > 16 or found["frames"] < 1:
            raise _bad("impossible facts")
    return (fmt, size), found


def preview(data: bytes, edge: int, limit: int) -> np.ndarray:
    """The pixels of the web app's preview (``preview_pixels_here``), decoded in a worker."""
    value = _call("preview", data, {"edge": int(edge), "limit": int(limit)}, 4 * edge * edge + (1 << 16))
    if not isinstance(value, np.ndarray) or value.dtype != np.uint8 or value.ndim != 3 or value.shape[2] not in (3, 4):
        raise _bad("something other than a preview")
    if max(value.shape[:2]) > edge or min(value.shape[:2]) < 1:
        raise _bad("a preview larger than asked for")
    return value


def probe_limits(directory: str, victim: str, port: int | None = None) -> dict:
    """What a worker may still do, for the tests and the self-test: write a file in ``directory``, create one, delete
    the file ``victim``, connect to ``port`` on the loopback interface."""
    return _call("_limits", b"", {"dir": directory, "victim": victim, "port": port, "target": os.getpid()}, 1 << 16)


def _bad(why: str) -> SandboxError:
    return SandboxError(f"the decoder sandbox answered {why}; nothing was written")


def _finding_ok(f) -> bool:
    from .inspect import COLOR, HIDDEN, METADATA, STRUCTURE

    return (
        f.category in (METADATA, HIDDEN, COLOR, STRUCTURE)
        and isinstance(f.label, str)
        and isinstance(f.detail, str)
        and len(f.label) <= 400
    )


def _check_pixels(arrays: list[np.ndarray], max_pixels: int) -> None:
    total = 0
    for a in arrays:
        if a.ndim not in (2, 3) or (a.ndim == 3 and not 1 <= a.shape[2] <= 4):
            raise _bad("an impossible pixel layout")
        total += a.shape[0] * a.shape[1]
        if a.shape[0] * a.shape[1] > max_pixels:
            raise _bad("a frame above the pixel limit")
    if total > 4 * max_pixels:
        raise _bad("frames above the pixel limit")


def _check_decoded(d, options, data: bytes) -> None:
    """Accept only what the decode stage can legitimately hand over (see core.Decoded)."""
    from PIL import Image

    from . import dct, icc
    from .core import INPUT_FORMATS, OUTPUT_FORMATS, Decoded, Frame, _Job, canonical_format
    from .inspect import Report

    if type(d) is not Decoded or d.kind not in ("pixels", "jpeg"):
        raise _bad("something other than a decoded image")
    r = d.report
    if type(r) is not Report or not (r.format is None or isinstance(r.format, str)):
        raise _bad("an impossible inspection report")
    if not all(_finding_ok(f) for f in r.findings) or not (r.error is None or isinstance(r.error, str)):
        raise _bad("an impossible finding")
    if set(r.info) - set(_REPORT_INFO):
        raise _bad("more than the report may carry")
    if "lossless" in r.info and not isinstance(r.info["lossless"], bool):
        raise _bad("an impossible report")
    if "chroma" in r.info and r.info["chroma"] not in ("4:2:0", "4:2:2", "4:4:4"):
        raise _bad("an impossible report")
    if "size" in r.info and not (_ints(r.info["size"], 0, 1 << 31) and len(r.info["size"]) == 2):
        raise _bad("an impossible report")
    if d.src not in {canonical_format(f) for f in INPUT_FORMATS} or d.out not in OUTPUT_FORMATS:
        raise _bad("an impossible format")
    # The output format follows from the options and the input format alone, so this side works it out again.
    if _Job(b"", options, None)._output_format(d.src) != d.out:
        raise _bad("an output format the options do not ask for")
    for texts in (d.actions, d.warnings, d.decoder_warnings):
        if not isinstance(texts, list) or not all(isinstance(s, str) for s in texts):
            raise _bad("impossible messages")
    if not isinstance(d.exact, bool):
        raise _bad("an impossible flag")
    for profile in (d.embed_icc, d.icc_out):
        if profile is not None and not (isinstance(profile, bytes) and icc.is_sanitized(profile)):
            raise _bad("a colour profile that is not one imgstrip rebuilds")
    if d.kind == "pixels":
        if not d.frames or not all(type(f) is Frame for f in d.frames) or d.rebuilt or d.original is not None:
            raise _bad("impossible frames")
        if d.loop is not None and not (type(d.loop) is int and 0 <= d.loop <= 0xFFFF):
            raise _bad("an impossible loop count")
        if d.source_subsampling not in (None, 0, 1, 2):
            raise _bad("an impossible chroma subsampling")
        total = 0
        for f in d.frames:
            if (f.image is None) == (f.array is None):
                raise _bad("a frame with no pixels, or two sets")
            if f.array is not None:
                a = f.array
                if a.dtype != np.uint16 or a.ndim != 3 or not 1 <= a.shape[2] <= 4 or f.alpha or f.key is not None:
                    raise _bad("an impossible 16-bit frame")
                w, h = a.shape[1], a.shape[0]
            else:
                if not isinstance(f.image, Image.Image) or f.image.mode not in _MODES:
                    raise _bad("an impossible image mode")
                w, h = f.image.size
                if f.alpha is not None and not (f.image.mode == "P" and _ints(f.alpha, 0, 255) and len(f.alpha) <= 256):
                    raise _bad("an impossible palette transparency")
                if f.key is not None and not (f.image.mode == "I;16" and type(f.key) is int and 0 <= f.key <= 0xFFFF):
                    raise _bad("an impossible colour key")
            if f.duration is not None and not (isinstance(f.duration, (int, float)) and 0 <= f.duration <= 1e9):
                raise _bad("an impossible frame duration")
            if w * h > options.max_pixels:
                raise _bad("a frame above the pixel limit")
            total += w * h
        if total > 4 * options.max_pixels or len({f.size for f in d.frames}) != 1:
            raise _bad("frames that do not make one picture")
        return
    # kind "jpeg": only the JPEG modes produce it
    if d.src != "JPEG" or d.out != "JPEG" or d.frames:
        raise _bad("an impossible JPEG result")
    if not (type(d.orientation) is int and 1 <= d.orientation <= 8) or not isinstance(d.had_jfif, bool):
        raise _bad("an impossible orientation")
    if d.adobe_transform is not None and not (type(d.adobe_transform) is int and 0 <= d.adobe_transform <= 255):
        raise _bad("an impossible colour transform")
    if not (type(d.dropped) is int and d.dropped >= 0):
        raise _bad("an impossible count")
    if (
        not isinstance(d.original, np.ndarray)
        or d.original.dtype != np.uint8
        or d.original_mode not in ("L", "RGB", "CMYK")
    ):
        raise _bad("impossible original pixels")
    _check_pixels([d.original], options.max_pixels)
    from .core import dct_mode

    mode = dct_mode(options)
    if (mode == "off") != (d.rebuilt is not None) or (d.rebuilt is not None and not isinstance(d.rebuilt, bytes)):
        raise _bad("a rebuilt file where none belongs")
    if d.rebuilt is not None and len(d.rebuilt) > len(data) + (32 << 20):
        raise _bad("a rebuilt file larger than its input")
    frame = d.coefficients
    if frame is None:
        if mode != "off":
            raise _bad("no coefficients")
        return
    if type(frame) is not dct.Frame or not 1 <= len(frame.comps) <= 4:
        raise _bad("impossible coefficients")
    if not all(type(c) is dct.Component for c in frame.comps):
        raise _bad("impossible coefficients")
    if not (
        type(frame.width) is int and type(frame.height) is int and frame.width * frame.height <= options.max_pixels
    ):
        raise _bad("coefficients above the pixel limit")
    if frame.width < 1 or frame.height < 1:
        raise _bad("impossible coefficients")
    hmax = max(c.h for c in frame.comps)
    vmax = max(c.v for c in frame.comps)
    if frame.mcus_x != -(-frame.width // (8 * hmax)) or frame.mcus_y != -(-frame.height // (8 * vmax)):
        raise _bad("an impossible block grid")
    for c in frame.comps:
        if not (type(c.h) is int and type(c.v) is int and 1 <= c.h <= 4 and 1 <= c.v <= 4):
            raise _bad("impossible sampling factors")
        if c.coef.dtype != np.int16 or c.coef.shape != (frame.mcus_y * c.v, frame.mcus_x * c.h, 64):
            raise _bad("an impossible coefficient array")
        if c.qt.dtype != np.uint16 or c.qt.shape != (64,) or not c.qt.all():
            raise _bad("an impossible quantisation table")
        cw = -(-frame.width * c.h // hmax)
        ch = -(-frame.height * c.v // vmax)
        if c.blocks_w != -(-cw // 8) or c.blocks_h != -(-ch // 8):
            raise _bad("an impossible block count")
