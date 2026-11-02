"""A system-call filter for the decoder sandbox (seccomp-bpf, Linux on x86-64 and arm64).

Landlock (sandbox.py) limits what a decoder worker may do to *files*; it says nothing about sockets, other processes,
the kernel's wider attack surface or ``mmap`` tricks that Landlock predates.  This filter closes those: once it is
installed a worker

* cannot create or use a socket of any kind (no network, no Unix sockets, no netlink), nor shared memory, message
  queues or semaphores that other processes of the user could also reach;
* cannot start a process (``fork``, ``vfork``, ``execve``, ``clone`` without ``CLONE_THREAD``), signal one outside
  itself, attach to one, or read its memory (``ptrace``, ``process_vm_*``, ``pidfd_*``);
* cannot open a file for writing, create, remove, rename, truncate or change the mode, owner or attributes of
  anything, and cannot make an anonymous executable file (``memfd_create``);
* cannot reach the parts of the kernel that decoders never use and that have a history of local privilege
  escalations: ``io_uring``, ``bpf``, ``perf_event_open``, ``userfaultfd``, keyrings, mounts and namespaces.

Everything else a decoder needs (memory, threads, reading, ``futex``, ``getrandom``) stays allowed: this is a deny
list over the 64-bit ABI, so a Python or library that makes a call nobody foresaw keeps working, and a call the
list names fails with ``EPERM`` (``ENOSYS`` for the two newer calls that have an older fallback).  A call made
through another ABI (32-bit or x32) is refused.  On any other architecture, or a kernel without seccomp, no filter is
installed and the worker reports that (``sandbox.status()``).

The syscall numbers below come from the kernel's own headers (``asm/unistd_64.h`` for x86-64 and
``asm-generic/unistd.h`` for arm64) and are checked against them in the tests when those headers are present.
"""

from __future__ import annotations

import ctypes
import struct
import sys

# --------------------------------------------------------------------------
# What is refused
# --------------------------------------------------------------------------

NETWORK = "socket socketpair connect bind listen accept accept4 sendto sendmsg sendmmsg"
IPC = (
    "shmget shmat shmctl shmdt semget semop semctl semtimedop msgget msgsnd msgrcv msgctl "
    "mq_open mq_unlink mq_timedsend mq_timedreceive mq_notify mq_getsetattr"
)
PROCESSES = (
    "ptrace process_vm_readv process_vm_writev process_madvise pidfd_open pidfd_getfd pidfd_send_signal kill tkill "
    "rt_sigqueueinfo rt_tgsigqueueinfo fork vfork execve execveat"
)
KERNEL = (
    "io_uring_setup io_uring_enter io_uring_register bpf perf_event_open userfaultfd keyctl add_key request_key mount "
    "umount2 pivot_root chroot unshare setns open_tree move_mount fsopen fsconfig fsmount fspick mount_setattr swapon "
    "swapoff reboot kexec_load kexec_file_load init_module finit_module delete_module acct quotactl quotactl_fd "
    "settimeofday clock_settime clock_adjtime adjtimex sethostname setdomainname syslog iopl ioperm modify_ldt vhangup "
    "name_to_handle_at open_by_handle_at fanotify_init memfd_create memfd_secret kcmp uselib"
)
FILES = (
    "creat truncate ftruncate fallocate unlink unlinkat rename renameat renameat2 mkdir mkdirat rmdir link linkat "
    "symlink symlinkat mknod mknodat chmod fchmod fchmodat fchmodat2 chown fchown lchown fchownat setxattr lsetxattr "
    "fsetxattr removexattr lremovexattr fremovexattr utime utimes futimesat utimensat"
)
REFUSED = " ".join((NETWORK, IPC, PROCESSES, KERNEL, FILES)).split()
NOT_THERE = ("clone3", "openat2")  # ENOSYS: glibc and Python fall back to ``clone`` and ``openat``, which are checked

# open(2) flags that mean "write": O_WRONLY | O_RDWR (the access mode, 3), O_CREAT, O_TRUNC, __O_TMPFILE.  The same
# values on x86-64 and arm64.
WRITE_FLAGS = 0x3 | 0x40 | 0x200 | 0x400000
CLONE_THREAD = 0x10000

_NUMBERS = {
    "x86_64": {
        "open": 2, "shmget": 29, "shmat": 30, "shmctl": 31, "socket": 41, "connect": 42, "accept": 43, "sendto": 44,
        "sendmsg": 46, "bind": 49, "listen": 50, "socketpair": 53, "clone": 56, "fork": 57, "vfork": 58,
        "execve": 59, "kill": 62, "semget": 64, "semop": 65, "semctl": 66, "shmdt": 67, "msgget": 68, "msgsnd": 69,
        "msgrcv": 70, "msgctl": 71, "truncate": 76, "ftruncate": 77, "rename": 82, "mkdir": 83, "rmdir": 84,
        "creat": 85, "link": 86, "unlink": 87, "symlink": 88, "chmod": 90, "fchmod": 91, "chown": 92, "fchown": 93,
        "lchown": 94, "ptrace": 101, "syslog": 103, "rt_sigqueueinfo": 129, "utime": 132, "mknod": 133,
        "uselib": 134, "vhangup": 153, "modify_ldt": 154, "pivot_root": 155, "adjtimex": 159, "chroot": 161,
        "acct": 163, "settimeofday": 164, "mount": 165, "umount2": 166, "swapon": 167, "swapoff": 168, "reboot": 169,
        "sethostname": 170, "setdomainname": 171, "iopl": 172, "ioperm": 173, "init_module": 175,
        "delete_module": 176, "quotactl": 179, "setxattr": 188, "lsetxattr": 189, "fsetxattr": 190,
        "removexattr": 197, "lremovexattr": 198, "fremovexattr": 199, "tkill": 200, "semtimedop": 220,
        "clock_settime": 227, "tgkill": 234, "utimes": 235, "mq_open": 240, "mq_unlink": 241, "mq_timedsend": 242,
        "mq_timedreceive": 243, "mq_notify": 244, "mq_getsetattr": 245, "kexec_load": 246, "add_key": 248,
        "request_key": 249, "keyctl": 250, "openat": 257, "mkdirat": 258, "mknodat": 259, "fchownat": 260,
        "futimesat": 261, "unlinkat": 263, "renameat": 264, "linkat": 265, "symlinkat": 266, "fchmodat": 268,
        "unshare": 272, "utimensat": 280, "fallocate": 285, "accept4": 288, "rt_tgsigqueueinfo": 297,
        "perf_event_open": 298, "fanotify_init": 300, "name_to_handle_at": 303, "open_by_handle_at": 304,
        "clock_adjtime": 305, "sendmmsg": 307, "setns": 308, "process_vm_readv": 310, "process_vm_writev": 311,
        "kcmp": 312, "finit_module": 313, "renameat2": 316, "memfd_create": 319, "kexec_file_load": 320, "bpf": 321,
        "execveat": 322, "userfaultfd": 323, "pidfd_send_signal": 424, "io_uring_setup": 425, "io_uring_enter": 426,
        "io_uring_register": 427, "open_tree": 428, "move_mount": 429, "fsopen": 430, "fsconfig": 431,
        "fsmount": 432, "fspick": 433, "pidfd_open": 434, "clone3": 435, "openat2": 437, "pidfd_getfd": 438,
        "process_madvise": 440, "mount_setattr": 442, "quotactl_fd": 443, "memfd_secret": 447, "fchmodat2": 452,
    },
    "aarch64": {
        "setxattr": 5, "lsetxattr": 6, "fsetxattr": 7, "removexattr": 14, "lremovexattr": 15, "fremovexattr": 16,
        "mknodat": 33, "mkdirat": 34, "unlinkat": 35, "symlinkat": 36, "linkat": 37, "renameat": 38, "umount2": 39,
        "mount": 40, "pivot_root": 41, "truncate": 45, "ftruncate": 46, "fallocate": 47, "chroot": 51, "fchmod": 52,
        "fchmodat": 53, "fchownat": 54, "fchown": 55, "openat": 56, "vhangup": 58, "quotactl": 60, "utimensat": 88,
        "acct": 89, "unshare": 97, "kexec_load": 104, "init_module": 105, "delete_module": 106, "clock_settime": 112,
        "syslog": 116, "ptrace": 117, "kill": 129, "tkill": 130, "tgkill": 131, "rt_sigqueueinfo": 138,
        "reboot": 142, "sethostname": 161, "setdomainname": 162, "settimeofday": 170, "adjtimex": 171,
        "mq_open": 180, "mq_unlink": 181, "mq_timedsend": 182, "mq_timedreceive": 183, "mq_notify": 184,
        "mq_getsetattr": 185, "msgget": 186, "msgctl": 187, "msgrcv": 188, "msgsnd": 189, "semget": 190,
        "semctl": 191, "semtimedop": 192, "semop": 193, "shmget": 194, "shmctl": 195, "shmat": 196, "shmdt": 197,
        "socket": 198, "socketpair": 199, "bind": 200, "listen": 201, "accept": 202, "connect": 203, "sendto": 206,
        "sendmsg": 211, "add_key": 217, "request_key": 218, "keyctl": 219, "clone": 220, "execve": 221,
        "swapon": 224, "swapoff": 225, "rt_tgsigqueueinfo": 240, "perf_event_open": 241, "accept4": 242,
        "fanotify_init": 262, "name_to_handle_at": 264, "open_by_handle_at": 265, "clock_adjtime": 266, "setns": 268,
        "sendmmsg": 269, "process_vm_readv": 270, "process_vm_writev": 271, "kcmp": 272, "finit_module": 273,
        "renameat2": 276, "memfd_create": 279, "bpf": 280, "execveat": 281, "userfaultfd": 282,
        "kexec_file_load": 294, "pidfd_send_signal": 424, "io_uring_setup": 425, "io_uring_enter": 426,
        "io_uring_register": 427, "open_tree": 428, "move_mount": 429, "fsopen": 430, "fsconfig": 431,
        "fsmount": 432, "fspick": 433, "pidfd_open": 434, "clone3": 435, "openat2": 437, "pidfd_getfd": 438,
        "process_madvise": 440, "mount_setattr": 442, "quotactl_fd": 443, "memfd_secret": 447, "fchmodat2": 452,
    },
}


_AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}  # EM_X86_64 / EM_AARCH64 | 64-bit | little-endian
_MACHINES = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}

_LD_ABS = 0x20  # BPF_LD | BPF_W | BPF_ABS
_JEQ = 0x15  # BPF_JMP | BPF_JEQ | BPF_K
_JGE = 0x35
_JSET = 0x45
_RET = 0x06
_ALLOW = 0x7FFF0000
_KILL = 0x80000000  # SECCOMP_RET_KILL_PROCESS
_EPERM, _ENOSYS = 1, 38
_NR, _ARCH, _ARGS = 0, 4, 16  # offsets in struct seccomp_data; the arguments are 8 bytes each, low half first
_X32 = 0x40000000


def machine() -> str | None:
    """The architecture this filter supports for this process (64-bit Python on x86-64 or arm64 Linux), else None."""
    if not sys.platform.startswith("linux") or sys.maxsize <= 2**32:
        return None  # a 32-bit Python on a 64-bit kernel makes 32-bit calls, which the filter would refuse
    import os

    return _MACHINES.get(os.uname().machine.lower())


def _ins(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("=HBBI", code, jt, jf, k & 0xFFFFFFFF)


def build(arch: str, pid: int) -> bytes:
    """The filter program (an array of ``struct sock_filter``) for ``arch``; ``pid`` is the only process ``tgkill`` may
    signal (the process itself: ``abort`` and the threads' cancellation need it)."""
    nr = _NUMBERS[arch]
    out: list[bytes] = [
        _ins(_LD_ABS, 0, 0, _ARCH),
        _ins(_JEQ, 1, 0, _AUDIT_ARCH[arch]),  # the right ABI: skip the kill
        _ins(_RET, 0, 0, _KILL),
        _ins(_LD_ABS, 0, 0, _NR),
    ]
    if arch == "x86_64":  # the x32 ABI shares the architecture value; its numbers have this bit set
        out += [_ins(_JGE, 0, 1, _X32), _ins(_RET, 0, 0, 0x00050000 | _ENOSYS)]
    for names, errno in ((REFUSED, _EPERM), (NOT_THERE, _ENOSYS)):
        for name in names:
            if name in nr:  # a call an architecture does not have needs no rule
                out += [_ins(_JEQ, 0, 1, nr[name]), _ins(_RET, 0, 0, 0x00050000 | errno)]
    reload = _ins(_LD_ABS, 0, 0, _NR)
    # open(2) / openat(2): refused when the flags ask for writing.  (flags is argument 1 / 2)
    for name, arg in (("open", 1), ("openat", 2)):
        if name in nr:
            out += [
                _ins(_JEQ, 0, 4, nr[name]),
                _ins(_LD_ABS, 0, 0, _ARGS + 8 * arg),
                _ins(_JSET, 0, 1, WRITE_FLAGS),
                _ins(_RET, 0, 0, 0x00050000 | _EPERM),
                reload,
            ]
    # clone(2): threads only
    out += [
        _ins(_JEQ, 0, 4, nr["clone"]),
        _ins(_LD_ABS, 0, 0, _ARGS),
        _ins(_JSET, 1, 0, CLONE_THREAD),
        _ins(_RET, 0, 0, 0x00050000 | _EPERM),
        reload,
    ]
    # tgkill(2): the process itself only
    out += [
        _ins(_JEQ, 0, 4, nr["tgkill"]),
        _ins(_LD_ABS, 0, 0, _ARGS),
        _ins(_JEQ, 1, 0, pid),
        _ins(_RET, 0, 0, 0x00050000 | _EPERM),
        reload,
    ]
    out.append(_ins(_RET, 0, 0, _ALLOW))
    return b"".join(out)


class _Prog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def install(libc, pid: int) -> bool:
    """Install the filter on the calling thread (which must have set no_new_privs).  False when it cannot be:
    an architecture without a table, or a kernel without seccomp filters."""
    arch = machine()
    if arch is None:
        return False
    program = build(arch, pid)
    buf = ctypes.create_string_buffer(program, len(program))
    prog = _Prog(len(program) // 8, ctypes.addressof(buf))
    return libc.prctl(22, 2, ctypes.byref(prog), 0, 0) == 0  # PR_SET_SECCOMP, SECCOMP_MODE_FILTER
