"""Fuzz targets (review item 8): each takes one input and either returns or raises an exception it is allowed to.

Anything else is a bug: an exception of another kind, an output that is not clean, or, under a sanitiser build, a
memory error in the C code behind Pillow (that is what the ``decode`` target is for).  ``fuzz/run.py`` drives the
targets with Atheris (coverage-guided, libFuzzer); ``tests/test_fuzz_targets.py`` runs every target on the seeds and
on random mutations of them, so they keep working without Atheris installed.
"""

from __future__ import annotations

import io
import json

from imgstrip import dct, fixtures, icc, jpeg, jumbf, sandbox
from imgstrip.core import Options, StripError, _pillow_guard, canonical_frames_here, open_image, strip_image
from imgstrip.inspect import inspect_bytes

MAX_PIXELS = 4_000_000  # keeps a run per input short; large pictures add no new code paths


def inspect(data: bytes) -> None:
    """The structural walk reads hostile input in this process, so it must never raise."""
    inspect_bytes(data)


def jpeg_rebuild(data: bytes) -> None:
    try:
        out, _dropped = jpeg.rebuild(data, 1, None, None)
    except (jpeg.JpegError, StripError):
        return
    assert out.startswith(b"\xff\xd8") and out.endswith(b"\xff\xd9")


def dct_round_trip(data: bytes) -> None:
    """What imgstrip's coefficient reader accepts, its canonical writer must write, and reading that back must give
    the same coefficients and the same bytes again (the canonical form is a fixed point)."""
    try:
        frame = dct.read(data, MAX_PIXELS)
    except (jpeg.JpegError, StripError):
        return
    try:
        body = dct.write(frame)
    except jpeg.JpegError as exc:
        assert "too large" in str(exc), exc
        return
    out = jpeg.assemble(body, 1, None, True, None)
    again = dct.read(out, MAX_PIXELS)
    assert dct.visible_equal(again, frame)
    assert dct.write(again) == body


def icc_sanitise(data: bytes) -> None:
    """A rebuilt profile is one the sandbox's check accepts, and rebuilding it again changes nothing."""
    try:
        clean = icc.build_sanitized(data)
    except (icc.ProfileError, StripError):
        return
    assert icc.is_sanitized(clean)
    assert icc.build_sanitized(clean) == clean


def jumbf_store(data: bytes) -> None:
    try:
        jumbf.parse_store(data)
    except jumbf.JumbfError:
        pass
    try:
        jumbf.cbor_decode(data)
    except jumbf.JumbfError:
        pass


def sandbox_answer(data: bytes) -> None:
    """The main process's reading of a worker's answer: a worker taken over by a hostile file controls these bytes,
    so anything but a refusal (the exceptions sandbox._call turns into a SandboxError) is a bug."""
    stream = sandbox._Relayed(sandbox._Reader(io.BytesIO(data)))
    try:
        kind, length = sandbox._inner_header(stream)
        if kind != b"H" or length > 1 << 20:
            return
        blobs = sandbox._Blobs(stream, 1 << 20)
        value = sandbox._unpack(json.loads(stream.read(length)), blobs)
    except sandbox._REFUSED:
        return
    if isinstance(value, sandbox._types()["Decoded"]):
        try:
            sandbox._accept_decoded(value, Options(), data)
        except StripError:
            pass


def decode(data: bytes) -> None:
    """Pillow and its C decoders on hostile bytes.  Python exceptions are expected (corrupt files raise all kinds);
    what this target looks for is a crash or a sanitiser report."""
    with _pillow_guard([], MAX_PIXELS):
        try:
            im = open_image(data)
            for i in range(min(getattr(im, "n_frames", 1) or 1, 4)):
                im.seek(i)
                im.load()
        except Exception:  # see the docstring
            return


def strip(data: bytes) -> None:
    """The whole engine, in this process: a verified clean file, or a StripError."""
    try:
        result = strip_image(data, Options(max_pixels=MAX_PIXELS, sandbox=False))
    except StripError:
        return
    report = inspect_bytes(result.data)
    assert report.error is None, report.error
    assert all(f.label.startswith("Pixels outside the visible crop") for f in report.findings), report.findings
    if fixtures.CANARY in result.data:
        # Only as pixel values the input itself shows (a damaged header can make trailing bytes part of the picture):
        # then the output holds nothing but those pixels.
        assert result.verified_exact
        want, got = canonical_frames_here(data, MAX_PIXELS), canonical_frames_here(result.data, MAX_PIXELS)
        assert len(want) == len(got) and all(
            a.shape == b.shape and (a == b).all() for a, b in zip(want, got, strict=True)
        )


TARGETS = {
    "inspect": inspect,
    "jpeg_rebuild": jpeg_rebuild,
    "dct_round_trip": dct_round_trip,
    "icc_sanitise": icc_sanitise,
    "jumbf_store": jumbf_store,
    "sandbox_answer": sandbox_answer,
    "decode": decode,
    "strip": strip,
}


def seeds(target: str) -> dict[str, bytes]:
    """A starting corpus for ``target``: the fixtures, and what each target's input format needs."""
    fx = fixtures.all_fixtures()
    if target in ("jpeg_rebuild", "dct_round_trip"):
        return {k: v for k, v in fx.items() if v[:2] == b"\xff\xd8"}
    if target == "icc_sanitise":
        from PIL import ImageCms

        out = {}
        for name in ("sRGB", "LAB", "XYZ"):
            try:
                out[name] = ImageCms.ImageCmsProfile(ImageCms.createProfile(name)).tobytes()
            except Exception:  # LittleCMS cannot serialise every built-in profile
                pass
        for name, data in fx.items():
            profile = _embedded_profile(data)
            if profile:
                out[name] = profile
        return out
    if target == "jumbf_store":
        out = {}
        for name, data in fx.items():
            for fmt in ("JPEG", "PNG", "WEBP", "GIF", "TIFF", "AVIF", "HEIF"):
                try:
                    store = jumbf.extract_store(data, fmt)
                except Exception:  # only the fixtures that carry a store give a seed
                    continue
                if store:
                    out[name] = store
                    break
        return out or {"empty": b""}
    if target == "sandbox_answer":
        return _answers()
    return dict(fx)


def _embedded_profile(data: bytes) -> bytes | None:
    try:
        return open_image(data).info.get("icc_profile") or None
    except Exception:
        return None


def _answers() -> dict[str, bytes]:
    """Well-formed worker answers, as the zygote relays them (data frames, then the exit status)."""
    import numpy as np

    from imgstrip.core import _Job

    out = {}
    fx = fixtures.all_fixtures()
    for name in ("png_everything.png", "jpeg_everything.jpg", "gif_animated.gif"):
        decoded = _Job(fx[name], Options(sandbox=False), None).decode()
        decoded.report.info = {k: v for k, v in decoded.report.info.items() if k in sandbox._REPORT_INFO}
        out[name] = _relayed(decoded)
    out["array"] = _relayed({"pixels": np.arange(24, dtype=np.uint8).reshape(2, 4, 3), "mode": "RGB"})
    return out


def _relayed(value) -> bytes:
    inner, outer = io.BytesIO(), io.BytesIO()
    blobs: list = []
    packed = sandbox._pack(value, blobs)
    w = sandbox._Writer(inner)
    w.frame(b"H", json.dumps(packed).encode())
    sandbox._send_blobs(blobs, w)
    w.frame(b"E")
    sandbox._Writer(outer).frame(b"D", inner.getvalue())
    sandbox._Writer(outer).frame(b"S", json.dumps({"exit": 0}).encode())
    return outer.getvalue()
