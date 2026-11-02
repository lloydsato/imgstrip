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
* For every job the zygote forks a fresh *worker*.  Before it touches the input the worker confines itself (``_restrict``):
  no core dump, no file it can write to (``RLIMIT_FSIZE`` 0), CPU-time and data-size limits, the first pick of the
  out-of-memory killer, no new privileges and death with its zygote; and on Linux two filters that no code in the
  worker can lift again.  Landlock lets it *read* only the Python installation, the libraries it has mapped and a few
  device and information files, and write nothing; a seccomp filter (``_seccomp``) refuses sockets, new processes,
  signals to others, ``ptrace``, ``io_uring``, ``bpf``, ``memfd_create`` and every call that opens a file for writing,
  creates, removes, renames or truncates one.  It cannot read further requests or write to the main process except
  through the pipe of its own job, which the zygote relays in framed chunks followed by the worker's exit status, so a
  worker that dies half-way is noticed, never misread.
* What the worker could see of other files is kept to a minimum.  The zygote reads a job's input into a buffer it wipes
  when the job ends, relays the answer through another buffer it wipes too, and retires itself (the main process
  starts a new one) after any job whose worker did not exit cleanly and after ``_MAX_JOBS`` jobs.  A decoder that has
  been exploited therefore finds in its parent's memory, at most, what an earlier worker left in memory the wipe did
  not reach (the allocator's free lists and the Python objects of the zygote's own start-up), never an earlier file's
  bytes.
* There is one zygote per job running at the same time (the web app works on several at once); idle ones are kept.
  On Windows, which cannot fork, every job gets a fresh process instead, and one is kept started in advance.

What each platform gets (``status()`` reports it; ``--require-sandbox`` refuses to run with less than the first):

* Linux with Landlock and seccomp (kernel 5.13 or later on x86-64 or arm64): the full confinement above.
* Linux without one of them, macOS, Windows: the separate process and the resource limits, nothing more.  A decoder
  exploited to run code could then read the user's files and use the network with the user's rights.  imgstrip warns
  once on standard error when this is the case.  (macOS's Seatbelt is not used: the profile language is undocumented
  and could not be tested for this release.)

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
_REQUIRE = False
_TIMEOUT = 900.0  # wall-clock seconds for one job
_CPU_SECONDS = 900  # the worker's CPU-time limit
_START_TIMEOUT = 120.0
_CHUNK = 1 << 20  # bytes per relayed chunk
_STRIP = 1 << 22  # bytes per strip of an image sent in pieces
_MAX_HEADER = 64 << 20  # the JSON part of a response
_MAX_ITEMS = 100_000  # list lengths in a response (findings, actions, frames, palette entries)
_MAX_TEXT = 1 << 16  # characters in one string
_MAX_JOBS = 64  # jobs one zygote serves before it is replaced
_BASE_SECONDS, _SECONDS_PER_MP, _SECONDS_PER_MB = 60.0, 2.0, 5.0  # the wall-clock allowance grows with the work
_BYTES_PER_PIXEL = 64  # data-size limit of a worker: this much per pixel of the limit (the peak is under 44), plus:
_MEMORY_SLACK = 512 << 20  # ... this, and four times the input


def harden_process() -> None:
    """Make this process harder to read for other processes of the same user: no core dump, and (Linux) not
    dumpable, which closes ``ptrace`` and ``/proc/PID/mem`` to them whatever the system's ptrace policy.  Called by
    the web app and whenever a signing key is loaded."""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):  # pragma: no cover - Windows
        pass
    if sys.platform.startswith("linux"):
        try:
            import ctypes

            ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE
        except (OSError, AttributeError):
            pass


def set_default(on: bool) -> None:
    """Switch the sandbox on or off for this process (``--no-sandbox`` switches it off)."""
    global _DEFAULT
    _DEFAULT = bool(on)


def set_require(on: bool) -> None:
    """Refuse to decode unless the worker could confine itself completely (``--require-sandbox``)."""
    global _REQUIRE
    _REQUIRE = bool(on)


def required() -> bool:
    return _REQUIRE or os.environ.get("IMGSTRIP_REQUIRE_SANDBOX", "") not in ("", "0")


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


class _Raw:
    """Writes to a file descriptor without a buffer of its own."""

    def __init__(self, fd: int):
        self.fd = fd

    def write(self, data) -> None:
        view = memoryview(data).cast("B")
        while len(view):
            view = view[os.write(self.fd, view) :]

    def flush(self) -> None:
        pass


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


def _vm_data() -> int:
    """This process's data segment size now (Linux, bytes): what ``RLIMIT_DATA`` counts."""
    try:
        with open("/proc/self/status", "rb") as fh:
            for line in fh:
                if line.startswith(b"VmData:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _tier(landlock: int = 0, reads: bool = False, seccomp: bool = False) -> dict:
    full = landlock >= 1 and reads and seccomp
    return {"landlock": int(landlock), "reads": bool(reads), "seccomp": bool(seccomp), "full": full}


def _restrict(roots=(), memory: int = 0) -> dict:
    """Limits a worker sets on itself before it reads the input (POSIX).  Returns the confinement it got (``_tier``).

    ``roots`` are the paths the worker may read (``_read_roots``); ``memory`` is the data-size limit to set, in bytes
    above what the process holds now (0: none)."""
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return _tier()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # a write past the limit fails with EFBIG instead of killing
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    _soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
    cpu = _CPU_SECONDS if hard == resource.RLIM_INFINITY else min(_CPU_SECONDS, hard)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, hard))
    if memory > 0 and sys.platform.startswith("linux"):
        # Private writable memory (the heap and anonymous maps) may not grow past the start by more than the job needs.
        limit = _vm_data() + memory
        _soft, hard = resource.getrlimit(resource.RLIMIT_DATA)
        resource.setrlimit(resource.RLIMIT_DATA, (limit if hard == resource.RLIM_INFINITY else min(limit, hard), hard))
    if not sys.platform.startswith("linux"):
        return _tier()
    try:
        with open("/proc/self/oom_score_adj", "w") as fh:  # the first the out-of-memory killer takes, not the user's work
            fh.write("1000")
    except OSError:
        pass
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(38, 1, 0, 0, 0)  # PR_SET_NO_NEW_PRIVS (Landlock and seccomp need it)
        libc.prctl(1, signal.SIGKILL, 0, 0, 0)  # PR_SET_PDEATHSIG: die with the zygote
        libc.prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE 0: no ptrace by other processes of the user, no core file
        abi, reads = _landlock(libc, roots)
        from . import _seccomp

        return _tier(abi, reads, _seccomp.install(libc, os.getpid()))
    except (OSError, AttributeError):
        return _tier()


# Landlock (Linux 5.13 and later): rights the worker gives up for good, everywhere.  It may read only the paths it is
# given (``_read_roots``); it may change nothing in the file system, execute a program or use a device.  With ABI 4 it
# also gives up TCP, and with ABI 6 signals and abstract sockets outside itself.  A Landlock domain also stops it from
# attaching to (ptrace), or reading the memory of, any process outside it.
_LL_FS = {1: 0x1FFF, 2: 0x3FFF, 3: 0x7FFF, 5: 0xFFFF}  # every access right of each ABI generation
_LL_READ = 0xC  # READ_FILE (4) | READ_DIR (8)
_LL_NET = 0x3  # BIND_TCP | CONNECT_TCP
_LL_SCOPE = 0x3  # ABSTRACT_UNIX_SOCKET | SIGNAL
_SYS_LANDLOCK = (444, 445, 446)  # landlock_create_ruleset, _add_rule, _restrict_self (the same on every architecture)


def _landlock(libc, roots=()) -> tuple[int, bool]:
    """Apply Landlock to this thread and what it starts.  Returns (ABI version applied or 0, whether reading is
    limited to ``roots``)."""
    import ctypes
    import stat

    libc.syscall.restype = ctypes.c_long
    abi = libc.syscall(_SYS_LANDLOCK[0], None, ctypes.c_size_t(0), ctypes.c_uint32(1))  # ..._VERSION
    if abi < 1:
        return 0, False

    class PathBeneath(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    def create(fs: int) -> int:
        attr = (ctypes.c_uint64 * 3)(fs, _LL_NET if abi >= 4 else 0, _LL_SCOPE if abi >= 6 else 0)
        size = 8 if abi < 4 else 16 if abi < 6 else 24
        return int(libc.syscall(_SYS_LANDLOCK[0], ctypes.byref(attr), ctypes.c_size_t(size), ctypes.c_uint32(0)))

    everything = _LL_FS[max(k for k in _LL_FS if k <= abi)]
    ruleset, reads = create(everything), True
    if ruleset < 0:
        return 0, False
    allowed = 0
    for path in roots:
        try:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            access = _LL_READ if stat.S_ISDIR(os.fstat(fd).st_mode) else _LL_READ & 4  # a file rule takes file rights
            rule = PathBeneath(access, fd)
            if libc.syscall(_SYS_LANDLOCK[1], ctypes.c_int(ruleset), ctypes.c_int(1), ctypes.byref(rule), 0) == 0:
                allowed += 1
        except OSError:
            pass
        finally:
            os.close(fd)
    if not allowed:  # nothing could be allowed: confine writing only, as before, rather than a worker that reads nothing
        os.close(ruleset)
        ruleset, reads = create(everything & ~_LL_READ), False
        if ruleset < 0:
            return 0, False
    try:
        if libc.syscall(_SYS_LANDLOCK[2], ctypes.c_int(ruleset), ctypes.c_uint32(0)) != 0:
            return 0, False
    finally:
        os.close(ruleset)
    return int(abi), reads


# What a worker may read (besides its own /proc/self): the Python installation and the places it imports from, the
# directories of every library mapped into the zygote, the few files below, and what the CPU and time-zone queries
# of the libraries read.  Directories that would take in other people's files, or the user's home, are never allowed.
_READ_FILES = ("/dev/null", "/dev/zero", "/dev/urandom", "/dev/random", "/etc/localtime", "/etc/ld.so.cache")
_READ_TREES = (
    "/proc/self",
    "/proc/cpuinfo",
    "/proc/meminfo",
    "/proc/stat",
    "/sys/devices/system/cpu",
    "/sys/fs/cgroup",
    "/usr/share/zoneinfo",
)
_NEVER = ("/", "/tmp", "/var", "/var/tmp", "/home", "/root", "/etc", "/mnt", "/media", "/run", "/proc", "/sys", "/dev", "/srv")
_READ_ROOTS: list[str] = []


def _read_roots() -> list[str]:
    """Paths the zygote lets its workers read, worked out once after everything is imported."""
    import sysconfig

    found: set[str] = set()
    for p in (sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix, *sys.path):
        if p:
            found.add(p)
    found.update(v for v in sysconfig.get_paths().values() if v)
    try:
        with open("/proc/self/maps", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split(None, 5)
                if len(parts) == 6 and parts[5].startswith("/"):
                    found.add(os.path.dirname(parts[5].removesuffix(" (deleted)\n").rstrip("\n")))
    except OSError:
        pass
    for var in ("LD_LIBRARY_PATH", "LIBHEIF_PLUGIN_PATH"):
        found.update(p for p in os.environ.get(var, "").split(os.pathsep) if p)
    home = os.path.realpath(os.path.expanduser("~"))
    roots = []
    for p in sorted(found):
        try:
            real = os.path.realpath(p)
        except OSError:
            continue
        if real in _NEVER or home == real or home.startswith(real.rstrip("/") + "/") or not os.path.exists(real):
            continue  # too broad: the home directory is inside it
        roots.append(real)
    roots.extend(p for p in (*_READ_FILES, *_READ_TREES) if os.path.exists(p))
    return roots


def _probe(params: dict) -> dict:
    """Try, from inside a worker, what a hostile decoder would: the answers go to ``probe_limits``."""
    import mmap
    import socket

    try:
        import resource

        limit = lambda which: list(resource.getrlimit(which))
        fsize, core_, cpu, data = (
            limit(r) for r in (resource.RLIMIT_FSIZE, resource.RLIMIT_CORE, resource.RLIMIT_CPU, resource.RLIMIT_DATA)
        )
    except ImportError:  # pragma: no cover - Windows: a process of its own, and nothing more
        fsize = core_ = cpu = data = None

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

    def environ():  # the main process's environment (a passphrase, a token)
        with open(f"/proc/{params['target']}/environ", "rb") as fh:
            fh.read(1)

    def connect():
        with socket.create_connection(("127.0.0.1", params["port"]), timeout=5):
            pass

    def datagram():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(b"x", ("127.0.0.1", params["port"]))

    def local_socket():  # a Unix socket of the user's desktop session, say
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(params["unix"])

    def netlink():
        socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0).close()

    def read_secret():  # any file of the user's outside the Python installation
        with open(params["secret"], "rb") as fh:
            fh.read(1)

    def map_write():  # RLIMIT_FSIZE stops write(2) but not a store through a writable mapping of a file
        fd = os.open(params["victim"], os.O_RDWR)
        try:
            mmap.mmap(fd, 1)[0:1] = b"x"
        finally:
            os.close(fd)

    def spawn():
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)

    def signal_parent():
        os.kill(params["target"], 0)

    def anonymous_file():
        os.close(os.memfd_create("x"))

    def optional(key, step):
        return attempt(step) if params.get(key) else "not tried"

    def oom_adj():
        try:
            with open("/proc/self/oom_score_adj", "rb") as fh:
                return int(fh.read())
        except (OSError, ValueError):
            return None

    return {
        "write": attempt(write),
        "create": attempt(lambda: os.close(os.open(os.path.join(params["dir"], "created"), os.O_CREAT | os.O_WRONLY))),
        "map_write": optional("victim", map_write),
        "remove": attempt(lambda: os.unlink(params["victim"])),
        "connect": optional("port", connect),
        "datagram": optional("port", datagram),
        "unix": optional("unix", local_socket),
        "netlink": attempt(netlink),
        "read": optional("secret", read_secret),
        "fork": attempt(spawn),
        "signal": optional("target", signal_parent),
        "memfd": attempt(anonymous_file) if hasattr(os, "memfd_create") else "no (not here)",
        "peek": optional("target", peek),
        "environ": optional("target", environ),
        "tier": params.get("tier"),
        "fsize": fsize,
        "core": core_,
        "cpu": cpu,
        "data": data,
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "oom_score_adj": oom_adj(),
        "environment": sorted(os.environ),
    }


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
    if name == "inspect":
        from .inspect import inspect_bytes

        report = inspect_bytes(data)
        report.info = {k: v for k, v in report.info.items() if k in _INSPECT_INFO}
        return report
    if name == "credential":
        import base64

        from . import credentials

        anchors = base64.b64decode(params["anchors"]) if params.get("anchors") else None
        try:
            return {"credential": credentials.read_credential(data, anchors).as_dict()}
        except credentials.CredentialsUnavailable as exc:
            return {"unavailable": str(exc)[:_MAX_TEXT]}
        except credentials.CredentialError as exc:
            return {"error": str(exc)[:_MAX_TEXT]}
    if name in ("facts", "preview"):
        from .web import server

        if name == "facts":
            return {"probe": list(server._probe_here(data)), "facts": server.image_facts_here(data)}
        return server.preview_pixels_here(data, int(params["edge"]), int(params["limit"]))
    if name == "_tier":  # what this worker got (also sent with every answer)
        return params["tier"]
    if name == "_limits":  # for the tests and the self-test: what a worker may still do
        return _probe(params)
    if name == "_echo":  # for the tests: the input, handed back
        return data
    if name == "_alloc":  # for the tests: take (and touch) this much memory
        block = bytearray(int(params["bytes"]))
        block[::4096] = b"x" * len(block[::4096])
        return len(block)
    if name == "_crash":
        os.abort()
    if name == "_hang":
        import time

        time.sleep(3600)
    if name == "_lie":  # an answer that breaks the rules, to test this side's checks (see _answer)
        return None
    raise StripError(f"unknown sandbox job {name!r}")


_REPORT_INFO = ("lossless", "chroma", "size")
_INSPECT_INFO = ("size",)  # what the callers of ``inspect`` use of a report's ``info``


def describe(tier: dict) -> str:
    """A tier in words, for messages."""
    parts = [
        f"Landlock {'ABI ' + str(tier['landlock']) if tier.get('landlock') else 'not available'}",
        "reads limited to the Python installation" if tier.get("reads") else "reads not limited",
        "system-call filter on" if tier.get("seccomp") else "no system-call filter",
    ]
    return ", ".join(parts)


def _answer(name: str, params: dict, data: bytes, out: _Writer) -> None:
    if name == "_lie":  # the frames the test asked for, exactly
        for kind, payload in params["frames"]:
            out.frame(kind.encode("ascii"), payload.encode("latin-1"))
        return
    try:
        tier = params.get("tier")
        if tier is not None:  # what this worker got, before anything of the input is looked at
            out.frame(b"T", json.dumps(tier).encode())
            if params.get("require") and not tier["full"]:
                raise StripError(
                    f"the decoder sandbox could not confine its worker completely ({describe(tier)}) and "
                    "--require-sandbox (IMGSTRIP_REQUIRE_SANDBOX) is set; nothing was decoded"
                )
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

    from . import analysis, codecs, core, credentials, dct, icc, inspect, jpeg, prnu, stego  # noqa: F401
    from .web import server  # noqa: F401

    Image.init()
    core._register_plugins()
    try:
        credentials._sdk()  # the C2PA library, if installed: loaded now so that its files are among the readable ones
        credentials._crypto()
    except Exception:  # not installed: the credential job then reports that
        pass
    global _READ_ROOTS
    _READ_ROOTS = _read_roots()


def _wipe(buf: bytearray) -> None:
    """Overwrite a buffer with zeros (what a job's input and answer pass through must not outlive the job)."""
    import ctypes

    if len(buf):
        view = (ctypes.c_char * len(buf)).from_buffer(buf)
        ctypes.memset(view, 0, len(buf))
        del view


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


def set_accepted(formats) -> None:
    """Have the decoder open only ``formats`` (--accept).  Zygotes already running are replaced."""
    global _ACCEPTED
    _ACCEPTED = None if formats is None else sorted(formats)
    _POOL.close()


def _serve(accept=None) -> None:
    """The zygote: wait for requests on stdin, fork a worker for each, relay its answer on stdout."""
    sys.dont_write_bytecode = True
    # Unbuffered, so no copy of a request's bytes or of an answer's stays behind in a stream buffer.
    stdin, stdout = open(0, "rb", buffering=0, closefd=False), _Raw(1)
    out = _Writer(stdout)
    try:
        _preload()
        if accept is not None:
            from . import core

            core.set_accepted(accept)
    except BaseException as exc:
        out.frame(b"F", f"{type(exc).__name__}: {exc}".encode())
        out.flush()
        return
    fork = hasattr(os, "fork")
    out.frame(b"Y", b"fork" if fork else b"once")
    out.flush()
    requests = _Reader(stdin)
    relay = bytearray(_CHUNK)  # the answer passes through here, and is wiped after every job
    jobs = 0
    while True:
        try:
            kind, length = requests.header()
            if kind != b"R" or length > _MAX_HEADER:
                return
            header = json.loads(requests.read(length))
            kind, length = requests.header()
            if kind != b"I":
                return
            data = bytearray(length)  # wiped when the job is over
            requests.readinto(memoryview(data))
        except (EOFError, ValueError, struct.error):
            return
        if not fork:
            try:
                _answer(header["job"], header, bytes(data), _Writer(_Chunked(out)))
            finally:
                _wipe(data)
            out.frame(b"S", json.dumps({"exit": 0, "retire": True}).encode())
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
                header["tier"] = _restrict(_READ_ROOTS, int(header.get("memory", 0)))
                with os.fdopen(write_end, "wb", buffering=_CHUNK) as fh:
                    _answer(header["job"], header, bytes(data), _Writer(fh))
            except BaseException:
                code = 70
            finally:
                os._exit(code)
        os.close(write_end)
        with os.fdopen(read_end, "rb", buffering=0) as pipe:
            view = memoryview(relay)
            while True:
                n = pipe.readinto(relay)
                if not n:
                    break
                out.frame(b"D", view[:n])
            view.release()
        _pid, status, usage = os.wait4(pid, 0)
        code = os.waitstatus_to_exitcode(status)
        jobs += 1
        # A worker that did not end cleanly may have been exploited; a zygote that has served enough files is replaced
        # too.  Either way this one ends after reporting, and the main process starts another.
        retire = code != 0 or jobs >= _MAX_JOBS
        report = {"exit": code, "peak": _kib(usage.ru_maxrss), "base": base, "retire": retire}
        out.frame(b"S", json.dumps(report).encode())
        out.flush()
        _wipe(relay)
        _wipe(data)
        del data
        if retire:
            return


# --------------------------------------------------------------------------
# The main-process side
# --------------------------------------------------------------------------

_BOOT = (
    "import json, sys\nsys.path[:] = json.loads(sys.argv[1])\nfrom imgstrip.sandbox import _serve\n"
    "_serve(json.loads(sys.argv[2]))\n"
)
_ACCEPTED: list[str] | None = None  # --accept: the formats the zygotes may open
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
                [sys.executable, "-I", "-c", _BOOT, json.dumps(_import_path()), json.dumps(_ACCEPTED)],
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
        self.retired = False  # it has said it ends after the job that is running
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
        if proc.retired and not proc.one_shot and proc.pid == os.getpid():
            proc.close()  # worn out, or its last worker did not end cleanly: a new one takes its place
            threading.Thread(target=self._spare, daemon=True).start()
            return
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


_TIER: dict | None = None  # the confinement the last worker reported
_ANNOUNCE = False
_ANNOUNCED = False


def announce_degraded(on: bool = True) -> None:
    """Have the first worker that gets less than the full confinement say so once on standard error (the command
    line tools switch this on; a library user reads ``status()``)."""
    global _ANNOUNCE
    _ANNOUNCE = bool(on)


def _note_tier(body: bytes) -> None:
    global _TIER, _ANNOUNCED
    tier = json.loads(body)
    if not (
        isinstance(tier, dict)
        and set(tier) == {"landlock", "reads", "seccomp", "full"}
        and type(tier["landlock"]) is int
        and all(type(tier[k]) is bool for k in ("reads", "seccomp", "full"))
    ):
        raise ValueError("an impossible confinement report")
    _TIER = tier
    if _ANNOUNCE and not _ANNOUNCED and not tier["full"]:
        _ANNOUNCED = True
        sys.stderr.write(
            f"imgstrip: warning: the decoder sandbox runs with reduced confinement here ({describe(tier)}); a decoder "
            "exploited by a hostile file could read your files or use the network.  --require-sandbox refuses instead.\n"
        )
        sys.stderr.flush()


def _pixels_of(params: dict) -> int:
    from . import core

    value = params.get("max_pixels") or (params.get("options") or {}).get("max_pixels") or core.DEFAULT_MAX_PIXELS
    return int(value) if isinstance(value, (int, float)) and value > 0 else core.DEFAULT_MAX_PIXELS


def _deadline(max_pixels: int, size: int) -> float:
    """Wall-clock seconds one job may take: a minute, plus time for the picture and the file (never above ``_TIMEOUT``)."""
    return min(_TIMEOUT, _BASE_SECONDS + _SECONDS_PER_MP * max_pixels / 1e6 + _SECONDS_PER_MB * size / (1 << 20))


def _call(job: str, data: bytes, params: dict, budget: int, timeout: float | None = None):
    """Run ``job`` on ``data`` in a fresh worker; return its answer, rebuilt and with every blob read."""
    proc = _POOL.acquire()
    expired = threading.Event()
    max_pixels = _pixels_of(params)
    if timeout is None:
        timeout = _deadline(max_pixels, len(data))
    if not job.startswith("_"):
        params = {"memory": _BYTES_PER_PIXEL * max_pixels + _MEMORY_SLACK + 4 * len(data), **params}

    def stop():
        expired.set()
        proc.kill()

    timer = threading.Timer(timeout, stop)
    timer.start()
    reusable = False
    try:
        try:
            proc.writer.frame(b"R", json.dumps({"require": required(), **params, "job": job}).encode())
            proc.writer.frame(b"I", data)
            proc.writer.flush()
            stream = _Relayed(proc.reader)
            kind, length = _inner_header(stream)
            if kind == b"T" and length <= 4096:
                _note_tier(stream.read(length))
                kind, length = _inner_header(stream)
            if length > _MAX_HEADER:
                raise ValueError("an answer too large")
            body = stream.read(length)
            if kind == b"X":
                failure = json.loads(body)
                proc.retired = bool(stream.finish().get("retire"))
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
            proc.retired = bool(status.get("retire"))
            reusable = True
            if status.get("exit") != 0:
                raise ValueError(f"the worker exited with status {status.get('exit')}")
            return value
        except _Ended:
            reusable = True
            proc.retired = bool((stream.status or {}).get("retire"))
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


_STATUS: dict | None = None


def status(refresh: bool = False) -> dict:
    """What decoding is confined by on this machine: ``{"enabled", "full", "landlock", "reads", "seccomp"}``.

    ``full`` is true when a worker is confined completely (Landlock for reading and writing, and the system-call
    filter); anything less is what ``--require-sandbox`` refuses.  The first call starts a worker to find out."""
    global _STATUS
    if not enabled():
        return {"enabled": False, "full": False, "landlock": 0, "reads": False, "seccomp": False}
    if _STATUS is None or refresh:
        try:
            tier = _call("_tier", b"", {"require": False}, 1 << 16)
        except StripError:
            tier = _TIER
        _STATUS = {"enabled": True, **(tier or _tier())}
    return dict(_STATUS)


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


_FORMATS = {"JPEG", "PNG", "WEBP", "GIF", "TIFF", "BMP", "AVIF", "HEIF"}


def inspect(data: bytes):
    """``inspect_bytes`` of ``data``, run in a worker, checked field by field: a Report with the format, the findings,
    the error and (when known) the size, nothing else."""
    return _validated(_accept_report, _call("inspect", data, {}, 1 << 22))


def _validated(check, *args):
    """``check(*args)`` with a field of the wrong type (which trips the checks themselves) refused like any other lie."""
    try:
        return check(*args)
    except (TypeError, AttributeError, KeyError, ValueError, IndexError) as exc:
        raise _bad(f"malformed fields ({type(exc).__name__})") from None


def _accept_report(value):
    from .inspect import Finding, Report

    if type(value) is not Report or not (value.format is None or value.format in _FORMATS):
        raise _bad("something other than an inspection report")
    if not isinstance(value.findings, list) or not all(type(f) is Finding and _finding_ok(f) for f in value.findings):
        raise _bad("an impossible finding")
    if not (value.error is None or (isinstance(value.error, str) and len(value.error) <= _MAX_TEXT)):
        raise _bad("an impossible error")
    if not isinstance(value.info, dict) or set(value.info) - set(_INSPECT_INFO):
        raise _bad("more than the report may carry")
    size = value.info.get("size")
    if size is not None:
        if not (_ints(size, 0, 1 << 31) and len(size) == 2):
            raise _bad("an impossible size")
        value.info["size"] = tuple(size)
    return value


def _json_ok(value, depth: int = 0) -> bool:
    """Plain JSON data of bounded depth and size (the assertion of a credential)."""
    if depth > 16:
        return False
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return len(value) <= _MAX_TEXT
    if isinstance(value, list):
        return len(value) <= 10_000 and all(_json_ok(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return len(value) <= 10_000 and all(isinstance(k, str) and _json_ok(v, depth + 1) for k, v in value.items())
    return False


def _texts(value, limit: int = 1000) -> bool:
    return isinstance(value, list) and len(value) <= limit and all(isinstance(t, str) and len(t) <= _MAX_TEXT for t in value)


def credential(data: bytes, anchors: bytes | None = None):
    """``credentials.read_credential`` of ``data``, run in a worker, checked field by field."""
    import base64

    params = {"anchors": base64.b64encode(anchors).decode("ascii") if anchors else None}
    return _validated(_accept_credential, _call("credential", data, params, 1 << 22))


def _accept_credential(value):
    from . import credentials
    from .inspect import Finding

    if not isinstance(value, dict) or len(value) != 1:
        raise _bad("something other than a credential")
    if "unavailable" in value and isinstance(value["unavailable"], str):
        raise credentials.CredentialsUnavailable(value["unavailable"])
    if "error" in value and isinstance(value["error"], str):
        raise credentials.CredentialError(value["error"])
    d = value.get("credential")
    keys = {
        "present", "format", "state", "label", "signer", "signer_name", "imgstrip", "sanitization", "pixels_only",
        "declared_ai", "problems", "notes", "other",
    }  # fmt: skip
    if not isinstance(d, dict) or set(d) != keys:
        raise _bad("something other than a credential")
    flags = ("present", "imgstrip", "pixels_only", "declared_ai")
    optional_text = ("format", "state", "signer", "signer_name")
    if not all(type(d[k]) is bool for k in flags):
        raise _bad("an impossible credential")
    if not all(d[k] is None or (isinstance(d[k], str) and len(d[k]) <= _MAX_TEXT) for k in optional_text):
        raise _bad("an impossible credential")
    if d["format"] is not None and d["format"] not in _FORMATS:
        raise _bad("an impossible credential")
    if d["state"] not in (None, "Invalid", "Valid", "Trusted") or not (d["sanitization"] is None or _json_ok(d["sanitization"])):
        raise _bad("an impossible credential")
    if not (_texts(d["problems"]) and _texts(d["notes"]) and isinstance(d["other"], list) and len(d["other"]) <= 1000):
        raise _bad("an impossible credential")
    others = []
    for item in d["other"]:
        if not (isinstance(item, dict) and set(item) == {"category", "label", "detail"}):
            raise _bad("an impossible finding")
        finding = Finding(item["category"], item["label"], item["detail"])
        if not isinstance(item["detail"], str) or not _finding_ok(finding):
            raise _bad("an impossible finding")
        others.append(finding)
    return credentials.Credential(
        present=d["present"],
        format=d["format"],
        state=d["state"],
        signer=d["signer"],
        signer_name=d["signer_name"],
        imgstrip=d["imgstrip"],
        sanitization=d["sanitization"],
        pixels_only=d["pixels_only"],
        declared_ai=d["declared_ai"],
        problems=list(d["problems"]),
        notes=list(d["notes"]),
        other_findings=others,
    )


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


def probe_limits(
    directory: str, victim: str, port: int | None = None, secret: str | None = None, unix: str | None = None
) -> dict:
    """What a worker may still do, for the tests and the self-test.  It tries to write a file in ``directory``, to
    create one, to change then delete the file ``victim``, to connect to ``port`` on the loopback interface (and send a
    datagram there), to connect to the Unix socket ``unix``, to read the file ``secret``, to read this process's memory
    and environment, to signal it, to start a process and to make an anonymous file; ``tier`` is what it was given."""
    params = {"dir": directory, "victim": victim, "port": port, "secret": secret, "unix": unix, "target": os.getpid()}
    return _call("_limits", b"", params, 1 << 16)


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
