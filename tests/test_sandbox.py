"""The decoder sandbox (review item 4): the input is decoded in a separate, restricted process that hands back only
pixels and a few typed values, and this side checks every one of them."""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import socket
import subprocess
import sys
import textwrap
import time

import numpy as np
import pytest

from imgstrip import analysis, cli, core, fixtures, sandbox
from imgstrip.core import Frame, Options, StripError, canonical_frames, strip_image
from imgstrip.web import server as web

NAMES = list(fixtures.all_fixtures())
# Modes whose output is a function of the input alone (no random scrub), so both paths must give the same bytes.
MODES = {
    "default": Options(),
    "png": Options(format="PNG"),
    "tiff": Options(format="TIFF"),
    "webp": Options(format="WEBP"),
    "icc-sanitize": Options(icc="sanitize"),
    "jpeg-lossless": Options(jpeg_lossless=True),
    "jpeg-canonical": Options(scrub_dct="canonical"),
}


def outcome(data: bytes, options: Options, on: bool):
    try:
        r = strip_image(data, dataclasses.replace(options, sandbox=on))
    except StripError as exc:
        return type(exc).__name__, str(exc)
    return r.data, r.output_format, r.actions, r.warnings, r.removed, r.verified_by, r.pixels_exact, r.dct_scrub


@pytest.mark.parametrize("mode", list(MODES))
@pytest.mark.parametrize("name", NAMES)
def test_the_sandbox_changes_nothing_in_the_output(fx, name, mode):
    assert outcome(fx[name], MODES[mode], True) == outcome(fx[name], MODES[mode], False)


def test_the_random_scrubs_are_the_same_apart_from_their_randomness(fx):
    for options in (Options(scrub_lsb=True), Options(scrub_dct="full", dct_min_psnr=0), Options(scrub_prnu=True)):
        for name in ("png_everything.png", "jpeg_everything.jpg"):
            on, off = outcome(fx[name], options, True), outcome(fx[name], options, False)
            if len(off) == 2:  # refused (the DCT scrub of a PNG): for the same reason
                assert on == off
                continue
            # the format, the warnings and what was done agree (the measured PSNR in one action varies)
            assert (on[1], on[3], len(on[2])) == (off[1], off[3], len(off[2])), (name, options)


@pytest.mark.parametrize("name", NAMES)
def test_comparisons_scans_facts_and_previews_agree(fx, monkeypatch, name):
    data = fx[name]

    def both(fn):
        results = []
        for on in (True, False):
            monkeypatch.setattr(sandbox, "_DEFAULT", on)
            try:
                results.append(fn())
            except StripError as exc:
                results.append(str(exc))
        return results

    on, off = both(lambda: canonical_frames(data))
    assert type(on) is type(off)
    if isinstance(on, list):
        assert len(on) == len(off) and all(
            a.dtype == b.dtype and np.array_equal(a, b) for a, b in zip(on, off, strict=True)
        )
    on, off = both(lambda: analysis.scan(data))
    assert on == off
    assert both(lambda: (web._probe(data), web.image_facts(data))) == both(lambda: web._survey(data))
    on, off = both(lambda: web._survey(data))
    assert on == off
    on, off = both(lambda: web.preview_bytes(data))
    assert on == off


def test_the_sandbox_is_on_by_default(fx, monkeypatch):
    seen = []
    real = sandbox.decode
    monkeypatch.setattr(sandbox, "decode", lambda data, options: seen.append(1) or real(data, options))
    strip_image(fx["png_everything.png"])
    assert seen
    seen.clear()
    strip_image(fx["png_everything.png"], Options(sandbox=False))
    assert not seen


def test_no_sandbox_on_the_command_line(fx, tmp_path, monkeypatch):
    src = tmp_path / "in.png"
    src.write_bytes(fx["png_everything.png"])

    def refuse(*_args, **_kw):
        raise AssertionError("the sandbox was used")

    monkeypatch.setattr(sandbox, "_call", refuse)
    monkeypatch.setattr(sandbox, "_DEFAULT", True)  # restored afterwards: main() switches it off for the process
    assert cli.main(["--no-sandbox", "--quiet", str(src), "-o", str(tmp_path / "out.png")]) == 0
    assert cli.main(["--no-sandbox", "--check", "--deep", "--quiet", str(src)]) == cli.EXIT_FOUND
    assert (tmp_path / "out.png").exists()


# -- what a worker may still do --------------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="on Windows the worker is only a process of its own")
def test_a_worker_cannot_write_and_keeps_nothing_of_this_process(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGSTRIP_TEST_SECRET", "hunter2")
    monkeypatch.setattr(sandbox, "_POOL", sandbox._Pool())  # a zygote started now, with the secret in os.environ
    victim = tmp_path / "victim"
    victim.write_text("keep me")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        limits = sandbox.probe_limits(str(tmp_path), str(victim), listener.getsockname()[1])
    finally:
        listener.close()
    assert limits["write"].startswith("no"), limits
    assert limits["fsize"] == [0, 0] and limits["core"] == [0, 0], limits
    assert limits["cpu"][0] == sandbox._CPU_SECONDS
    assert limits["pid"] != os.getpid()
    assert "IMGSTRIP_TEST_SECRET" not in limits["environment"]
    assert victim.read_text() == "keep me" and not (tmp_path / "written").exists()
    if limits["landlock"]:  # Linux 5.13 and later
        assert limits["create"].startswith("no") and limits["remove"].startswith("no"), limits
        assert limits["peek"].startswith("no"), limits
        assert not (tmp_path / "created").exists()
    if limits["landlock"] >= 4:
        assert limits["connect"].startswith("no"), limits


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Landlock is Linux only")
def test_landlock_is_used_where_the_kernel_has_it(tmp_path):
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = libc.syscall(444, None, ctypes.c_size_t(0), ctypes.c_uint32(1))
    limits = sandbox.probe_limits(str(tmp_path), str(tmp_path / "none"))
    assert limits["landlock"] == max(0, abi)


# -- failures ----------------------------------------------------------------------------------------------------


def test_a_crashing_worker_is_an_error_and_the_next_file_still_works(fx):
    with pytest.raises(sandbox.SandboxError, match="crashed .* nothing was written"):
        sandbox._call("_crash", b"", {}, 1 << 16)
    assert strip_image(fx["png_everything.png"]).data


def test_a_hanging_worker_is_stopped(fx):
    start = time.monotonic()
    with pytest.raises(sandbox.SandboxError, match="longer than 2 s"):
        sandbox._call("_hang", b"", {}, 1 << 16, timeout=2)
    assert time.monotonic() - start < 30
    assert strip_image(fx["png_everything.png"]).data


def test_a_decoder_failure_is_reported_as_it_is_in_process(fx):
    broken = fx["png_everything.png"][:200]
    with pytest.raises(StripError) as on:
        strip_image(broken)
    with pytest.raises(StripError) as off:
        strip_image(broken, Options(sandbox=False))
    assert str(on.value) == str(off.value)


def test_a_sandbox_that_cannot_start_refuses_every_file(fx, monkeypatch):
    monkeypatch.setattr(sandbox, "_POOL", sandbox._Pool())
    monkeypatch.setattr(sys, "executable", "")
    with pytest.raises(sandbox.SandboxError, match="--no-sandbox"):
        strip_image(fx["png_everything.png"])
    monkeypatch.setattr(sys, "executable", "/nonexistent/python")
    with pytest.raises(sandbox.SandboxError, match="--no-sandbox"):
        strip_image(fx["png_everything.png"])


def frames(*items):
    return {"frames": [[kind, payload] for kind, payload in items]}


@pytest.mark.parametrize(
    "answer, why",
    [
        (frames(("H", "not json")), "sandbox failed"),
        (frames(("Q", "")), "unexpected frame"),
        (frames(("H", json.dumps({"$bytes": 0, "size": 100})), ("B", "x" * 10), ("E", "")), "shorter than declared"),
        (frames(("H", json.dumps({"$array": 0, "dtype": "|u1", "shape": [1 << 20]}))), "larger than the job allows"),
        (frames(("H", json.dumps({"$array": 0, "dtype": "|O", "shape": [1]}))), "may not send"),
        (frames(("H", json.dumps({"$image": 0, "mode": "RGB", "size": [0, 5]}))), "impossible image size"),
        (frames(("H", json.dumps({"$type": "Options", "format": "PNG"}))), "type the sandbox may not send"),
        (frames(("H", json.dumps({"$type": "Finding", "category": "x", "label": "y", "evil": 1}))), "unknown fields"),
        (frames(("H", json.dumps(json.loads("[" * 40 + "]" * 40)))), "nested too deeply"),
        (frames(("H", json.dumps({"$bytes": 0, "size": 4})), ("B", "abcd"), ("B", "more"), ("E", "")), "more data"),
        (frames(("H", json.dumps(1))), "before its answer was complete"),  # no end frame
    ],
)
def test_answers_that_break_the_rules_are_refused(answer, why):
    with pytest.raises(sandbox.SandboxError, match=why):
        sandbox._call("_lie", b"", answer, 1 << 16)


def test_an_error_frame_becomes_the_error_it_names():
    with pytest.raises(StripError, match="^nope$") as exc:
        sandbox._call("_lie", b"", frames(("X", json.dumps({"kind": "strip", "message": "nope"}))), 1 << 16)
    assert not isinstance(exc.value, sandbox.SandboxError)


# -- what the decode stage may hand over ---------------------------------------------------------------------


def decoded_of(data: bytes, options: Options):
    return core._Job(data, options, None).decode()


def tamper(d, how):
    d = copy.deepcopy(d)
    how(d)
    return d


def _frame(d, **changes):
    for k, v in changes.items():
        setattr(d.frames[0], k, v)


PIXEL_LIES = {
    "format the options do not ask for": lambda d: setattr(d, "out", "TIFF"),
    "unknown format": lambda d: setattr(d, "src", "EXE"),
    "unsanitised profile": lambda d: setattr(d, "embed_icc", b"\0" * 200),
    "report carries more": lambda d: d.report.info.update(exif=b"secret"),
    "report lies about chroma": lambda d: d.report.info.update(chroma="4:1:1"),
    "unknown finding category": lambda d: d.report.findings.append(type(d.report.findings[0])("Secret", "x")),
    "negative loop": lambda d: setattr(d, "loop", -1),
    "huge loop": lambda d: setattr(d, "loop", 1 << 20),
    "negative duration": lambda d: _frame(d, duration=-5),
    "two sets of pixels": lambda d: _frame(d, array=np.zeros((2, 2, 3), np.uint16)),
    "no pixels": lambda d: _frame(d, image=None),
    "frames of two sizes": lambda d: d.frames.append(Frame(image=d.frames[0].image.resize((3, 3)))),
    "a palette transparency on RGB": lambda d: _frame(d, alpha=[0, 255]),
    "a subsampling that does not exist": lambda d: setattr(d, "source_subsampling", 7),
    "jpeg fields on pixels": lambda d: setattr(d, "rebuilt", b"\xff\xd8"),
    "not a bool": lambda d: setattr(d, "exact", "yes"),
    "messages that are not text": lambda d: d.warnings.append(b"bytes"),
}


@pytest.mark.parametrize("lie", list(PIXEL_LIES))
def test_the_decode_stage_cannot_hand_over_what_it_may_not(fx, lie):
    options = Options()
    good = decoded_of(fx["png_everything.png"], options)
    good.report.info = {k: v for k, v in good.report.info.items() if k in sandbox._REPORT_INFO}
    sandbox._check_decoded(good, options, fx["png_everything.png"])
    with pytest.raises(sandbox.SandboxError):
        sandbox._check_decoded(tamper(good, PIXEL_LIES[lie]), options, fx["png_everything.png"])


def test_a_frame_above_the_pixel_limit_is_refused(fx):
    options = Options(max_pixels=10_000)
    data = fx["png_everything.png"]
    good = decoded_of(data, Options())
    good.report.info = {}
    with pytest.raises(sandbox.SandboxError, match="pixel limit"):
        sandbox._check_decoded(good, options, data)


def _coef(d, **changes):
    for k, v in changes.items():
        setattr(d.coefficients.comps[0], k, v)


JPEG_LIES = {
    "a rebuilt file in a coefficient mode": lambda d: setattr(d, "rebuilt", b"\xff\xd8\xff\xd9"),
    "orientation 9": lambda d: setattr(d, "orientation", 9),
    "pixels of another type": lambda d: setattr(d, "original", d.original.astype(np.uint16)),
    "coefficients of another type": lambda d: _coef(d, coef=d.coefficients.comps[0].coef.astype(np.int32)),
    "a zero quantiser": lambda d: _coef(d, qt=np.zeros(64, np.uint16)),
    "a block grid that does not fit": lambda d: setattr(d.coefficients, "mcus_x", d.coefficients.mcus_x + 1),
    "sampling factor 5": lambda d: _coef(d, h=5),
    "frames as well": lambda d: d.frames.append(Frame()),
}


@pytest.mark.parametrize("lie", list(JPEG_LIES))
def test_the_jpeg_modes_cannot_hand_over_what_they_may_not(fx, lie):
    options = Options(scrub_dct="canonical")
    data = fx["jpeg_everything.jpg"]
    good = decoded_of(data, options)
    good.report.info = {k: v for k, v in good.report.info.items() if k in sandbox._REPORT_INFO}
    sandbox._check_decoded(good, options, data)
    with pytest.raises(sandbox.SandboxError):
        sandbox._check_decoded(tamper(good, JPEG_LIES[lie]), options, data)


# -- processes ---------------------------------------------------------------------------------------------------


def test_a_script_without_a_main_guard_is_not_run_again(tmp_path):
    script = tmp_path / "use_it.py"
    script.write_text(
        textwrap.dedent(
            """
            print("module body ran")
            from imgstrip import fixtures, strip_image
            result = strip_image(fixtures.all_fixtures()["png_everything.png"])
            print("cleaned", len(result.data) > 0)
            """
        )
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=120, env=env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == ["module body ran", "cleaned True"]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_a_forked_child_starts_its_own_zygote(fx):
    strip_image(fx["png_everything.png"])  # leaves an idle zygote in this process's pool
    assert sandbox._POOL.idle
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child
        code = 1
        try:
            ok = strip_image(fixtures.all_fixtures()["png_everything.png"]).data != b""
            code = 0 if ok and all(p.pid == os.getpid() for p in sandbox._POOL.idle) else 2
        finally:
            os.write(write_end, bytes([code]))
            os._exit(0)
    os.close(write_end)
    status = os.read(read_end, 1)
    os.close(read_end)
    os.waitpid(pid, 0)
    assert status == b"\0"
    assert strip_image(fx["png_everything.png"]).data  # the parent's zygotes still work


def test_parallel_batches_use_the_sandbox_or_not_as_asked(tmp_path, fx, monkeypatch):
    monkeypatch.setattr(sandbox, "_DEFAULT", True)  # main() switches it off for the process with --no-sandbox
    files = []
    for i in range(4):
        files.append(tmp_path / f"in{i}.png")
        files[-1].write_bytes(fx["png_everything.png"])
    for flags in ([], ["--no-sandbox"]):
        out = tmp_path / ("out" + "".join(flags))
        out.mkdir()
        assert cli.main([*flags, "-j", "2", "--quiet", "-o", str(out), *map(str, files)]) == 0
        assert len(list(out.iterdir())) == 4


def test_threads_share_the_pool(fx):
    import concurrent.futures

    data = fx["jpeg_everything.jpg"]
    want = strip_image(data, Options(sandbox=False)).data
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        got = list(pool.map(lambda _i: strip_image(data).data, range(8)))
    assert all(g == want for g in got)
