"""The sanitised ICC profile is rebuilt from parsed numbers; no source bytes are copied into it."""

import struct

import pytest
from PIL import Image

from imgstrip import Options, fixtures, icc as iccmod, inspect_bytes, strip_image

CANARY = b"CANARY-ICC-PAYLOAD"


def _with_tags(profile: bytes, **replace: bytes) -> bytes:
    tags = iccmod._tag_table(profile)
    tags = {k: tags[k] for k in (b"rXYZ", b"gXYZ", b"bXYZ", b"rTRC", b"gTRC", b"bTRC")}
    for name, body in replace.items():
        tags[name.encode()] = body
    return iccmod._assemble(sorted(tags.items()), gray=False)


def _clean_profile() -> bytes:
    return fixtures.rgb_profile("test")


def test_trailing_and_reserved_bytes_do_not_survive():
    base = _clean_profile()
    tags = iccmod._tag_table(base)
    dirty = _with_tags(
        base,
        rXYZ=tags[b"rXYZ"] + CANARY * 4,
        gXYZ=tags[b"gXYZ"][:4] + b"RSVD" + tags[b"gXYZ"][8:],
        rTRC=tags[b"rTRC"] + CANARY * 4,
    )
    assert CANARY in dirty and b"RSVD" in dirty
    out = iccmod.build_sanitized(dirty)
    assert CANARY not in out and b"RSVD" not in out
    assert out == iccmod.build_sanitized(_with_tags(base))  # identical to the sanitised clean profile


def test_curve_table_low_bytes_do_not_survive():
    entries = b"".join(struct.pack(">H", (i * 257) & 0xFF00 | CANARY[i % len(CANARY)]) for i in range(256))
    table = b"curv\x00\x00\x00\x00" + struct.pack(">I", 256) + entries
    out = iccmod.build_sanitized(_with_tags(_clean_profile(), rTRC=table, gTRC=table, bTRC=table))
    assert CANARY not in out
    curve = iccmod._Curve(iccmod._tag_table(out)[b"rTRC"])
    values = (curve.params[0] * 65535).round().astype(int)
    assert curve.kind == "table"
    # every entry is exactly a 12-bit level, so the low-order payload cannot be carried through
    assert all(round(round(v * 4095 / 65535) * 65535 / 4095) == v for v in values)


def test_clean_profile_keeps_its_colours():
    out = iccmod.build_sanitized(_clean_profile())
    a, b = iccmod.MatrixShaper(_clean_profile()), iccmod.MatrixShaper(out)
    assert abs(a.matrix - b.matrix).max() < 1e-4


def test_oversized_curve_table_is_refused():
    table = b"curv\x00\x00\x00\x00" + struct.pack(">I", 20000) + b"\x00\x00" * 20000
    with pytest.raises(iccmod.ProfileError):
        iccmod.build_sanitized(_with_tags(_clean_profile(), rTRC=table))


@pytest.mark.parametrize("options", [Options(icc="sanitize"), Options(icc="sanitize", format="webp")])
def test_payload_in_profile_is_gone_from_cleaned_image(options):
    base = _clean_profile()
    tags = iccmod._tag_table(base)
    dirty = _with_tags(base, rTRC=tags[b"rTRC"] + CANARY * 4)
    buf = __import__("io").BytesIO()
    Image.new("RGB", (16, 16), (200, 30, 90)).save(buf, "PNG", icc_profile=dirty)
    result = strip_image(buf.getvalue(), options)
    assert CANARY not in result.data
    assert CANARY not in (inspect_bytes(result.data).info.get("icc") or b"")
