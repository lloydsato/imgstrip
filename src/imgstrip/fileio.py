"""Writing files: completely or not at all, and never by overwriting an existing file in place.

Every file imgstrip writes (cleaned images, the web app's saved files, C2PA receipts, test identities) is first
written to a temporary file in the destination's own directory and flushed to disk, and only then takes the
destination's name.  An existing file is never opened for writing, so nobody can see a half-written file under the
destination's name and a failure leaves the destination as it was.  Two ways to take the name:

``write_new``       never replaces anything.  If the name exists (a file, a directory or a symbolic link, even a
                    dangling one) it raises ``FileExistsError`` and the destination is untouched.  The name is
                    claimed atomically, so a file that appears between the check and the write is not replaced
                    either: with a hard link where the file system has them, else with a rename that refuses to
                    replace (``renameat2(RENAME_NOREPLACE)`` on Linux, ``renamex_np(RENAME_EXCL)`` on macOS, a plain
                    rename on Windows).  Only where none of these exists does it claim the name with an empty
                    placeholder created exclusively and then rename the content over that placeholder; another
                    program would have to replace the placeholder in the instant between the two steps to lose
                    its file.
``replace_atomic``  replaces the destination with one rename (``--force``, ``--in-place``, the receipt file).  A
                    reader sees the old or the new file, never a mixture, and other hard links to the old file keep
                    the old data.  A symbolic link at the destination is replaced by the file, not followed.
"""

from __future__ import annotations

import errno
import os
import shutil
import sys
import tempfile

_TEMP_PREFIX = ".imgstrip-"
_UMASK: int | None = None
# What a file system says when it has no hard links (FAT, exFAT, some network shares and FUSE file systems).
_NO_LINKS = {
    getattr(errno, name)
    for name in ("EPERM", "EACCES", "EXDEV", "EMLINK", "ENOSYS", "EINVAL", "ENOTSUP", "EOPNOTSUPP")
    if hasattr(errno, name)
}
# What a system says when it has no non-replacing rename for this file system.
_NO_NOREPLACE = {getattr(errno, name) for name in ("EINVAL", "ENOSYS", "ENOTSUP", "EOPNOTSUPP") if hasattr(errno, name)}


def default_mode() -> int:
    """The permission bits a new file gets from the process's umask."""
    global _UMASK
    if _UMASK is None:
        _UMASK = os.umask(0)
        os.umask(_UMASK)
    return 0o666 & ~_UMASK


def _temporary(directory: str, data: bytes | os.PathLike, mode: int | None) -> str:
    """A complete, flushed temporary file in ``directory`` holding ``data`` (removed again on any failure).

    ``data`` is the content, or the path of a file to copy it from (so a large file is never held in memory).
    """
    fd, tmp = tempfile.mkstemp(prefix=_TEMP_PREFIX, suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            if isinstance(data, os.PathLike):
                with open(data, "rb") as src:
                    shutil.copyfileobj(src, fh, 1 << 20)
            else:
                fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode if mode is not None else default_mode())
    except BaseException:
        _remove(tmp)
        raise
    return tmp


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _sync_directory(directory: str) -> None:
    """Make the new name durable (POSIX; a no-op where directories cannot be opened)."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _directory_of(dest: str) -> str:
    directory = os.path.dirname(os.path.abspath(dest))
    os.makedirs(directory, exist_ok=True)
    return directory


def replace_atomic(dest: str | os.PathLike, data: bytes | os.PathLike, mode: int | None = None) -> None:
    """Write ``data`` (bytes, or the path of a file to copy) to ``dest``, replacing whatever is there in one rename."""
    dest = os.fspath(dest)
    directory = _directory_of(dest)
    tmp = _temporary(directory, data, mode)
    try:
        os.replace(tmp, dest)
    except BaseException:
        _remove(tmp)
        raise
    _sync_directory(directory)


def _exists_error(dest: str) -> FileExistsError:
    return FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), dest)


def _rename_noreplace(src: str, dest: str) -> bool:
    """Rename ``src`` to ``dest`` unless ``dest`` exists (then FileExistsError).  False if the system cannot."""
    if sys.platform == "win32":
        os.rename(src, dest)  # Windows never replaces an existing file on rename
        return True
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
    except (ImportError, OSError):
        return False
    src_b, dest_b = os.fsencode(src), os.fsencode(dest)
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        at_fdcwd, rename_noreplace = -100, 1
        result = libc.renameat2(at_fdcwd, src_b, at_fdcwd, dest_b, rename_noreplace)
    elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        rename_excl = 0x00000004
        result = libc.renamex_np(src_b, dest_b, rename_excl)
    else:
        return False
    if result == 0:
        return True
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        raise _exists_error(dest)
    if err in _NO_NOREPLACE:
        return False  # this file system does not support it
    raise OSError(err, os.strerror(err), dest)


def write_new(dest: str | os.PathLike, data: bytes | os.PathLike, mode: int | None = None) -> None:
    """Write ``data`` (bytes, or the path of a file to copy) to ``dest`` completely or not at all, and never replace
    anything already there."""
    dest = os.fspath(dest)
    if os.path.lexists(dest):
        raise _exists_error(dest)
    directory = _directory_of(dest)
    tmp = _temporary(directory, data, mode)
    try:
        try:
            os.link(tmp, dest)  # atomic, and it fails if the name is taken
        except FileExistsError:
            raise
        except OSError as exc:
            if exc.errno not in _NO_LINKS:
                raise
        else:
            _remove(tmp)
            _sync_directory(directory)
            return
        if _rename_noreplace(tmp, dest):
            _sync_directory(directory)
            return
        # Last resort: claim the name with an empty file nobody else can have created, then rename over it.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        os.close(os.open(dest, flags, 0o600))
        try:
            os.replace(tmp, dest)
        except BaseException:
            _remove(dest)
            raise
    except BaseException:
        _remove(tmp)
        raise
    _sync_directory(directory)
