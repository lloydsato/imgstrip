"""2.0.7: the six issues a review of the code of 2.0.6 reproduced (after the experiments of the paper).

Every test uses synthetic inputs and throwaway processes: nothing here signals, renices or reads anything of the
person who runs the tests (a signal test targets a child of its own that only sleeps).

1. the worker could read the folder imgstrip was started from;
2. the system-call filter did not refuse the signals sent through ``fcntl`` (``F_SETOWN`` with ``F_SETSIG``);
3. the Pillow floor was declared and tested, but not checked when the tool runs;
4. the camera-RAW check parsed TIFF directories in the main process, and a crafted file made it hold thousands of
   times its size;
5. ``--verify-record`` said "ok" and exited 0 for a record signed again with any key, unless the key was pinned;
6. the filter checked the id of ``setpriority`` and ``ioprio_set`` but not their kind (a process, a group, a user);
7. (from the same review, found by reading) the web interface's same-user check let a connection through when the
   kernel's table of connections worked but did not list it.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import textwrap
import tracemalloc
from pathlib import Path

import pytest

import imgstrip
from imgstrip import _floor, record, sandbox
from imgstrip import inspect as inspector
from imgstrip.cli import main
from imgstrip.core import camera_raw_kind

from test_confinement import ALLOW, EPERM, PID, filt, full_confinement  # noqa: F401  (fixtures)

ROOT = Path(__file__).resolve().parent.parent
linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
needs_crypto = pytest.mark.skipif(not record.available(), reason="needs the cryptography library")
SRC = str(Path(imgstrip.__file__).resolve().parent.parent)  # the folder that holds the package under test


def run_python(code: str, *, cwd: Path, path: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """A fresh interpreter (not isolated: that is the point of some tests) that finds imgstrip on ``path``."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = os.pathsep.join(path)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout
    )


# --------------------------------------------------------------------------
# 1. The folder imgstrip was started from
# --------------------------------------------------------------------------


@linux
def test_the_folder_the_tool_was_started_from_is_not_a_read_root(monkeypatch, tmp_path):
    launch = tmp_path / "work" / "case"
    launch.mkdir(parents=True)
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setattr(sys, "path", [str(launch), str(other), *sys.path])
    roots = sandbox._read_roots()
    assert str(launch) not in roots and str(other) not in roots
    assert not any(r == str(tmp_path) or str(launch).startswith(r.rstrip("/") + "/") for r in roots)


@linux
def test_the_folders_the_imported_modules_came_from_stay_readable(monkeypatch, tmp_path):
    home = tmp_path / "home" / "me"
    package = home / "libs"
    (package / "thing207").mkdir(parents=True)
    (package / "thing207" / "__init__.py").write_text("")
    (package / "thing207" / "inner.py").write_text("")
    (package / "flat207.py").write_text("")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.syspath_prepend(str(package))
    import importlib

    for name in ("thing207", "thing207.inner", "flat207"):
        importlib.import_module(name)
    try:
        assert str(package) in sandbox._import_sources()
        assert str(package) in sandbox._read_roots()
    finally:
        for name in ("thing207", "thing207.inner", "flat207"):
            sys.modules.pop(name, None)
    assert str(package) not in sandbox._import_sources()  # once nothing was imported from it, it is not a source


@linux
def test_the_installation_stays_readable_even_where_nothing_was_imported_from_it(monkeypatch, tmp_path):
    home = tmp_path / "home" / "me"
    prefix = home / "project" / "venv"
    site = prefix / "lib" / "python3" / "site-packages"
    site.mkdir(parents=True)
    launch = home / "project"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "path", [str(launch), str(site), *sys.path])
    roots = sandbox._read_roots()
    assert str(site) in roots and str(prefix) in roots
    assert str(launch) not in roots  # the folder that holds the environment is not the environment


@linux
def test_a_confined_worker_cannot_read_a_file_in_the_folder_the_tool_was_started_from(tmp_path, full_confinement):  # noqa: F811
    launch, elsewhere = tmp_path / "launch", tmp_path / "elsewhere"
    launch.mkdir()
    elsewhere.mkdir()
    (launch / ".env").write_text("not for a decoder")
    (elsewhere / "victim").write_text("keep")
    (launch / "driver207.py").write_text(
        textwrap.dedent(
            f"""
            import json, os, sys
            from imgstrip import sandbox
            assert sys.path[0] == os.getcwd(), sys.path[:2]  # python -m puts the folder first
            r = sandbox.probe_limits({str(elsewhere)!r}, {str(elsewhere / "victim")!r}, secret=os.path.join(os.getcwd(), ".env"))
            print(json.dumps({{"read": r["read"], "full": r["tier"]["full"]}}))
            """
        )
    )
    done = subprocess.run(
        [sys.executable, "-m", "driver207"],
        cwd=launch,
        env={**{k: v for k, v in os.environ.items() if k != "PYTHONPATH"}, "PYTHONPATH": SRC},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout.splitlines()[-1])
    assert got["full"] and got["read"].startswith("no"), got


@linux
def test_cleaning_works_from_the_folder_where_the_source_is_started(tmp_path, full_confinement):  # noqa: F811
    """The narrower list still holds what the decoder needs: a source checkout started from its own folder."""
    from PIL import Image

    Image.new("RGB", (16, 16), (200, 30, 90)).save(tmp_path / "in.png")
    done = run_python(
        "import sys; from imgstrip.cli import main; sys.exit(main(['in.png', '-o', 'out.png', '--require-sandbox']))",
        cwd=tmp_path,
        path=[SRC],
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert (tmp_path / "out.png").stat().st_size > 0


# --------------------------------------------------------------------------
# 2 and 6. The system-call filter
# --------------------------------------------------------------------------

F_GETFD, F_SETFD, F_GETFL, F_SETFL, F_SETOWN, F_GETOWN, F_SETSIG, F_GETSIG, F_SETOWN_EX, F_GETOWN_EX = (
    1, 2, 3, 4, 8, 9, 10, 11, 15, 16
)  # fmt: skip
F_DUPFD_CLOEXEC, F_ADD_SEALS = 1030, 1033


@pytest.mark.parametrize("command", [F_SETOWN, F_SETSIG, F_SETOWN_EX])
def test_fcntl_cannot_name_a_process_to_be_signalled(filt, command):  # noqa: F811
    assert filt("fcntl", 5, command, PID + 1) == EPERM
    assert filt("fcntl", 5, command, 0) == EPERM
    assert filt("fcntl", 5, command, -1 & 0xFFFFFFFF) == EPERM  # a process group


@pytest.mark.parametrize(
    "command", [F_GETFD, F_SETFD, F_GETFL, F_SETFL, F_GETOWN, F_GETSIG, F_GETOWN_EX, F_DUPFD_CLOEXEC, F_ADD_SEALS]
)
def test_the_other_fcntl_commands_a_decoder_uses_pass(filt, command):  # noqa: F811
    assert filt("fcntl", 5, command, 0) == ALLOW


def test_fcntl_is_in_both_tables_and_the_commands_are_the_kernels():
    for arch in ("x86_64", "aarch64"):
        assert "fcntl" in imgstrip._seccomp._NUMBERS[arch]
    assert (imgstrip._seccomp.F_SETOWN, imgstrip._seccomp.F_SETSIG, imgstrip._seccomp.F_SETOWN_EX) == (8, 10, 15)
    header = Path("/usr/include/asm-generic/fcntl.h")
    if header.exists():
        text = header.read_text()
        for name, value in (("F_SETOWN", 8), ("F_SETSIG", 10), ("F_SETOWN_EX", 15)):
            assert f"#define {name}\t{value}" in text, name


def test_setpriority_and_ioprio_set_work_on_one_process_only_and_the_caller_only(filt):  # noqa: F811
    one = {"setpriority": 0, "ioprio_set": 1}  # PRIO_PROCESS, IOPRIO_WHO_PROCESS
    for name, process in one.items():
        assert filt(name, process, 0, 5) == ALLOW  # the caller
        assert filt(name, process, PID + 1, 5) == EPERM  # another process
        for kind in (0, 1, 2, 3, 4):
            if kind != process:  # a group, a user: with id 0 they mean the caller's group and every process of the user
                assert filt(name, kind, 0, 5) == EPERM, (name, kind)
                assert filt(name, kind, PID, 5) == EPERM, (name, kind)


@linux
def test_a_filtered_process_cannot_signal_an_outside_process_through_fcntl(tmp_path):
    """The real kernel, the filter alone (Landlock's signal scope would stop this on 6.12 or newer, which hides it)."""
    if imgstrip._seccomp.machine() is None:
        pytest.skip("no filter for this machine")
    done = run_python(
        """
        import ctypes, fcntl, os, signal, time
        from imgstrip import _seccomp

        victim = os.fork()
        if victim == 0:
            time.sleep(20)
            os._exit(0)
        r, w = os.pipe()
        worker = os.fork()
        if worker == 0:
            libc = ctypes.CDLL(None, use_errno=True)
            libc.prctl(38, 1, 0, 0, 0)
            installed = _seccomp.install(libc, os.getpid())
            a, b = os.pipe()
            try:
                fcntl.fcntl(a, fcntl.F_SETOWN, victim)
                fcntl.fcntl(a, 10, signal.SIGTERM)  # F_SETSIG
                fcntl.fcntl(a, fcntl.F_SETFL, fcntl.fcntl(a, fcntl.F_GETFL) | os.O_ASYNC)
                os.write(b, b"x")
                said = "accepted"
            except OSError as exc:
                said = "refused " + exc.strerror
            os.write(w, f"{installed}|{said}".encode())
            os._exit(0)
        os.close(w)
        said = os.read(r, 200).decode()
        os.waitpid(worker, 0)
        time.sleep(0.5)
        pid, status = os.waitpid(victim, os.WNOHANG)
        died = pid == victim and os.WIFSIGNALED(status)
        if not died:
            os.kill(victim, signal.SIGKILL)
            os.waitpid(victim, 0)
        print(said, died)
        """,
        cwd=tmp_path,
        path=[SRC],
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["True|refused", "Operation", "not", "permitted", "False"], done.stdout


@linux
def test_a_filtered_process_cannot_renice_its_group(tmp_path):
    if imgstrip._seccomp.machine() is None:
        pytest.skip("no filter for this machine")
    done = run_python(
        """
        import ctypes, os, time
        from imgstrip import _seccomp

        base = os.getpriority(os.PRIO_PROCESS, 0)  # 0, or what the test run was started with (nice, a service manager)
        lead = os.fork()
        if lead == 0:
            os.setsid()  # a group of its own
            sibling = os.fork()
            if sibling == 0:
                time.sleep(10)
                os._exit(0)
            worker = os.fork()
            if worker == 0:
                libc = ctypes.CDLL(None, use_errno=True)
                libc.prctl(38, 1, 0, 0, 0)
                _seccomp.install(libc, os.getpid())
                try:
                    os.setpriority(os.PRIO_PGRP, 0, 19 if base < 19 else base)
                    said = "accepted"
                except OSError as exc:
                    said = "refused"
                print(said, flush=True)
                os._exit(0)
            os.waitpid(worker, 0)
            print("sibling nice unchanged", os.getpriority(os.PRIO_PROCESS, sibling) == base, flush=True)
            os.kill(sibling, 9)
            os.waitpid(sibling, 0)
            os._exit(0)
        os.waitpid(lead, 0)
        """,
        cwd=tmp_path,
        path=[SRC],
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split("\n")[:2] == ["refused", "sibling nice unchanged True"], done.stdout


def test_a_confined_worker_cannot_name_another_process_as_a_signal_target_or_renice_a_group(tmp_path, full_confinement):  # noqa: F811
    victim = tmp_path / "victim"
    victim.write_text("keep")
    limits = sandbox.probe_limits(str(tmp_path), str(victim))
    assert limits["tier"]["full"]
    for what in ("async_owner", "group_priority", "other_priority", "signal"):
        assert limits[what].startswith("no"), (what, limits[what])
    assert limits["own_priority"] == "yes" and limits["own_limit"] == "yes"


def test_the_self_test_checks_both_new_things():
    text = (ROOT / "src" / "imgstrip" / "selftest.py").read_text()
    assert '"group_priority"' in text and '"async_owner"' in text


# --------------------------------------------------------------------------
# 3. The Pillow floor, checked when the tool runs
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [("13.0.0", (13, 0, 0)), ("13.1", (13, 1, 0)), ("14.2.3.post1", (14, 2, 3)), ("12.3.0", (12, 3, 0)),
     ("13.0.0.dev0", (13, 0, 0)), ("", None), ("dev", None), (None, None), (13, None)],
)  # fmt: skip
def test_a_version_is_read_from_its_first_three_numbers(text, expected):
    assert _floor.parse(text) == expected


@pytest.mark.parametrize("version", ["12.3.0", "12.99.99", "11.0.0", "9.5.0", "unknown", "", None])
def test_a_pillow_older_than_the_floor_is_refused(monkeypatch, version):
    import PIL

    monkeypatch.setattr(PIL, "__version__", version, raising=False)
    with pytest.raises(ImportError, match=r"Pillow 13\.0\.0 or newer.*GHSA-q45j-58j2-45rg.*pip install"):
        _floor.check()


@pytest.mark.parametrize("version", ["13.0.0", "13.0.1", "13.1.0rc1", "14.0.0", "99.0"])
def test_a_pillow_at_or_above_the_floor_is_accepted(monkeypatch, version):
    import PIL

    monkeypatch.setattr(PIL, "__version__", version, raising=False)
    _floor.check()


def test_the_floor_in_the_code_is_the_floor_in_the_package_metadata():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    import re

    declared = re.search(r'"Pillow>=([0-9.]+)"', text).group(1)
    assert _floor.parse(declared) == _floor.PILLOW_FLOOR
    assert "13.0.0" in (ROOT / "src" / "imgstrip" / "_floor.py").read_text()  # the advice names the same version


def test_importing_the_package_on_a_pillow_below_the_floor_fails_with_the_reason(tmp_path):
    fake = tmp_path / "fake"
    (fake / "PIL").mkdir(parents=True)
    (fake / "PIL" / "__init__.py").write_text('__version__ = "12.3.0"\n')
    done = run_python("import imgstrip", cwd=tmp_path, path=[str(fake), SRC])
    assert done.returncode != 0
    last = done.stderr.strip().splitlines()[-1]
    assert last.startswith("ImportError: imgstrip needs Pillow 13.0.0 or newer, and this is 12.3.0")
    assert "GHSA-q45j-58j2-45rg" in last


def test_the_package_imports_on_the_pillow_that_is_installed():
    # The test run itself is the proof (the package is imported at the top of this file); here is the reason it can be.
    import PIL

    assert _floor.parse(PIL.__version__) >= _floor.PILLOW_FLOOR


def test_the_version_is_at_least_2_0_7():
    assert tuple(int(n) for n in imgstrip.__version__.split(".")) >= (2, 0, 7)


# --------------------------------------------------------------------------
# 4. A crafted TIFF and the camera-RAW check
# --------------------------------------------------------------------------


def crafted_tiff(size: int = 512 * 1024, entries: int = 4096, tag0: int = 60000) -> bytes:
    """A classic TIFF whose ``entries`` tags each hold ``size - 16`` bytes, at offset 0: each points at nearly all of it."""
    head = b"II*\x00" + struct.pack("<I", 8)
    ifd = struct.pack("<H", entries) + b"".join(
        struct.pack("<HHII", tag0 + i % 5000, 1, size - 16, 0) for i in range(entries)
    ) + struct.pack("<I", 0)
    body = head + ifd
    return body + b"\x00" * (size - len(body))


def peak_mib(function, *args) -> tuple[object, float]:
    tracemalloc.start()
    try:
        result = function(*args)
        return result, tracemalloc.get_traced_memory()[1] / (1 << 20)
    finally:
        tracemalloc.stop()


def test_the_camera_raw_check_holds_no_copy_of_what_the_tags_point_at():
    data = crafted_tiff()
    result, peak = peak_mib(camera_raw_kind, data)
    assert result is None
    assert peak < 4, f"{peak:.0f} MiB for a file of 0.5 MiB"  # 2.0.6 held about 2,000 MiB


def test_the_inspector_refuses_tag_values_that_overlap_far_beyond_the_file():
    data = crafted_tiff()
    report, peak = peak_mib(inspector.inspect_bytes, data)
    assert peak < 16, f"{peak:.0f} MiB"
    assert report.error and "overlap" in report.error and report.format == "TIFF"


def test_a_scan_of_the_directory_for_its_tags_costs_nothing_per_tag():
    data = crafted_tiff()
    t = inspector._Tiff(data)
    tags, peak = peak_mib(t.tag_numbers, 8)
    assert len(tags) == 4096
    assert peak < 1


def test_a_dng_is_still_recognised_and_other_tiffs_are_not(tmp_path):
    def tiff(tag_numbers):
        head = b"II*\x00" + struct.pack("<I", 8)
        ifd = struct.pack("<H", len(tag_numbers)) + b"".join(struct.pack("<HHII", t, 3, 1, 1) for t in tag_numbers)
        return head + ifd + struct.pack("<I", 0)

    assert camera_raw_kind(tiff([256, 257, 50706])) == "DNG"
    assert camera_raw_kind(tiff([256, 257, 258])) is None
    assert camera_raw_kind(crafted_tiff(entries=4097)) is None  # an implausible entry count is no DNG
    assert camera_raw_kind(b"II*\x00") is None


def test_tags_that_share_their_bytes_a_little_are_still_read():
    """A few entries may point at the same value (writers do reuse one); the limit is far above that."""
    value = bytes(range(32))
    head = b"II*\x00" + struct.pack("<I", 8)
    entries = [struct.pack("<HHII", 40000 + i, 1, 32, 8 + 2 + 12 * 20 + 4) for i in range(20)]
    ifd = struct.pack("<H", 20) + b"".join(entries) + struct.pack("<I", 0)
    data = head + ifd + value
    t = inspector._Tiff(data)
    got, _ = t.read_ifd(8)
    assert len(got) == 20 and all(e[4] == value for e in got)


def test_the_value_budget_follows_the_size_of_the_file():
    assert inspector._Tiff(crafted_tiff(1 << 20)).value_budget > inspector._Tiff(crafted_tiff(1 << 10)).value_budget
    assert inspector._Tiff(crafted_tiff(1 << 20)).value_budget < 16 * (1 << 20)


def test_a_crafted_tiff_is_still_refused_by_the_tool(tmp_path):
    from imgstrip import Options, StripError, strip_image

    with pytest.raises(StripError):
        strip_image(crafted_tiff(), Options())


# --------------------------------------------------------------------------
# 5. A record is a pass only for a key you trust
# --------------------------------------------------------------------------


def make_signed(tmp_path: Path, name: str, **kw) -> tuple[Path, bytes]:
    from test_record import make_body

    private, public, _ = record.generate_key()
    path = tmp_path / name
    path.write_bytes(record.seal(make_body(**kw), record.load_signer(private)))
    return path, public


@needs_crypto
def test_a_signature_that_was_not_compared_with_a_key_is_not_a_pass():
    from test_record import make_body

    private, public, _ = record.generate_key()
    document = record.seal(make_body(), record.load_signer(private))
    unpinned = record.verify(document)
    assert unpinned.status == record.SIGNED and not unpinned.ok and unpinned.as_dict()["ok"] is False
    pinned = record.verify(document, record.load_public(public))
    assert pinned.status == record.TRUSTED and pinned.ok and pinned.as_dict()["ok"] is True


@needs_crypto
def test_a_record_edited_and_signed_again_with_another_key_does_not_pass_without_a_pinned_key(tmp_path, capsys):
    genuine, genuine_public = make_signed(tmp_path, "genuine.json", operator="Officer A")
    forged, _ = make_signed(tmp_path, "forged.json", operator="Officer A")  # a stranger's key
    pub = tmp_path / "genuine.pub"
    pub.write_bytes(genuine_public)

    assert main(["--verify-record", str(genuine), "--record-pubkey", str(pub)]) == 0
    assert capsys.readouterr().out.startswith("ok ")

    assert main(["--verify-record", str(forged)]) == 3  # no key pinned: 2.0.6 said "ok" and exited 0
    out = capsys.readouterr().out
    assert "UNCHECKED" in out and not out.startswith("ok") and "--record-pubkey" in out

    assert main(["--verify-record", str(forged), "--record-pubkey", str(pub)]) == 3  # pinned: invalid
    assert "PROBLEM" in capsys.readouterr().out


@needs_crypto
def test_a_genuine_record_without_a_pinned_key_asks_for_the_key_and_exits_with_the_status_of_a_problem(tmp_path, capsys):
    genuine, _ = make_signed(tmp_path, "genuine.json")
    assert main(["--verify-record", str(genuine), "--json"]) == 3
    entry = json.loads(capsys.readouterr().out)["records"][0]
    assert entry["status"] == "signed" and entry["ok"] is False and "use --record-pubkey" in entry["problems"][0]


# --------------------------------------------------------------------------
# What did not change
# --------------------------------------------------------------------------


def test_the_hardening_of_2_0_6_still_holds(filt):  # noqa: F811
    for name in ("sched_setparam", "sched_setscheduler", "sched_setaffinity", "sched_setattr", "migrate_pages", "move_pages"):
        assert filt(name, 0) == ALLOW and filt(name, PID + 1) == EPERM
    assert filt("prlimit64", 0, 7) == ALLOW and filt("prlimit64", PID + 1, 7) == EPERM
    assert filt("tgkill", PID, 5, 6) == ALLOW and filt("tgkill", PID + 1, 5, 9) == EPERM
    assert filt("kill", 1, 9) == EPERM


# --------------------------------------------------------------------------
# 7. The web interface's same-user check
# --------------------------------------------------------------------------

TABLE_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def table_line(client: str, server_port: int, uid: int) -> str:
    def hexaddr(host: str, port: int) -> str:
        return "%08X:%04X" % (int.from_bytes(socket.inet_aton(host), "little"), port)

    return f"   0: {hexaddr('127.0.0.1', int(client))} {hexaddr('127.0.0.1', server_port)} 01 0:0 00:0 0 {uid} 0 1 1 1\n"


@pytest.fixture
def fake_table(monkeypatch):
    """Make /proc/net/tcp say what a test wants; every other file opens as usual."""
    import builtins
    import io

    real = builtins.open
    content = {"text": None}

    def opener(file, *args, **kwargs):
        if file == "/proc/net/tcp":
            if content["text"] is None:
                raise OSError("no table")
            return io.StringIO(content["text"])
        return real(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", opener)
    return content


def test_a_connection_the_working_table_does_not_list_is_nobodys_and_is_refused(fake_table, monkeypatch):
    from imgstrip.web import server as web

    fake_table["text"] = TABLE_HEADER + table_line("40000", 8765, os.getuid() + 5)
    assert web.peer_uid(("127.0.0.1", 40000), 8765) == os.getuid() + 5  # listed: its owner
    assert web.peer_uid(("127.0.0.1", 40001), 8765) == web.NOT_LISTED  # not listed: gone
    server = web.Server.__new__(web.Server)
    server.server_address, server.any_user = ("127.0.0.1", 8765), False
    assert not server.verify_request(None, ("127.0.0.1", 40001))
    assert not server.verify_request(None, ("127.0.0.1", 40000))  # listed, another user's
    fake_table["text"] = TABLE_HEADER + table_line("40000", 8765, os.getuid())
    assert server.verify_request(None, ("127.0.0.1", 40000))
    server.any_user = True
    assert server.verify_request(None, ("127.0.0.1", 40001))  # --any-user serves everyone


def test_where_the_table_cannot_tell_the_check_is_skipped_as_documented(fake_table):
    from imgstrip.web import server as web

    server = web.Server.__new__(web.Server)
    server.server_address, server.any_user = ("127.0.0.1", 8765), False
    fake_table["text"] = None  # unreadable (no /proc, another system)
    assert web.peer_uid(("127.0.0.1", 40000), 8765) is None and server.verify_request(None, ("127.0.0.1", 40000))
    fake_table["text"] = TABLE_HEADER  # readable, lists nothing: it does not work, so it cannot tell
    assert web.peer_uid(("127.0.0.1", 40000), 8765) is None and server.verify_request(None, ("127.0.0.1", 40000))
    assert web.peer_uid(("::1", 1, 0, 0), 8765) is None  # not IPv4
    assert web.peer_uid("garbage", 8765) is None
