"""Batch parallelism and the parallel self-test: same results as serial, in the same order, failures isolated."""

import hashlib
import json

import pytest

from imgstrip import cli, selftest
from imgstrip.cli import AUTO_PARALLEL_MIN, _worker_count, main
from imgstrip.core import Options


@pytest.mark.parametrize(
    ("requested", "tasks", "cores", "expected"),
    [
        (None, 0, 8, 1),
        (None, 1, 8, 1),
        (None, AUTO_PARALLEL_MIN - 1, 8, 1),  # small batches avoid the worker start-up cost
        (None, AUTO_PARALLEL_MIN, 8, AUTO_PARALLEL_MIN),  # never more workers than files
        (None, 50, 8, 8),
        (None, 50, 1, 1),  # single core machine
        (None, 50, None, 1),  # os.cpu_count() can be None
        (1, 50, 8, 1),  # -j 1 forces serial
        (3, 2, 8, 3),  # an explicit -j is honoured as given for any real batch
        (3, 1, 8, 1),  # ... but a single file never spawns a pool
    ],
)
def test_worker_count(monkeypatch, requested, tasks, cores, expected):
    monkeypatch.setattr(cli.os, "cpu_count", lambda: cores)
    assert _worker_count(requested, tasks) == expected


@pytest.fixture
def batch(tmp_path, fx):
    """Eight inputs of mixed formats, one of them corrupt, in a directory."""
    src = tmp_path / "in"
    src.mkdir()
    names = ["jpeg_everything.jpg", "png_everything.png"]
    names += [n for n in fx if n.endswith((".webp", ".gif", ".bmp"))][:4]
    for i, name in enumerate(names):
        (src / f"f{i:02d}_{name}").write_bytes(fx[name])
    (src / "f99_corrupt.jpg").write_bytes(b"\xff\xd8\xff\xe0 this is not a jpeg")
    return src


def _digest(directory):
    h = hashlib.sha256()
    for p in sorted(directory.iterdir()):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def _run(batch, out, capsys, *extra):
    code = main(["-r", str(batch), "-o", str(out), "--json", *extra])
    return code, json.loads(capsys.readouterr().out)["files"]


def test_default_parallel_batch_matches_serial(batch, tmp_path, capsys):
    serial_out, auto_out, forced_out = tmp_path / "s", tmp_path / "a", tmp_path / "f"
    code_s, rec_s = _run(batch, serial_out, capsys, "-j", "1")
    code_a, rec_a = _run(batch, auto_out, capsys)
    code_f, rec_f = _run(batch, forced_out, capsys, "-j", "3")
    assert code_s == code_a == code_f == 1  # the corrupt file fails, the rest are cleaned
    assert _digest(serial_out) == _digest(auto_out) == _digest(forced_out)
    assert len(list(serial_out.iterdir())) == len(list(batch.iterdir())) - 1

    def order(records):  # reporting order follows the input order, not completion order
        return [(r["file"], r["status"]) for r in records]

    assert order(rec_s) == order(rec_a) == order(rec_f)
    assert [f for f, _ in order(rec_a)] == sorted(f for f, _ in order(rec_a))
    assert [s for _, s in order(rec_a)].count("failed") == 1


def test_one_bad_file_does_not_stop_the_parallel_batch(batch, tmp_path, capsys):
    code, records = _run(batch, tmp_path / "o", capsys, "-j", "4")
    assert code == 1
    assert [r["status"] for r in records if "f99_corrupt" in r["file"]] == ["failed"]
    assert sum(r["status"] == "cleaned" for r in records) == len(records) - 1
    assert len(list((tmp_path / "o").iterdir())) == len(list(batch.iterdir())) - 1


def test_more_files_than_the_in_flight_window(tmp_path, fx, capsys):
    """With -j 2 the window holds 4 files; a 25-file batch must still finish, complete and in order."""
    src = tmp_path / "many"
    src.mkdir()
    for i in range(25):
        (src / f"p{i:02d}.png").write_bytes(fx["png_everything.png"])
    code, records = _run(src, tmp_path / "o", capsys, "-j", "2")
    assert code == 0
    assert len(list((tmp_path / "o").iterdir())) == 25
    assert [r["file"].rsplit("/", 1)[-1] for r in records] == [f"p{i:02d}.png" for i in range(25)]


def test_jobs_must_be_positive(tmp_path, fx):
    f = tmp_path / "a.png"
    f.write_bytes(fx["png_everything.png"])
    for bad in ("0", "-2"):
        with pytest.raises(SystemExit) as exc:
            main([str(f), "-j", bad])
        assert exc.value.code == 2


def test_stdin_is_never_parallel(monkeypatch, fx, capsysbinary):
    import io

    class Stdin:
        buffer = io.BytesIO(fx["png_everything.png"])

    monkeypatch.setattr(cli.sys, "stdin", Stdin)
    assert main(["-", "-o", "-", "-j", "4"]) == 0
    assert capsysbinary.readouterr().out.startswith(b"\x89PNG")


def test_self_test_variants_cover_each_fixture(fx):
    formats = ["PNG", "JPEG"]
    for name in fx:
        variants = selftest._variants(name, formats)
        labels = [label for label, _ in variants]
        assert labels[:4] == ["default", "scrub-lsb", "scrub-prnu", "icc=sanitize"]
        assert labels[4:6] == ["scrub-alpha", "scrub-alpha+lsb"]
        assert labels[-2:] == ["to png", "to jpeg"]
        assert ("jpeg-lossless" in labels) == (name.endswith(".jpg") and "mpo" not in name)
        assert all(isinstance(opts, Options) for _, opts in variants)


def test_self_test_job_runs_the_same_in_a_worker(fx):
    """_run_variant is what the worker processes execute; it must report problems, readers and the log line."""
    found, readers, log = selftest._run_variant(("jpeg_everything.jpg", fx["jpeg_everything.jpg"], Options(), "default", True))
    assert found == []
    assert readers
    assert len(log) == 1 and "jpeg_everything.jpg" in log[0] and "default" in log[0]
    # without -v nothing is logged
    assert selftest._run_variant(("jpeg_everything.jpg", fx["jpeg_everything.jpg"], Options(), "default", False))[2] == []


def test_self_test_reports_a_surviving_canary(monkeypatch, fx):
    """A broken stripper must still be caught when the check runs through the worker wrapper."""
    from imgstrip import fixtures

    real = selftest.strip_image

    def leaky(data, options):
        result = real(data, options)
        result.data = result.data + fixtures.CANARY
        return result

    monkeypatch.setattr(selftest, "strip_image", leaky)
    found, _, _ = selftest._run_variant(("png_everything.png", fx["png_everything.png"], Options(), "default", False))
    assert any("canary string survived" in p for p in found)
    assert all(p.startswith("png_everything.png: ") for p in found)


GIB = 1 << 30


@pytest.mark.parametrize(
    ("available", "per_job", "expected"),
    [
        (None, 4 * GIB, 8),  # the system does not say: one per core
        (64 * GIB, 1 * GIB, 8),  # plenty
        (16 * GIB, 2 * GIB, 5),  # 12 GiB usable, 2.2 GiB per worker
        (4 * GIB, 8 * GIB, 1),  # never fewer than one
    ],
)
def test_the_automatic_worker_count_fits_in_memory(monkeypatch, available, per_job, expected):
    monkeypatch.setattr(cli.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(cli, "_available_memory", lambda: available)
    assert _worker_count(None, 50, per_job) == expected
    assert _worker_count(6, 50, per_job) == 6  # an explicit -j is not second-guessed


def _png_header(width, height, depth=8) -> bytes:
    import struct
    import zlib

    body = struct.pack(">IIBBBBB", width, height, depth, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + body + struct.pack(">I", zlib.crc32(b"IHDR" + body))


def test_the_peak_is_estimated_from_the_largest_files_headers(tmp_path):
    small = tmp_path / "small.png"
    small.write_bytes(_png_header(100, 100))
    big = tmp_path / "big.png"
    big.write_bytes(_png_header(10_000, 5_000) + b"\0" * 1000)  # larger on disk, so it is looked at
    deep = tmp_path / "deep.png"
    deep.write_bytes(_png_header(1_000, 1_000, depth=16) + b"\0" * 500)
    tasks = [cli.Task(str(p), tmp_path) for p in (small, big, deep)]
    assert cli._peak_estimate(tasks, 150_000_000) == 50_000_000 * cli.PEAK_BYTES_PER_PIXEL
    assert cli._peak_estimate(tasks, 10_000_000) == 10_000_000 * cli.PEAK_BYTES_PER_PIXEL  # capped by --max-pixels
    assert cli._peak_estimate([tasks[2]], 150_000_000) == 1_000_000 * cli.PEAK_BYTES_PER_PIXEL_16BIT
    assert cli._peak_estimate([cli.Task(str(tmp_path / "gone.png"), tmp_path)], 150_000_000) is None


def test_the_default_pixel_limit_is_150_megapixels():
    from imgstrip.core import DEFAULT_MAX_PIXELS

    assert Options().max_pixels == DEFAULT_MAX_PIXELS == 150_000_000
    assert cli.build_parser().parse_args(["x.png"]).max_pixels == 150_000_000
