"""How far a decoder worker is confined (2.0.3): the system-call filter, the read allow-list, the zygote that keeps
nothing of earlier files, the limits, and what happens where the system offers less than the full set.

The filter is checked three ways: its tables against the kernel's headers, its program run through a small
interpreter of classic BPF for both architectures (so the arm64 filter is tested on an x86-64 machine too), and the
real thing on this machine, from inside a worker that does what a hostile decoder would."""

from __future__ import annotations

import io
import json
import os
import re
import socket
import struct
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from imgstrip import _seccomp as seccomp
from imgstrip import sandbox
from imgstrip.core import StripError

linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")


@pytest.fixture(scope="module")
def full_confinement():
    """Skip the test unless this machine can confine a worker completely (Linux 5.13+, x86-64 or arm64)."""
    if not sys.platform.startswith("linux") or not sandbox.status().get("full"):
        pytest.skip("needs a Linux with Landlock and seccomp")


# ---------------------------------------------------------------------------------------------------------------
# The tables and the program
# ---------------------------------------------------------------------------------------------------------------


def header_numbers(path: str, prefix: str = "__NR_") -> dict[str, int]:
    found = {}
    for line in Path(path).read_text().splitlines():
        m = re.match(rf"#define\s+{prefix}([a-z0-9_]+)\s+(\d+)\s*$", line)
        if m:
            found[m[1]] = int(m[2])
    return found


def test_the_x86_64_table_matches_the_kernel_headers():
    path = "/usr/include/x86_64-linux-gnu/asm/unistd_64.h"
    if not Path(path).exists():
        pytest.skip("no kernel headers here")
    numbers = header_numbers(path)
    for name, number in seccomp._NUMBERS["x86_64"].items():
        assert numbers[name] == number, name


def test_the_arm64_table_matches_the_kernel_headers():
    path = "/usr/include/asm-generic/unistd.h"
    if not Path(path).exists():
        pytest.skip("no kernel headers here")
    numbers = header_numbers(path)
    # the generic header numbers a few calls through __NR3264_*; its other entries are plain
    numbers.update({"truncate": 45, "ftruncate": 46})
    for name, number in seccomp._NUMBERS["aarch64"].items():
        assert numbers[name] == number, name


def test_every_refused_call_is_in_the_x86_64_table_and_the_two_tables_agree_where_both_know_a_call():
    for name in (*seccomp.REFUSED, *seccomp.NOT_THERE, "open", "openat", "clone", "tgkill"):
        assert name in seccomp._NUMBERS["x86_64"], name
    for name, number in seccomp._NUMBERS["aarch64"].items():
        if number >= 424:  # the numbers of every call added since 5.1 are the same on all architectures
            assert seccomp._NUMBERS["x86_64"][name] == number, name


class Bpf:
    """Classic BPF, as far as the filter uses it, run on a made-up ``seccomp_data``."""

    def __init__(self, program: bytes):
        self.ins = [struct.unpack_from("=HBBI", program, i) for i in range(0, len(program), 8)]

    def run(self, arch: int, nr: int, *args: int) -> int:
        data = struct.pack("=IIQ6Q", nr, arch, 0, *(list(args) + [0] * 6)[:6])
        a, pc = 0, 0
        for _ in range(10_000):
            code, jt, jf, k = self.ins[pc]
            pc += 1
            if code == 0x20:  # ld abs
                (a,) = struct.unpack_from("=I", data, k)
            elif code == 0x15:  # jeq
                pc += jt if a == k else jf
            elif code == 0x35:  # jge
                pc += jt if a >= k else jf
            elif code == 0x45:  # jset
                pc += jt if a & k else jf
            elif code == 0x06:  # ret
                return k
            else:
                raise AssertionError(f"instruction {code:#x}")
        raise AssertionError("no verdict")


ALLOW, EPERM, ENOSYS, KILL = 0x7FFF0000, 0x00050001, 0x00050026, 0x80000000
PID = 4242
O_WRONLY, O_RDWR, O_CREAT, O_TRUNC, O_TMPFILE, O_DIRECTORY, O_CLOEXEC = 1, 2, 0o100, 0o1000, 0o20200000, 0o200000, 0o2000000


@pytest.fixture(params=["x86_64", "aarch64"])
def filt(request):
    arch = request.param
    bpf = Bpf(seccomp.build(arch, PID))
    nr = seccomp._NUMBERS[arch]
    audit = seccomp._AUDIT_ARCH[arch]

    def verdict(name_or_number, *args):
        number = nr[name_or_number] if isinstance(name_or_number, str) else name_or_number
        return bpf.run(audit, number, *args)

    verdict.arch, verdict.nr, verdict.audit, verdict.bpf = arch, nr, audit, bpf
    return verdict


def test_the_program_is_short_and_ends_in_a_verdict(filt):
    assert 8 < len(filt.bpf.ins) < 4096
    assert filt.bpf.ins[-1][0] == 0x06


def test_calls_a_decoder_makes_pass(filt):
    for name in ("read", "write", "mmap", "munmap", "mprotect", "brk", "futex", "madvise", "getrandom", "exit_group"):
        number = {"x86_64": {"read": 0, "write": 1, "mmap": 9, "mprotect": 10, "munmap": 11, "brk": 12, "madvise": 28,
                             "exit_group": 231, "futex": 202, "getrandom": 318},
                  "aarch64": {"read": 63, "write": 64, "mmap": 222, "mprotect": 226, "munmap": 215, "brk": 214,
                              "madvise": 233, "exit_group": 94, "futex": 98, "getrandom": 278}}[filt.arch][name]
        assert filt(number) == ALLOW, name
    assert filt("openat", 0, 0, os.O_RDONLY | O_CLOEXEC) == ALLOW
    assert filt("openat", 0, 0, os.O_RDONLY | O_DIRECTORY) == ALLOW
    assert filt("clone", seccomp.CLONE_THREAD | 0x100) == ALLOW  # a thread
    assert filt("tgkill", PID, 5, 6) == ALLOW  # abort() signals the process itself


@pytest.mark.parametrize("name", seccomp.REFUSED)
def test_every_refused_call_fails_with_eperm(filt, name):
    if name not in filt.nr:
        pytest.skip(f"{filt.arch} has no {name}")
    assert filt(name, 1, 2, 3) == EPERM


@pytest.mark.parametrize("flags", [O_WRONLY, O_RDWR, O_CREAT, O_TRUNC, O_TMPFILE, O_WRONLY | O_CREAT | O_TRUNC, 3])
def test_opening_for_writing_is_refused_however_it_is_asked(filt, flags):
    assert filt("openat", 0, 0, flags) == EPERM
    if "open" in filt.nr:
        assert filt("open", 0, flags) == EPERM


def test_a_process_cannot_be_started_or_signalled(filt):
    assert filt("clone", 0x11) == EPERM  # SIGCHLD, no CLONE_THREAD: a fork
    assert filt("clone", 0) == EPERM
    assert filt("tgkill", PID + 1, 5, 9) == EPERM  # someone else's process
    assert filt("clone3") == ENOSYS and filt("openat2") == ENOSYS  # the callers fall back to the checked calls


def test_another_abi_is_refused(filt):
    assert filt.bpf.run(0x40000003, 0) == KILL  # i386
    assert filt.bpf.run(0xC00000B7 if filt.arch == "x86_64" else 0xC000003E, 0) == KILL
    if filt.arch == "x86_64":
        assert filt.bpf.run(filt.audit, 0x40000000 | 0) == ENOSYS  # x32 numbers


def test_the_filter_takes_the_pid_it_is_built_for():
    a, b = seccomp.build("x86_64", 1), seccomp.build("x86_64", 2)
    assert a != b and len(a) == len(b)


# ---------------------------------------------------------------------------------------------------------------
# On this machine
# ---------------------------------------------------------------------------------------------------------------


def test_a_machine_without_a_table_gets_no_filter(monkeypatch):
    monkeypatch.setattr(os, "uname", lambda: os.uname_result(("Linux", "h", "6", "1", "riscv64")))
    assert seccomp.machine() is None
    assert seccomp.install(None, 1) is False  # no libc is touched


def test_a_32_bit_python_gets_no_filter(monkeypatch):
    monkeypatch.setattr(sys, "maxsize", 2**31 - 1)
    assert seccomp.machine() is None


def run_child(code: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """A Python that runs ``code`` as a worker would (in its own process, so the pytest process stays unconfined)."""
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True, timeout=timeout, check=False
    )


@linux
def test_without_seccomp_the_tier_says_so():
    out = run_child(
        """
        import json
        from imgstrip import _seccomp, sandbox
        _seccomp.machine = lambda: None
        print(json.dumps(sandbox._restrict(sandbox._read_roots())))
        """
    )
    tier = json.loads(out.stdout)
    assert tier["seccomp"] is False and tier["full"] is False, out.stderr


@linux
def test_without_landlock_the_tier_says_so():
    out = run_child(
        """
        import json
        from imgstrip import sandbox
        sandbox._landlock = lambda libc, roots=(): (0, False)
        print(json.dumps(sandbox._restrict([])))
        """
    )
    tier = json.loads(out.stdout)
    assert tier["landlock"] == 0 and tier["full"] is False and tier["seccomp"] is True, out.stderr


@linux
def test_a_read_list_that_matches_nothing_falls_back_to_the_write_only_ruleset():
    out = run_child(
        """
        import json
        from imgstrip import sandbox
        tier = sandbox._restrict(["/nonexistent-1", "/nonexistent-2"])
        print(json.dumps(tier))
        open("/etc/hostname").read(1) if __import__("os").path.exists("/etc/hostname") else None
        print("still reads")
        """
    )
    tier = json.loads(out.stdout.splitlines()[0])
    if tier["landlock"]:
        assert tier["reads"] is False and tier["full"] is False
        assert "still reads" in out.stdout


@linux
def test_the_read_roots_leave_out_the_home_directory_and_everything_above_it(monkeypatch, tmp_path):
    home = tmp_path / "home" / "me"
    (home / "project" / "venv").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sys, "path", [*sys.path, str(home), str(tmp_path / "home"), "/", str(home / "project" / "venv")])
    roots = sandbox._read_roots()
    assert str(home) not in roots and str(tmp_path / "home") not in roots and "/" not in roots
    assert str(home / "project" / "venv") in roots
    assert "/tmp" not in roots and "/etc" not in roots and "/root" not in roots


@linux
def test_the_read_roots_hold_what_python_and_the_libraries_need():
    roots = sandbox._read_roots()
    assert any(os.path.realpath(sys.prefix) == r or os.path.realpath(sys.prefix).startswith(r + "/") for r in roots)
    assert "/proc/self" in roots and "/dev/null" in roots


def test_a_confined_worker_can_do_none_of_the_things_a_hostile_decoder_would(tmp_path, full_confinement):
    victim, secret = tmp_path / "victim", Path.home() / ".imgstrip-test-secret"
    victim.write_text("keep")
    secret.write_text("secret")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    local = socket.socket(socket.AF_UNIX)
    local.bind(str(tmp_path / "sock"))
    local.listen()
    try:
        limits = sandbox.probe_limits(
            str(tmp_path), str(victim), listener.getsockname()[1], str(secret), str(tmp_path / "sock")
        )
    finally:
        listener.close()
        local.close()
        secret.unlink()
    assert limits["tier"]["full"], limits["tier"]
    for what in (
        "write", "create", "map_write", "remove", "connect", "datagram", "unix", "netlink", "read", "fork", "signal",
        "memfd", "peek", "environ",
    ):  # fmt: skip
        assert limits[what].startswith("no"), (what, limits[what])
    assert victim.read_text() == "keep" and not (tmp_path / "written").exists() and not (tmp_path / "created").exists()
    assert limits["oom_score_adj"] == 1000
    assert limits["environment"] == sorted(limits["environment"]) and "HOME" not in limits["environment"]


def test_a_worker_is_the_first_the_out_of_memory_killer_takes_and_cannot_grow_without_limit(full_confinement):
    assert sandbox.probe_limits("/nonexistent", "/nonexistent")["oom_score_adj"] == 1000
    assert sandbox._call("_alloc", b"", {"bytes": 8 << 20, "memory": 256 << 20}, 1 << 16) == 8 << 20
    with pytest.raises(StripError, match="not enough memory"):
        sandbox._call("_alloc", b"", {"bytes": 1 << 30, "memory": 128 << 20}, 1 << 16)
    # the worker that ran out of memory ended cleanly: the next file is served
    assert sandbox._call("_alloc", b"", {"bytes": 1 << 20, "memory": 256 << 20}, 1 << 16) == 1 << 20


def test_the_data_limit_follows_the_pixel_limit_and_the_input(monkeypatch):
    seen = []
    monkeypatch.setattr(sandbox, "_POOL", sandbox._Pool())

    class Stop(Exception):
        pass

    class Fake:
        one_shot = False
        retired = False

        class writer:
            @staticmethod
            def frame(kind, payload=b""):
                if kind == b"R":
                    seen.append(json.loads(payload))
                    raise Stop

        def kill(self): ...
        def close(self): ...

    monkeypatch.setattr(sandbox._POOL, "acquire", lambda: Fake())
    for pixels, size in ((1_000, 10), (50_000_000, 5 << 20)):
        with pytest.raises(Stop):
            sandbox._call("decode", b"x" * size, {"options": {"max_pixels": pixels}}, 1)
    assert seen[0]["memory"] == 64 * 1_000 + (512 << 20) + 40
    assert seen[1]["memory"] == 64 * 50_000_000 + (512 << 20) + 4 * (5 << 20)


def test_the_time_allowed_grows_with_the_work_up_to_a_ceiling():
    small = sandbox._deadline(1_000_000, 100_000)
    big = sandbox._deadline(150_000_000, 100 << 20)
    assert 60 <= small < 70
    assert small < big <= sandbox._TIMEOUT
    assert sandbox._deadline(10**12, 10**12) == sandbox._TIMEOUT


# ---------------------------------------------------------------------------------------------------------------
# Strict mode and the report of the tier
# ---------------------------------------------------------------------------------------------------------------


class Sink(io.BytesIO):
    def frames(self):
        raw, out = self.getvalue(), []
        while raw:
            _magic, kind, length = struct.unpack_from(">2scQ", raw)
            out.append((kind, raw[11 : 11 + length]))
            raw = raw[11 + length :]
        return out


def answer(name: str, params: dict) -> list:
    sink = Sink()
    sandbox._answer(name, params, b"", sandbox._Writer(sink))
    return sink.frames()


WEAK = {"landlock": 0, "reads": False, "seccomp": False, "full": False}
FULL = {"landlock": 7, "reads": True, "seccomp": True, "full": True}


def test_a_worker_that_is_asked_for_full_confinement_and_does_not_have_it_refuses_before_it_reads_anything():
    frames = answer("_tier", {"tier": WEAK, "require": True})
    assert [k for k, _ in frames] == [b"T", b"X"]
    failure = json.loads(frames[1][1])
    assert failure["kind"] == "strip" and "--require-sandbox" in failure["message"] and "nothing was decoded" in failure["message"]


def test_a_weak_worker_that_is_not_held_to_it_goes_on():
    frames = answer("_tier", {"tier": WEAK, "require": False})
    assert [k for k, _ in frames] == [b"T", b"H", b"E"]
    assert json.loads(frames[0][1]) == WEAK


def test_a_full_worker_goes_on_under_strict_mode():
    assert [k for k, _ in answer("_tier", {"tier": FULL, "require": True})] == [b"T", b"H", b"E"]


@pytest.mark.parametrize(
    "tier",
    [
        {"landlock": 7},
        {**FULL, "full": "yes"},
        {**FULL, "landlock": True},
        {**FULL, "extra": 1},
        [1, 2],
        "full",
    ],
)
def test_a_tier_report_that_is_not_what_a_worker_sends_is_refused(tier):
    with pytest.raises(ValueError):
        sandbox._note_tier(json.dumps(tier).encode())


def lie(*items):
    return {"frames": [[kind, payload] for kind, payload in items]}


def test_tier_frames_that_are_too_long_or_cut_short_are_an_error_not_a_hang():
    with pytest.raises(StripError, match="nothing was written"):
        sandbox._call("_lie", b"", lie(("T", "x" * 5000)), 1 << 16)
    with pytest.raises(StripError, match="nothing was written"):
        sandbox._call("_lie", b"", lie(("T", json.dumps(FULL))), 1 << 16)  # a tier and then nothing
    with pytest.raises(StripError, match="nothing was written"):
        sandbox._call("_lie", b"", lie(("T", "not json"), ("H", "null"), ("E", "")), 1 << 16)


def test_the_tier_is_remembered_and_the_warning_comes_once(monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "_ANNOUNCE", True)
    monkeypatch.setattr(sandbox, "_ANNOUNCED", False)
    sandbox._note_tier(json.dumps(WEAK).encode())
    sandbox._note_tier(json.dumps(WEAK).encode())
    err = capsys.readouterr().err
    assert err.count("reduced confinement") == 1 and "--require-sandbox" in err
    monkeypatch.setattr(sandbox, "_ANNOUNCED", False)
    sandbox._note_tier(json.dumps(FULL).encode())
    assert capsys.readouterr().err == ""
    assert sandbox._TIER == FULL


def test_no_warning_unless_asked_for(monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "_ANNOUNCE", False)
    monkeypatch.setattr(sandbox, "_ANNOUNCED", False)
    sandbox._note_tier(json.dumps(WEAK).encode())
    assert capsys.readouterr().err == ""


def test_describe_names_what_is_missing():
    assert "not available" in sandbox.describe(WEAK) and "no system-call filter" in sandbox.describe(WEAK)
    assert "ABI 7" in sandbox.describe(FULL) and "filter on" in sandbox.describe(FULL)


def test_the_requirement_comes_from_the_switch_or_the_environment(monkeypatch):
    monkeypatch.setattr(sandbox, "_REQUIRE", False)
    monkeypatch.delenv("IMGSTRIP_REQUIRE_SANDBOX", raising=False)
    assert sandbox.required() is False
    monkeypatch.setenv("IMGSTRIP_REQUIRE_SANDBOX", "0")
    assert sandbox.required() is False
    monkeypatch.setenv("IMGSTRIP_REQUIRE_SANDBOX", "1")
    assert sandbox.required() is True
    monkeypatch.delenv("IMGSTRIP_REQUIRE_SANDBOX")
    sandbox.set_require(True)
    try:
        assert sandbox.required() is True
    finally:
        sandbox.set_require(False)


def test_status_reports_the_tier_or_that_the_sandbox_is_off(monkeypatch):
    status = sandbox.status(refresh=True)
    assert set(status) == {"enabled", "full", "landlock", "reads", "seccomp"} and status["enabled"] is True
    monkeypatch.setattr(sandbox, "_DEFAULT", False)
    assert sandbox.status() == {"enabled": False, "full": False, "landlock": 0, "reads": False, "seccomp": False}


def test_a_system_that_can_confine_completely_says_so(full_confinement):
    assert sandbox.status(refresh=True)["full"] is True


# ---------------------------------------------------------------------------------------------------------------
# A zygote keeps nothing of earlier files, and does not outlive a suspicious job
# ---------------------------------------------------------------------------------------------------------------


def zygote_pid() -> int:
    return sandbox.probe_limits("/nonexistent", "/nonexistent")["ppid"]


@linux
def test_a_zygote_that_served_a_crash_is_replaced():
    first = zygote_pid()
    assert zygote_pid() == first  # an ordinary job leaves it in place
    with pytest.raises(sandbox.SandboxError, match="crashed"):
        sandbox._call("_crash", b"", {}, 1 << 16)
    assert zygote_pid() != first
    assert not _alive(first)  # it ended itself after reporting the crash


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie still answers kill(0)
        return Path(f"/proc/{pid}/stat").read_text().split(")")[1].split()[0] != "Z"
    except OSError:
        return False


@linux
def test_a_zygote_is_replaced_after_a_fixed_number_of_jobs():
    seen = {zygote_pid()}
    for _ in range(sandbox._MAX_JOBS + 3):
        seen.add(zygote_pid())
    assert len(seen) >= 2


MARKER = b"IMGSTRIP-TEST-MARKER-" + os.urandom(12).hex().encode()


def memory_holds(pid: int, needle: bytes) -> bool:
    """Whether the writable memory of process ``pid`` holds ``needle``."""
    with open(f"/proc/{pid}/maps") as maps, open(f"/proc/{pid}/mem", "rb", buffering=0) as mem:
        for line in maps:
            span, perms = line.split()[:2]
            if "r" not in perms or "w" not in perms or line.rstrip().endswith(("[vvar]", "[vsyscall]")):
                continue
            start, end = (int(x, 16) for x in span.split("-"))
            try:
                mem.seek(start)
                region = mem.read(end - start)
            except (OSError, OverflowError, ValueError):
                continue
            if needle in region:
                return True
    return False


@linux
def test_a_zygote_keeps_no_copy_of_the_file_it_served_or_of_the_answer_it_relayed():
    pid = zygote_pid()
    try:
        open(f"/proc/{pid}/mem", "rb").close()
    except OSError:
        pytest.skip("this system does not let the test read the zygote's memory")
    assert memory_holds(os.getpid(), MARKER)  # the search can find it: this process holds the marker itself
    # a small file (so its buffer would stay in the heap once freed) that ends in the marker, which the worker hands
    # back (so it passes through the relay buffer too)
    file = os.urandom(20_000) + MARKER
    assert sandbox._call("_echo", file, {}, 1 << 20) == file  # in through the zygote, out through its relay
    serving = zygote_pid()
    assert serving == pid  # an ordinary job leaves the zygote in place, and it holds nothing of the two
    assert not memory_holds(serving, MARKER)


@linux
def test_the_job_input_reaches_the_worker_through_the_zygote_unchanged(fx):
    from imgstrip.core import canonical_frames

    frames = canonical_frames(fx["png_everything.png"])
    assert frames and frames[0].ndim == 3
