"""A strict reader for the C2PA manifest store (JUMBF boxes holding CBOR).

imgstrip never trusts the C2PA SDK to tell it that a credential holds nothing but what it
should: this module walks every byte of the manifest store itself.  A store that passes
``parse_store`` is a tree of superboxes with one fixed shape, whose leaves are definite-length
CBOR or JSON documents, and whose only free space is runs of zero bytes in the ``pad`` fields
the format reserves for later time-stamps.  Anything else (an unknown box, trailing bytes, an
indefinite-length item, a non-zero pad) raises :class:`JumbfError`.

It also exposes the pieces the signature covers (the claim and the COSE_Sign1 structure) so
the tests can check a credential independently of the SDK that wrote it.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field

MAX_STORE = 16 << 20
MAX_DEPTH = 8
MAX_ITEMS = 100_000

UUID_STORE = bytes.fromhex("6332706100110010800000aa00389b71")  # "c2pa"
UUID_MANIFEST = bytes.fromhex("63326d6100110010800000aa00389b71")  # "c2ma"
UUID_ASSERTIONS = bytes.fromhex("6332617300110010800000aa00389b71")  # "c2as"
UUID_CLAIM = bytes.fromhex("6332636c00110010800000aa00389b71")  # "c2cl"
UUID_SIGNATURE = bytes.fromhex("6332637300110010800000aa00389b71")  # "c2cs"
UUID_CBOR = bytes.fromhex("63626f7200110010800000aa00389b71")  # "cbor"
UUID_JSON = bytes.fromhex("6a736f6e00110010800000aa00389b71")  # "json"
UUID_CREDENTIALS = bytes.fromhex("6332766300110010800000aa00389b71")  # "c2vc"
UUID_DATABOXES = bytes.fromhex("6332646200110010800000aa00389b71")  # "c2db"

JPEG_XT_HEADER = 8  # 'JP', box instance number (2), packet sequence number (4)


class JumbfError(ValueError):
    """The manifest store is malformed or holds something that is not accounted for."""


# --------------------------------------------------------------------------
# CBOR (RFC 8949), definite lengths only
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Tagged:
    tag: int
    value: object


class _Undefined:
    def __repr__(self) -> str:
        return "undefined"


UNDEFINED = _Undefined()


def cbor_decode(data: bytes, *, max_depth: int = 16) -> object:
    """Decode exactly one CBOR item; trailing bytes are an error."""
    value, end = _cbor(data, 0, 0, max_depth, [0])
    if end != len(data):
        raise JumbfError(f"{len(data) - end} unexplained byte(s) after a CBOR item")
    return value


def _cbor(b: bytes, i: int, depth: int, max_depth: int, budget: list[int]):
    if depth > max_depth:
        raise JumbfError("CBOR nested too deeply")
    budget[0] += 1
    if budget[0] > MAX_ITEMS:
        raise JumbfError("CBOR document too large")
    if i >= len(b):
        raise JumbfError("truncated CBOR")
    head = b[i]
    major, info = head >> 5, head & 31
    i += 1
    if info < 24:
        arg = info
    elif info in (24, 25, 26, 27):
        width = 1 << (info - 24)
        if i + width > len(b):
            raise JumbfError("truncated CBOR")
        arg = int.from_bytes(b[i : i + width], "big")
        i += width
    else:
        raise JumbfError("indefinite-length or reserved CBOR item")
    if major == 0:
        return arg, i
    if major == 1:
        return -1 - arg, i
    if major in (2, 3):
        if i + arg > len(b):
            raise JumbfError("truncated CBOR string")
        raw = b[i : i + arg]
        i += arg
        if major == 2:
            return bytes(raw), i
        try:
            return raw.decode("utf-8"), i
        except UnicodeDecodeError:
            raise JumbfError("CBOR text is not UTF-8") from None
    if major == 4:
        if arg > MAX_ITEMS:
            raise JumbfError("CBOR array too large")
        items = []
        for _ in range(arg):
            v, i = _cbor(b, i, depth + 1, max_depth, budget)
            items.append(v)
        return items, i
    if major == 5:
        if arg > MAX_ITEMS:
            raise JumbfError("CBOR map too large")
        out: dict = {}
        for _ in range(arg):
            k, i = _cbor(b, i, depth + 1, max_depth, budget)
            try:
                hash(k)  # a tagged array is not a usable key either
            except TypeError:
                raise JumbfError("unsupported CBOR map key") from None
            if k in out:
                raise JumbfError("duplicate CBOR map key")
            v, i = _cbor(b, i, depth + 1, max_depth, budget)
            out[k] = v
        return out, i
    if major == 6:
        v, i = _cbor(b, i, depth + 1, max_depth, budget)
        return Tagged(arg, v), i
    # major type 7
    if info == 20:
        return False, i
    if info == 21:
        return True, i
    if info == 22:
        return None, i
    if info == 23:
        return UNDEFINED, i
    if info in (25, 26, 27):
        width = 1 << (info - 24)
        fmt = {2: ">e", 4: ">f", 8: ">d"}[width]
        return struct.unpack(fmt, b[i - width : i])[0], i
    raise JumbfError("unsupported CBOR simple value")


def cbor_encode_bstr(value: bytes) -> bytes:
    n = len(value)
    if n < 24:
        head = bytes([0x40 | n])
    elif n < 256:
        head = bytes([0x58, n])
    elif n < 65536:
        head = b"\x59" + struct.pack(">H", n)
    else:
        head = b"\x5a" + struct.pack(">I", n)
    return head + value


# --------------------------------------------------------------------------
# JUMBF boxes
# --------------------------------------------------------------------------


@dataclass
class Box:
    type: bytes
    uuid: bytes | None = None  # description box of a superbox
    label: str | None = None
    toggles: int = 0
    children: list[Box] = field(default_factory=list)
    body: bytes = b""  # content of a leaf box
    start: int = 0
    end: int = 0


def _boxes(data: bytes, start: int, end: int, depth: int, count: list[int]) -> list[Box]:
    if depth > MAX_DEPTH:
        raise JumbfError("boxes nested too deeply")
    out: list[Box] = []
    pos = start
    while pos < end:
        count[0] += 1
        if count[0] > MAX_ITEMS:
            raise JumbfError("too many boxes")
        if pos + 8 > end:
            raise JumbfError("truncated box header")
        size, typ = struct.unpack_from(">I4s", data, pos)
        hdr = 8
        if size == 1:
            if pos + 16 > end:
                raise JumbfError("truncated box header")
            (size,) = struct.unpack_from(">Q", data, pos + 8)
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            raise JumbfError(f"box '{typ.decode('latin-1')}' overruns its parent")
        body_start, body_end = pos + hdr, pos + size
        box = Box(typ, start=pos, end=body_end)
        if typ == b"jumb":
            kids = _boxes(data, body_start, body_end, depth + 1, count)
            if not kids or kids[0].type != b"jumd":
                raise JumbfError("superbox without a description box")
            desc = kids[0]
            box.uuid, box.label, box.toggles = desc.uuid, desc.label, desc.toggles
            box.children = kids[1:]
        elif typ == b"jumd":
            body = data[body_start:body_end]
            if len(body) < 17:
                raise JumbfError("short description box")
            box.uuid, box.toggles = bytes(body[:16]), body[16]
            rest = body[17:]
            label = None
            if box.toggles & 0x02:  # requestable, label present, null-terminated
                nul = rest.find(b"\x00")
                if nul < 0:
                    raise JumbfError("unterminated label")
                try:
                    label = rest[:nul].decode("utf-8")
                except UnicodeDecodeError:
                    raise JumbfError("label is not UTF-8") from None
                rest = rest[nul + 1 :]
            # Optional ID (4 bytes, toggle 0x04) and a 32-byte signature (toggle 0x08) are
            # fixed-size fields; a private box (toggle 0x10) holds the random salt that C2PA
            # mixes into assertion hashes ('c2sh').  Anything else is unexplained.
            if box.toggles & 0x04:
                if len(rest) < 4:
                    raise JumbfError("short description box")
                rest = rest[4:]
            if box.toggles & 0x08:
                if len(rest) < 32:
                    raise JumbfError("short description box")
                rest = rest[32:]
            if box.toggles & 0x10:
                private = _boxes(rest, 0, len(rest), depth + 1, count)
                if len(private) != 1 or private[0].type != b"c2sh" or not 16 <= len(private[0].body) <= 64:
                    raise JumbfError("unexpected private box in a description box")
                rest = b""
            if rest:
                raise JumbfError(f"{len(rest)} unexplained byte(s) in a description box")
            box.label = label
        else:
            box.body = bytes(data[body_start:body_end])
        out.append(box)
        pos = body_end
    return out


def parse_boxes(data: bytes) -> list[Box]:
    if len(data) > MAX_STORE:
        raise JumbfError("manifest store is too large")
    return _boxes(data, 0, len(data), 0, [0])


# --------------------------------------------------------------------------
# The manifest store
# --------------------------------------------------------------------------


@dataclass
class Manifest:
    label: str
    assertions: dict[str, object]  # assertion label -> decoded content
    claim: dict
    claim_bytes: bytes
    signature: Tagged  # COSE_Sign1
    signature_bytes: bytes


@dataclass
class Store:
    manifests: list[Manifest]


def _leaf(box: Box, what: str) -> object:
    """Decode the single content box of a leaf superbox (CBOR or JSON)."""
    if len(box.children) != 1 or box.children[0].children:
        raise JumbfError(f"{what} must hold exactly one content box")
    content = box.children[0]
    if box.uuid == UUID_JSON:
        if content.type != b"json":
            raise JumbfError(f"{what} is not a JSON box")
        try:
            return json.loads(content.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise JumbfError(f"{what} is not valid JSON") from None
    if content.type != b"cbor":
        raise JumbfError(f"{what} is not a CBOR box")
    return cbor_decode(content.body)


def _require_zero_pads(value: object, where: str) -> None:
    """Free space is allowed only as zero-filled ``pad`` byte strings."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k in ("pad", "pad1", "pad2") or (isinstance(k, str) and k.startswith("pad")):
                if not isinstance(v, (bytes, bytearray)) or any(v):
                    raise JumbfError(f"{where}: padding '{k}' is not all zero bytes")
            else:
                _require_zero_pads(v, where)
    elif isinstance(value, list):
        for v in value:
            _require_zero_pads(v, where)
    elif isinstance(value, Tagged):
        _require_zero_pads(value.value, where)


def parse_store(jumbf: bytes) -> Store:
    """Parse a complete manifest store and account for every byte of it."""
    top = parse_boxes(jumbf)
    if len(top) != 1 or top[0].type != b"jumb" or top[0].uuid != UUID_STORE:
        raise JumbfError("not a C2PA manifest store")
    manifests: list[Manifest] = []
    for mbox in top[0].children:
        if mbox.type != b"jumb" or mbox.uuid != UUID_MANIFEST:
            raise JumbfError("unexpected box in the manifest store")
        assertions: dict[str, object] = {}
        claim = signature = None
        claim_bytes = signature_bytes = b""
        for part in mbox.children:
            if part.type != b"jumb":
                raise JumbfError("unexpected box in a manifest")
            if part.uuid == UUID_ASSERTIONS:
                for a in part.children:
                    if a.type != b"jumb" or a.uuid not in (UUID_CBOR, UUID_JSON) or not a.label:
                        raise JumbfError("unexpected box among the assertions")
                    if a.label in assertions:
                        raise JumbfError(f"duplicate assertion {a.label}")
                    assertions[a.label] = _leaf(a, f"assertion {a.label}")
            elif part.uuid == UUID_CLAIM:
                if claim is not None:
                    raise JumbfError("more than one claim")
                if len(part.children) != 1 or part.children[0].type != b"cbor":
                    raise JumbfError("the claim is not a single CBOR box")
                claim_bytes = part.children[0].body
                claim = cbor_decode(claim_bytes)
                if not isinstance(claim, dict):
                    raise JumbfError("the claim is not a map")
            elif part.uuid == UUID_SIGNATURE:
                if signature is not None:
                    raise JumbfError("more than one signature")
                if len(part.children) != 1 or part.children[0].type != b"cbor":
                    raise JumbfError("the signature is not a single CBOR box")
                signature_bytes = part.children[0].body
                signature = cbor_decode(signature_bytes)
            else:
                raise JumbfError("unexpected superbox in a manifest")
        if claim is None or signature is None:
            raise JumbfError("manifest without a claim or a signature")
        if not (isinstance(signature, Tagged) and signature.tag == 18 and isinstance(signature.value, list)):
            raise JumbfError("the signature is not a COSE_Sign1 structure")
        if len(signature.value) != 4:
            raise JumbfError("malformed COSE_Sign1 structure")
        _require_zero_pads(claim, "claim")
        _require_zero_pads(signature.value[1], "signature")
        for label, content in assertions.items():
            _require_zero_pads(content, label)
        manifests.append(Manifest(mbox.label or "", assertions, claim, claim_bytes, signature, signature_bytes))
    if not manifests:
        raise JumbfError("the manifest store holds no manifest")
    return Store(manifests)


# --------------------------------------------------------------------------
# Finding the store inside each container
# --------------------------------------------------------------------------


def extract_jpeg(data: bytes) -> bytes:
    """Reassemble the manifest store from the JPEG XT APP11 segments."""
    pos, n = 2, len(data)
    parts: dict[tuple[int, int], bytes] = {}
    while pos + 4 <= n and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xDA or marker == 0xD9:
            break
        (length,) = struct.unpack_from(">H", data, pos + 2)
        payload = data[pos + 4 : pos + 2 + length]
        if marker == 0xEB and payload[:2] == b"JP":
            en, z = struct.unpack_from(">HI", payload, 2)
            parts[(en, z)] = payload[JPEG_XT_HEADER:]
        pos += 2 + length
    if not parts:
        raise JumbfError("no manifest store in the JPEG file")
    instances = {en for en, _ in parts}
    if len(instances) != 1:
        raise JumbfError("more than one JUMBF box instance")
    # Each later packet repeats the 8-byte box header (size + type) of the first.
    ordered = [parts[k] for k in sorted(parts)]
    if [z for _, z in sorted(parts)] != list(range(1, len(parts) + 1)):
        raise JumbfError("JUMBF packets are missing or out of order")
    out = ordered[0]
    for chunk in ordered[1:]:
        out += chunk[8:]
    return out


def _one(parts: list[bytes], where: str) -> bytes:
    if not parts:
        raise JumbfError(f"no manifest store in the {where}")
    if len(parts) > 1:
        raise JumbfError(f"more than one manifest store in the {where}")
    return parts[0]


def extract_png(data: bytes) -> bytes:
    parts, pos = [], 8
    while pos + 12 <= len(data):
        length, ctype = struct.unpack_from(">I4s", data, pos)
        if ctype == b"caBX":
            parts.append(data[pos + 8 : pos + 8 + length])
        pos += 12 + length
    return _one(parts, "PNG file")


def extract_webp(data: bytes) -> bytes:
    parts, pos = [], 12
    end = min(len(data), 8 + struct.unpack_from("<I", data, 4)[0])
    while pos + 8 <= end:
        fourcc, size = struct.unpack_from("<4sI", data, pos)
        if fourcc == b"C2PA":
            parts.append(data[pos + 8 : pos + 8 + size])
        pos += 8 + size + (size & 1)
    return _one(parts, "WebP file")


def extract_gif(data: bytes) -> bytes:
    packed = data[10]
    pos = 13 + ((3 << ((packed & 7) + 1)) if packed & 0x80 else 0)
    parts = []

    def subblocks(p: int) -> tuple[bytes, int]:
        out = bytearray()
        while True:
            n = data[p]
            p += 1
            if n == 0:
                return bytes(out), p
            out += data[p : p + n]
            p += n

    while pos < len(data) and data[pos] != 0x3B:
        if data[pos] == 0x2C:
            flags = data[pos + 9]
            pos += 10 + ((3 << ((flags & 7) + 1)) if flags & 0x80 else 0) + 1
            _, pos = subblocks(pos)
        elif data[pos] == 0x21:
            label = data[pos + 1]
            body, nxt = subblocks(pos + 2)
            if label == 0xFF and body[:11] == b"C2PA_GIF\x01\x00\x00":
                parts.append(body[11:])
            pos = nxt
        else:
            raise JumbfError("unexpected GIF block")
    return _one(parts, "GIF file")


def extract_tiff(data: bytes) -> bytes:
    from .inspect import _Tiff

    t = _Tiff(data)
    if t.big:
        raise JumbfError("BigTIFF is not supported for credentials")
    (off,) = struct.unpack_from(t.e + "I", data, 4)
    parts = []
    while off:
        entries, off = t.read_ifd(off)
        parts += [raw for tag, _typ, _cnt, _size, raw in entries if tag == 52545]
    return _one(parts, "TIFF file")


def extract_bmff(data: bytes) -> bytes:
    from .inspect import C2PA_BMFF_UUID

    parts, pos = [], 0
    while pos + 8 <= len(data):
        size, typ = struct.unpack_from(">I4s", data, pos)
        hdr = 8
        if size == 1:
            (size,) = struct.unpack_from(">Q", data, pos + 8)
            hdr = 16
        elif size == 0:
            size = len(data) - pos
        if size < hdr or pos + size > len(data):
            raise JumbfError("malformed box")
        if typ == b"uuid" and data[pos + hdr : pos + hdr + 16] == C2PA_BMFF_UUID:
            body = data[pos + hdr + 16 : pos + size]
            # FullBox header, a null-terminated purpose and a Merkle offset (always 0 here).
            if body[:4] != b"\x00\x00\x00\x00":
                raise JumbfError("unsupported C2PA box version")
            nul = body.find(b"\x00", 4)
            if nul < 0 or body[4:nul] != b"manifest" or body[nul + 1 : nul + 9] != b"\x00" * 8:
                raise JumbfError("unsupported C2PA box layout")
            parts.append(body[nul + 9 :])
        pos += size
    return _one(parts, "file")


_EXTRACT = {
    "JPEG": extract_jpeg,
    "PNG": extract_png,
    "WEBP": extract_webp,
    "GIF": extract_gif,
    "TIFF": extract_tiff,
    "AVIF": extract_bmff,
    "HEIF": extract_bmff,
}


def extract_store(data: bytes, fmt: str) -> bytes:
    """The raw manifest store embedded in ``data`` (a container format name as in ``inspect``)."""
    try:
        return _EXTRACT[fmt](data)
    except KeyError:
        raise JumbfError(f"{fmt} files cannot carry Content Credentials") from None
    except (struct.error, IndexError) as exc:
        raise JumbfError(f"malformed {fmt} container ({exc})") from None
