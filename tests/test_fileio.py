"""The output write path (review item 5): complete or nothing, and never overwrite an existing file in place."""

from __future__ import annotations

import errno
import os
import stat
import sys

import pytest

from imgstrip import fileio
from imgstrip.cli import main

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file semantics")


def leftovers(folder):
    return sorted(p.name for p in folder.iterdir() if p.name.startswith(".imgstrip-"))


def racing(monkeypatch, dest, content=b"someone else's file"):
    """Make another program create ``dest`` while imgstrip is writing its temporary file."""
    real = fileio._temporary

    def temporary(directory, data, mode):
        tmp = real(directory, data, mode)
        dest.write_bytes(content)
        return tmp

    monkeypatch.setattr(fileio, "_temporary", temporary)


def test_write_new_writes_the_file_and_nothing_else(tmp_path):
    dest = tmp_path / "sub" / "a.jpg"
    fileio.write_new(dest, b"pixels")
    assert dest.read_bytes() == b"pixels"
    assert stat.S_IMODE(dest.stat().st_mode) == fileio.default_mode()
    assert leftovers(dest.parent) == []


@POSIX
@pytest.mark.parametrize("kind", ["file", "directory", "dangling symlink", "symlink to a file"])
def test_write_new_never_replaces_what_is_there(tmp_path, kind):
    dest = tmp_path / "a.jpg"
    target = tmp_path / "target"
    if kind == "file":
        dest.write_bytes(b"old")
    elif kind == "directory":
        dest.mkdir()
    elif kind == "dangling symlink":
        dest.symlink_to(tmp_path / "nowhere")
    else:
        target.write_bytes(b"old")
        dest.symlink_to(target)
    with pytest.raises(FileExistsError):
        fileio.write_new(dest, b"new")
    if kind == "file":
        assert dest.read_bytes() == b"old"
    if kind == "symlink to a file":
        assert target.read_bytes() == b"old" and dest.is_symlink()
    assert leftovers(tmp_path) == []


@pytest.mark.parametrize("path", ["link", "noreplace rename", "placeholder"])
def test_a_file_that_appears_while_writing_is_not_replaced(tmp_path, monkeypatch, path):
    """The check-then-write race of 1.2.4's command line: the name is claimed atomically, on every path."""
    if path != "link":
        monkeypatch.setattr(os, "link", lambda *a: (_ for _ in ()).throw(PermissionError(errno.EPERM, "no links")))
    if path == "placeholder":
        monkeypatch.setattr(fileio, "_rename_noreplace", lambda src, dest: False)
    dest = tmp_path / "a.jpg"
    racing(monkeypatch, dest)
    with pytest.raises(FileExistsError):
        fileio.write_new(dest, b"cleaned")
    assert dest.read_bytes() == b"someone else's file"
    assert leftovers(tmp_path) == []


@pytest.mark.parametrize("path", ["link", "noreplace rename", "placeholder"])
def test_every_publishing_path_writes_the_file(tmp_path, monkeypatch, path):
    if path != "link":
        monkeypatch.setattr(os, "link", lambda *a: (_ for _ in ()).throw(OSError(errno.ENOTSUP, "no links")))
    if path == "placeholder":
        monkeypatch.setattr(fileio, "_rename_noreplace", lambda src, dest: False)
    dest = tmp_path / "a.jpg"
    fileio.write_new(dest, b"cleaned")
    assert dest.read_bytes() == b"cleaned"
    assert leftovers(tmp_path) == []


@pytest.mark.parametrize("call", [fileio.write_new, fileio.replace_atomic])
def test_a_failure_part_way_leaves_nothing_behind(tmp_path, monkeypatch, call):
    dest = tmp_path / "a.jpg"
    if call is fileio.replace_atomic:
        dest.write_bytes(b"old")

    def broken_disk(fd):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(os, "fsync", broken_disk)
    with pytest.raises(OSError):
        call(dest, b"cleaned")
    assert leftovers(tmp_path) == []
    assert (dest.read_bytes() == b"old") if call is fileio.replace_atomic else not dest.exists()


@POSIX
def test_replace_atomic_swaps_the_file_instead_of_writing_into_it(tmp_path):
    dest = tmp_path / "a.jpg"
    dest.write_bytes(b"old")
    other = tmp_path / "hard-link.jpg"
    os.link(dest, other)
    before = dest.stat().st_ino
    fileio.replace_atomic(dest, b"new", 0o640)
    assert dest.read_bytes() == b"new" and dest.stat().st_ino != before
    assert other.read_bytes() == b"old", "the old file was written into instead of replaced"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o640


@POSIX
def test_replace_atomic_replaces_a_symlink_instead_of_following_it(tmp_path):
    target = tmp_path / "target.jpg"
    target.write_bytes(b"somebody's photo")
    dest = tmp_path / "a.jpg"
    dest.symlink_to(target)
    fileio.replace_atomic(dest, b"new")
    assert not dest.is_symlink() and dest.read_bytes() == b"new"
    assert target.read_bytes() == b"somebody's photo"


# -- the command line -----------------------------------------------------------------------------------------


@pytest.fixture()
def jpg(tmp_path, fx):
    p = tmp_path / "photo.jpg"
    p.write_bytes(fx["jpeg_everything.jpg"])
    return p


def test_the_cli_does_not_replace_an_output_that_appears_meanwhile(jpg, monkeypatch, capsys):
    out = jpg.with_name("photo.clean.jpg")
    racing(monkeypatch, out)
    assert main([str(jpg)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert out.read_bytes() == b"someone else's file"
    assert leftovers(jpg.parent) == []


@POSIX
def test_the_cli_treats_a_dangling_symlink_as_taken(jpg, capsys):
    out = jpg.with_name("photo.clean.jpg")
    out.symlink_to(jpg.parent / "elsewhere.jpg")
    assert main([str(jpg)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert out.is_symlink() and not (jpg.parent / "elsewhere.jpg").exists()


@POSIX
def test_force_replaces_the_output_with_a_new_file(jpg):
    out = jpg.with_name("photo.clean.jpg")
    out.write_bytes(b"old output")
    keep = jpg.with_name("kept.jpg")
    os.link(out, keep)
    assert main([str(jpg), "--force"]) == 0
    assert keep.read_bytes() == b"old output" and out.read_bytes() != b"old output"
