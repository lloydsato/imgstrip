"""A signed processing record: what was done to one file, written down next to the output and never inside it.

After a file is cleaned, imgstrip knows what it removed and which checks passed.  This module writes that down in a
small JSON file so that a later reader can confirm what was done and that the account was not edited afterwards:

* SHA-256 of the original and of the cleaned file, with their sizes and formats;
* the versions of imgstrip and the libraries it ran on;
* the options used, what was removed, the actions taken, the warnings and the checks that passed;
* the time (UTC), and an optional case reference and operator name typed by whoever ran the tool.

The record is signed with an Ed25519 key that belongs to the person or agency running imgstrip, so any edit made
afterwards is detectable.  It is a different mechanism from the C2PA Content Credentials in :mod:`credentials`: that
note travels inside the image for the public, this one stays with the evidence.  **The record names the original
file, so it is as sensitive as the original and must never be published with the cleaned image.**

Format (``imgstrip-record`` version 1): one JSON object ``{"record": {...}, "signature": {...} | null}``.  The signature
is Ed25519 over the *canonical form* of ``record``: JSON with sorted keys, no whitespace, ``ensure_ascii`` escaping,
UTF-8 encoded; the body holds only strings, integers, booleans, null, lists and objects (no floating-point numbers), so
any JSON library can rebuild the same bytes.  ``signature.key_id`` is the SHA-256 of the raw 32-byte public key.

Signing needs the optional ``cryptography`` library (``pip install 'imgstrip[record]'``).  Without a key a record can
still be written, but it is labelled unsigned: it documents the work and cannot show that it was left untouched.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
import os
import platform
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from . import __version__
from .core import StripError

FORMAT = "imgstrip-record"
VERSION = 1
SUFFIX = ".imgstrip-record.json"
MAX_FIELD = 200  # characters of a case reference or an operator name
MAX_RECORD_BYTES = 4 << 20  # a record is a few KB; anything larger is not one
INSTALL_HINT = "signing records needs the optional 'cryptography' library: pip install 'imgstrip[record]'"

UNSIGNED = "unsigned"
SIGNED = "signed"  # the signature is consistent; the key was not compared with one the reader trusts
TRUSTED = "trusted"  # the signature is valid and the key is the one the reader trusts
INVALID = "invalid"

# Controls, bidirectional overrides and zero-width characters: text that reads differently from its bytes.  Zero-width
# non-joiner and joiner (U+200C, U+200D) stay, because some scripts cannot be written without them.
_INVISIBLE = re.compile("[\x00-\x1f\x7f-\x9f\u061c\u180e\u200b\u200e\u200f\u2028\u2029\u202a-\u202e\u2060\u2066-\u2069\ufeff]")


class RecordError(StripError):
    """A problem with a record or its key."""


class PassphraseRequired(RecordError):
    """The private key is protected and no (or the wrong) passphrase was given."""


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------


def _crypto():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError:
        raise RecordError(INSTALL_HINT) from None
    return serialization, ed25519


def available() -> bool:
    try:
        _crypto()
    except RecordError:
        return False
    return True


def key_id_of(raw_public: bytes) -> str:
    return hashlib.sha256(raw_public).hexdigest()


def short_id(key_id: str) -> str:
    """The key id as groups of four hex digits, 16 digits long: enough to read aloud and compare."""
    head = key_id[:16]
    return " ".join(head[i : i + 4] for i in range(0, len(head), 4))


def _raw_public(public_key) -> bytes:
    serialization, _ = _crypto()
    return public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


@dataclass
class Signer:
    """A loaded private key.  It is only ever held in memory by this object."""

    private_key: Any = field(repr=False)
    public_raw: bytes = b""

    @property
    def key_id(self) -> str:
        return key_id_of(self.public_raw)

    def public_pem(self) -> bytes:
        serialization, _ = _crypto()
        return self.private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    def sign(self, message: bytes) -> bytes:
        return self.private_key.sign(message)


# A key protected by a passphrase is wrapped with scrypt and ChaCha20-Poly1305.  The usual PKCS#8 file stretches the
# passphrase with PBKDF2 at 2,048 rounds, which the library offers no way to raise.  Keys made by older versions
# (encrypted PKCS#8) are still read.
_WRAP_LABEL = "IMGSTRIP ENCRYPTED PRIVATE KEY"
_SCRYPT = {"n": 1 << 17, "r": 8, "p": 1}  # 128 MiB, a few tenths of a second
_SCRYPT_LIMIT = {"n": 1 << 18, "r": 8, "p": 4}  # a key file may not ask for more than this when it is opened
_SCRYPT_MEMORY = 512 << 20


def _wrap_key(private_der: bytes, passphrase: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    salt, nonce = os.urandom(16), os.urandom(12)
    head = {"format": "imgstrip-key", "version": 1, "kdf": "scrypt", **_SCRYPT, "salt": base64.b64encode(salt).decode("ascii"),
            "cipher": "chacha20poly1305", "nonce": base64.b64encode(nonce).decode("ascii")}
    aad = canonical(head)
    key = hashlib.scrypt(passphrase, salt=salt, dklen=32, maxmem=_SCRYPT_MEMORY, **_SCRYPT)
    head["ciphertext"] = base64.b64encode(ChaCha20Poly1305(key).encrypt(nonce, private_der, aad)).decode("ascii")
    body = base64.b64encode(json.dumps(head, sort_keys=True).encode("ascii")).decode("ascii")
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return ("\n".join([f"-----BEGIN {_WRAP_LABEL}-----", *lines, f"-----END {_WRAP_LABEL}-----"]) + "\n").encode("ascii")


def _unwrap_key(pem: bytes, passphrase: bytes | None) -> bytes:
    """The private key's DER from a wrapped key file."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    match = re.search(rb"-----BEGIN " + _WRAP_LABEL.encode() + rb"-----(.+?)-----END", pem, re.S)
    try:
        head = json.loads(base64.b64decode(b"".join(match.group(1).split()), validate=True))
        params = {k: head[k] for k in ("n", "r", "p")}
        if head["format"] != "imgstrip-key" or head["version"] != 1 or head["kdf"] != "scrypt" or head["cipher"] != "chacha20poly1305":
            raise ValueError
        if any(not isinstance(v, int) or v < 1 or v > _SCRYPT_LIMIT[k] for k, v in params.items()) or params["n"] & (params["n"] - 1):
            raise ValueError
        salt, nonce = base64.b64decode(head["salt"], validate=True), base64.b64decode(head["nonce"], validate=True)
        sealed = base64.b64decode(head.pop("ciphertext"), validate=True)
    except (AttributeError, KeyError, TypeError, ValueError, binascii.Error, RecursionError):
        raise RecordError("that is not a readable imgstrip private key") from None
    if not passphrase:
        raise PassphraseRequired("the signing key is protected by a passphrase")
    key = hashlib.scrypt(passphrase, salt=salt, dklen=32, maxmem=_SCRYPT_MEMORY, **params)
    try:
        return ChaCha20Poly1305(key).decrypt(nonce, sealed, canonical(head))
    except InvalidTag:
        raise PassphraseRequired("wrong passphrase for the signing key") from None


def generate_key(passphrase: bytes | None = None) -> tuple[bytes, bytes, str]:
    """A new signing key: ``(private PEM, public PEM, key id)``.  The private PEM is encrypted when a passphrase is given."""
    serialization, ed25519 = _crypto()
    private = ed25519.Ed25519PrivateKey.generate()
    if passphrase:
        der = private.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        private_pem = _wrap_key(der, passphrase)
    else:
        private_pem = private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    public = private.public_key()
    public_pem = public.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return private_pem, public_pem, key_id_of(_raw_public(public))


def load_signer(pem: bytes, passphrase: bytes | None = None) -> Signer:
    serialization, ed25519 = _crypto()
    if b"PRIVATE KEY" not in pem:
        raise RecordError("that file does not hold a private key (expected a PEM file with an Ed25519 private key)")
    if _WRAP_LABEL.encode() in pem:
        der = _unwrap_key(pem, passphrase)  # raises RecordError or PassphraseRequired
        try:
            key = serialization.load_der_private_key(der, password=None)
        except Exception:
            raise RecordError("that is not a readable imgstrip private key") from None
        if not isinstance(key, ed25519.Ed25519PrivateKey):
            raise RecordError("the key must be an Ed25519 key; make one with --new-record-key")
        return Signer(key, _raw_public(key.public_key()))
    try:
        try:
            key = serialization.load_pem_private_key(pem, password=passphrase or None)
        except TypeError:
            if not passphrase:
                raise
            # A passphrase is set (say in the environment) but this key has none: that is not a problem.
            key = serialization.load_pem_private_key(pem, password=None)
    except TypeError:
        raise PassphraseRequired("the signing key is protected by a passphrase") from None
    except ValueError as exc:
        if "assword" in str(exc) or "ecrypt" in str(exc):
            raise PassphraseRequired("wrong passphrase for the signing key") from None
        raise RecordError("that is not a readable PEM private key") from None
    except Exception:
        raise RecordError("that is not a readable PEM private key") from None
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise RecordError("the key must be an Ed25519 key; make one with --new-record-key")
    return Signer(key, _raw_public(key.public_key()))


def load_public(pem: bytes) -> bytes:
    """The raw 32-byte public key from a PEM public key (or from the PEM of a private key)."""
    serialization, ed25519 = _crypto()
    try:
        if b"PRIVATE KEY" in pem:
            raise RecordError("that is a PRIVATE key; give the public key (the .pub file) to people who check records")
        key = serialization.load_pem_public_key(pem)
    except RecordError:
        raise
    except Exception:
        raise RecordError("that is not a readable PEM public key") from None
    if not isinstance(key, ed25519.Ed25519PublicKey):
        raise RecordError("the public key must be an Ed25519 key")
    return _raw_public(key)


# --------------------------------------------------------------------------
# Building a record
# --------------------------------------------------------------------------


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean_text(value: str | None, what: str) -> str | None:
    """A case reference or operator name, trimmed.  Empty means none; control characters are refused."""
    if value is None:
        return None
    text = " ".join(str(value).split())  # collapses runs of whitespace, newlines and tabs
    if not text:
        return None
    if len(text) > MAX_FIELD:
        raise RecordError(f"the {what} is longer than {MAX_FIELD} characters")
    if _INVISIBLE.search(text):
        raise RecordError(f"the {what} contains control characters, text-direction marks or zero-width characters")
    return text


def display_text(value: Any) -> Any:
    """Text from a record file as it is shown to a reader: controls, text-direction marks and zero-width characters
    are replaced by U+FFFD.  A record made elsewhere can hold them, and they must not reach a terminal or a page."""
    return _INVISIBLE.sub("\ufffd", value) if isinstance(value, str) else value


def _plain(value: Any) -> Any:
    """JSON-safe and float-free: numbers that are not integers are written as text so every reader rebuilds the same bytes."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    return str(value)


def _versions() -> dict[str, Any]:
    out: dict[str, Any] = {"imgstrip": __version__, "python": platform.python_version()}
    for name, module in (("pillow", "PIL"), ("numpy", "numpy"), ("cryptography", "cryptography")):
        try:
            mod = __import__(module)
            out[name] = str(getattr(mod, "__version__", "unknown"))
        except Exception:
            out[name] = None
    out["platform"] = f"{platform.system()} {platform.machine()}".strip()
    return out


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def build(
    *,
    input_name: str,
    input_data_sha256: str,
    input_bytes: int,
    input_format: str,
    output_name: str,
    output_data: bytes,
    output_format: str,
    options: Any,
    result: dict,
    case: str | None = None,
    operator: str | None = None,
    when: str | None = None,
) -> dict:
    """The body of a record.  ``result`` holds the facts imgstrip reported for this file (see :func:`result_facts`)."""
    return {
        "format": FORMAT,
        "version": VERSION,
        "created": when or _now(),
        "case": clean_text(case, "case reference"),
        "operator": clean_text(operator, "operator name"),
        "tool": _versions(),
        "input": {"name": input_name, "sha256": input_data_sha256, "bytes": input_bytes, "format": input_format},
        "output": {"name": output_name, "sha256": sha256(output_data), "bytes": len(output_data), "format": output_format},
        "options": _plain(asdict(options) if hasattr(options, "__dataclass_fields__") else dict(options or {})),
        "result": _plain(result),
    }


# The engine's messages name the ICC profile they removed or converted ("removed ICC profile 'Display P3 of Jane's
# laptop' ...").  The name is text from the original, so the record says only that a profile was.
_PROFILE_NAME = re.compile(r"ICC profile '.*?'(?= \(| without| with a| removed|$)", re.S)


def _anonymous(line: Any) -> Any:
    return _PROFILE_NAME.sub("ICC profile", line) if isinstance(line, str) else line


def result_facts(payload: dict) -> dict:
    """What a cleaning reported (the CLI payload or the web payload), reduced to what the record keeps."""
    steg = payload.get("steganography") or {}
    facts = {
        "size": payload.get("size"),
        "frames": payload.get("frames"),
        "pixels_exact": payload.get("pixels_exact"),
        "verified_by": payload.get("verified_by") or [],
        "removed": [
            # Category and label only.  The detail line holds what was found (a location, a camera, a name), and a
            # record is kept and passed on; whoever holds the original can see the values in it.
            {"category": f.get("category"), "label": f.get("label")}
            for f in payload.get("removed") or []
        ],
        "actions": [_anonymous(line) for line in payload.get("actions") or []],
        "warnings": [_anonymous(line) for line in payload.get("warnings") or []],
        "kept": payload.get("kept") or {},
        "steganography": steg.get("state"),
    }
    for key in ("dct", "alpha"):
        if payload.get(key):
            facts[key] = payload[key].get("headline") or True
    if payload.get("credentials"):
        facts["content_credentials"] = bool(payload["credentials"].get("signed"))
    return facts


def canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def seal(body: dict, signer: Signer | None) -> bytes:
    """The record file's bytes: the body, and its signature when a key is given."""
    signature = None
    if signer is not None:
        signature = {
            "algorithm": "ed25519",
            "key_id": signer.key_id,
            "public_key": base64.b64encode(signer.public_raw).decode("ascii"),
            "value": base64.b64encode(signer.sign(canonical(body))).decode("ascii"),
        }
    doc = {"record": body, "signature": signature}
    return (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def file_name(output_name: str, output_sha256: str) -> str:
    """Where a record is kept: named after the cleaned file and a stretch of its hash, so two runs never clash."""
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in output_name).strip(".") or "output"
    if safe.startswith("-"):  # a name that looks like an option is a trap for the next command typed
        safe = "_" + safe
    return f"{safe[:120]}.{output_sha256[:12]}{SUFFIX}"


# --------------------------------------------------------------------------
# Checking a record
# --------------------------------------------------------------------------


@dataclass
class Verdict:
    status: str  # UNSIGNED | SIGNED | TRUSTED | INVALID
    problems: list[str] = field(default_factory=list)
    body: dict | None = None
    key_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (SIGNED, TRUSTED)

    def as_dict(self) -> dict:
        body = self.body or {}
        return {
            "status": self.status,
            "ok": self.ok,
            "key_id": self.key_id,
            "problems": list(self.problems),
            "created": body.get("created"),
            "case": display_text(body.get("case")),
            "operator": display_text(body.get("operator")),
            "input": _shown(body.get("input")),
            "output": _shown(body.get("output")),
        }


def _shown(entry: Any) -> Any:
    return {k: display_text(v) for k, v in entry.items()} if isinstance(entry, dict) else None


def _bad(*problems: str) -> Verdict:
    return Verdict(INVALID, list(problems))


class _Repeated(ValueError):
    pass


def _no_repeats(pairs: list[tuple[str, Any]]) -> dict:
    """``json.loads`` keeps the last of a repeated key, while a person or another program may read the first."""
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise _Repeated(key)
        out[key] = value
    return out


_SIGNATURE_KEYS = {"algorithm", "key_id", "public_key", "value"}
# Ed25519 points of small order: a public key that is one of them verifies a signature chosen without any secret.
_SMALL_ORDER = {
    bytes.fromhex(h)
    for h in (
        "0100000000000000000000000000000000000000000000000000000000000000",
        "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
        "0000000000000000000000000000000000000000000000000000000000000000",
        "0000000000000000000000000000000000000000000000000000000000000080",
        "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
        "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa",
        "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
        "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc85",
    )
}


def verify(document: bytes, trusted_public: bytes | None = None) -> Verdict:
    """Check a record file.  ``trusted_public`` is the raw public key the reader already trusts (from :func:`load_public`)."""
    if len(document) > MAX_RECORD_BYTES:
        return _bad("this file is too large to be an imgstrip record")
    try:
        doc = json.loads(document, object_pairs_hook=_no_repeats)
        body, signature = doc["record"], doc["signature"]
        if not isinstance(body, dict) or body.get("format") != FORMAT:
            raise ValueError
    except _Repeated as exc:
        return _bad(
            f"the file repeats the key {str(exc)!r}, so different readers would see different values; "
            "an imgstrip record never does that"
        )
    except (ValueError, KeyError, TypeError, RecursionError, UnicodeDecodeError):
        return _bad("this is not an imgstrip record")
    if set(doc) != {"record", "signature"}:
        return _bad("the file holds fields outside the signed record, which nothing protects")
    if body.get("version") != VERSION:
        return _bad(f"this record is version {body.get('version')!r}; this imgstrip reads version {VERSION}")
    if signature is None:
        return Verdict(UNSIGNED, ["the record is not signed, so nothing shows that it was left unedited"], body)
    if not isinstance(signature, dict) or set(signature) != _SIGNATURE_KEYS:
        return _bad("the signature block is malformed")
    try:
        if signature["algorithm"] != "ed25519":
            return _bad(f"unknown signature algorithm {signature['algorithm']!r}")
        public_raw = base64.b64decode(signature["public_key"], validate=True)
        value = base64.b64decode(signature["value"], validate=True)
        claimed = signature["key_id"]
    except (KeyError, TypeError, ValueError, binascii.Error):
        return _bad("the signature block is malformed")
    if public_raw in _SMALL_ORDER:
        return Verdict(INVALID, ["the signature block holds a key of no value: anyone can sign for it"], body)
    if len(public_raw) != 32 or claimed != key_id_of(public_raw):
        return Verdict(INVALID, ["the signature block names a key that does not match its own key id"], body)
    try:
        _, ed25519 = _crypto()
    except RecordError as exc:
        return Verdict(INVALID, [str(exc)], body, claimed)
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(public_raw).verify(value, canonical(body))
    except Exception:
        return Verdict(INVALID, ["the signature does not match the record: it was changed after it was signed"], body, claimed)
    if trusted_public is None:
        return Verdict(
            SIGNED,
            ["the signature is consistent, but the key was not compared with one you trust (use --record-pubkey)"],
            body,
            claimed,
        )
    if trusted_public != public_raw:
        return Verdict(
            INVALID,
            [f"the record was signed with a different key ({short_id(claimed)}) than the one you trust"],
            body,
            claimed,
        )
    return Verdict(TRUSTED, [], body, claimed)


def check_files(body: dict, *, original: bytes | None = None, cleaned: bytes | None = None) -> list[str]:
    """Problems found when the files themselves are compared with what the record says (empty: they match)."""
    problems = []
    for label, data, key in (("original", original, "input"), ("cleaned file", cleaned, "output")):
        if data is None:
            continue
        want = (body.get(key) or {}).get("sha256")
        if sha256(data) != want:
            problems.append(f"the {label} you gave does not match the record (SHA-256 differs)")
    return problems
