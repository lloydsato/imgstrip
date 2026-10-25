"""Content Credentials (C2PA): an optional, signed statement that imgstrip sanitised an image.

After an image has been rebuilt from its pixels and verified clean, this module can attach one
C2PA manifest that says, in a form anyone can check with standard tools::

    This image was sanitised by <signer> on <date> to protect its source, and the pixel data
    is <identical to | equivalent to | deliberately altered from> the file that was received.

The credential vouches for that *processing* only.  It says nothing about who made the image
or whether it is authentic, and it says so in the manifest itself.

Design rules, in the spirit of the rest of imgstrip:

* **Opt-in.**  Nothing here runs unless the caller asks for credentials.
* **Nothing from the original goes in.**  No byte of the source file is passed to the C2PA SDK.
  The manifest holds a fixed vocabulary of fields plus the signer's certificate chain.  The
  original's own Content Credentials, if it had any, are removed like any other metadata.
* **Fail closed.**  The signed file is re-verified before it is released: the inspector must
  find exactly one new item (the credential), the decoded pixels must equal those of the clean
  file, every byte of the manifest store is parsed by ``jumbf`` (padding must be zero), the
  manifest must match what was requested field for field, the signature is checked
  independently of the SDK, and the SDK must report a valid, hard-bound manifest.
* **Offline.**  The SDK's network features (remote manifests, OCSP, time-stamp authorities) are
  switched off, and a test proves they stay off.  The price is that signatures carry no trusted
  time-stamp, so viewers treat them as invalid once the signing certificate expires.
* **Keys stay in memory.**  The private key is held as a ``cryptography`` object, signs through a
  callback, and appears in no report, error message or temporary file.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import hashlib
import hmac
import io
import json
import math
import re
import secrets
import struct
from dataclasses import dataclass, field

import numpy as np

from . import __version__, jumbf, stego
from .core import DEFAULT_MAX_PIXELS, Options, Result, StripError, canonical_frames
from .inspect import Finding, inspect_bytes, is_c2pa_finding, sniff_format

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

LABEL = "org.imgstrip.sanitization"
ASSERTION_VERSION = 1
CLAIM_TITLE = "Sanitized image"
INGREDIENT_TITLE = "Image as received"
GENERATOR = "imgstrip"

# Formats the C2PA SDK can write.  BMP has no place for a manifest.  TIFF is excluded on
# purpose: the SDK appends a second directory and leaves the old one orphaned in the file,
# which the inspector (rightly) reports as unreferenced bytes.
SIGNABLE = ("JPEG", "PNG", "WEBP", "GIF", "AVIF", "HEIF")
MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "AVIF": "image/avif",
    "HEIF": "image/heic",
}

INTEGRITY = ("identical", "equivalent", "altered")
# The coarse vocabulary of things done to the pixels (listed only at detail "full").
OPERATIONS = (
    "colour-converted",
    "format-converted",
    "frames-dropped",
    "orientation-applied",
    "re-encoded",
    "sensor-fingerprint-disrupted",
    "lsb-scrubbed",
    "dct-scrubbed",
)
DATE_PRECISIONS = ("day", "minute")
DETAILS = ("minimal", "full")

_PIXEL_SENTENCE = {
    "identical": "The pixel data is identical to the file received.",
    "equivalent": "The picture is the same as in the file received but was re-encoded, so pixel values are not bit-identical.",
    "altered": "The pixel data was deliberately altered to protect the source.",
}
_REMOVAL_SENTENCE = "Metadata and hidden data were removed from this image to protect its source."
_SCOPE_SENTENCE = "This credential vouches for that processing only, not for the origin or authenticity of the image."

# Extra C2PA actions, so viewers that ignore our own assertion still read the pixel changes right.
_ACTION_DESCRIPTIONS = {
    "c2pa.edited.metadata": "Removed all metadata and hidden data",
    "c2pa.edited": "Pixel data deliberately altered to protect the source",
    "c2pa.transcoded": "Re-encoded",
    "c2pa.orientation": "Orientation applied to the pixels",
    "c2pa.converted": "Converted to another file format",
}

COMMITMENT_ALG = "hmac-sha256"
_COMMITMENT_DOMAIN = b"imgstrip/source-commitment/v1\x00"
RECEIPT_VERSION = 1

# --------------------------------------------------------------------------
# Errors and optional dependencies
# --------------------------------------------------------------------------


class CredentialError(StripError):
    """Credentials could not be created or verified (the reason is user-facing)."""


class CredentialsUnavailable(CredentialError):
    """The optional C2PA support is not installed."""


class PassphraseRequired(CredentialError):
    """The private key is encrypted and no (or the wrong) passphrase was given."""


INSTALL_HINT = "Content Credentials need the optional C2PA support: pip install 'imgstrip[c2pa]'"


def _sdk():
    try:
        import c2pa
    except ImportError:
        raise CredentialsUnavailable(INSTALL_HINT) from None
    except OSError as exc:  # the native library failed to load
        raise CredentialsUnavailable(f"The C2PA library could not be loaded ({exc}). {INSTALL_HINT}") from None
    return c2pa


def available() -> bool:
    try:
        _sdk()
        _crypto()
    except CredentialsUnavailable:
        return False
    return True


def sdk_version() -> str | None:
    try:
        return _sdk().sdk_version()
    except Exception:  # noqa: BLE001 - purely informational
        return None


def _crypto():
    try:
        import cryptography  # noqa: F401
    except ImportError:
        raise CredentialsUnavailable(INSTALL_HINT) from None
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    class _C:  # a namespace, so the imports above stay lazy
        pass

    c = _C()
    c.x509, c.hashes, c.serialization = x509, hashes, serialization
    c.ec, c.ed25519, c.padding, c.rsa = ec, ed25519, padding, rsa
    c.decode_dss_signature, c.encode_dss_signature = decode_dss_signature, encode_dss_signature
    c.NameOID, c.ExtendedKeyUsageOID = NameOID, ExtendedKeyUsageOID
    return c


def sdk_settings(anchors: bytes | None = None) -> dict:
    """Settings for every SDK context: no thumbnails, no network, whatever the build's defaults.

    The SDK's defaults *do* fetch remote manifests named in a file's XMP (a hostile file could
    make imgstrip phone home), and it silently ignores setting names it does not know, so the
    tests prove these two switches by watching a local server.
    """
    settings: dict = {
        "builder": {"thumbnail": {"enabled": False}},
        "verify": {"remote_manifest_fetch": False, "ocsp_fetch": False, "verify_after_sign": True},
        "core": {"allowed_network_hosts": []},
    }
    if anchors:
        settings["trust"] = {
            "user_anchors": anchors.decode("utf-8", "replace") if isinstance(anchors, bytes) else anchors
        }
    return settings


@contextlib.contextmanager
def _context(anchors: bytes | None = None):
    c2pa = _sdk()
    ctx = c2pa.Context(settings=c2pa.Settings.from_dict(sdk_settings(anchors)))
    try:
        yield ctx
    finally:
        with contextlib.suppress(Exception):
            ctx.close()


def _sdk_message(exc: Exception) -> str:
    """The SDK's own reason, shortened to one line (it never contains key material)."""
    text = re.sub(r"\s+", " ", str(exc)).strip()
    text = re.sub(r"urn:c2pa:[0-9a-f-]+|self#jumbf=\S+", "", text).strip()
    return text[:240]


# --------------------------------------------------------------------------
# Signing identity
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Notice:
    code: str
    severity: str  # error | warning | info
    message: str

    def as_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "message": self.message}


TEST_MARKER = "imgstrip TEST identity - not for publication"

_ALG_BY_CURVE = {"secp256r1": "ES256", "secp384r1": "ES384", "secp521r1": "ES512"}
_KEY_TEXT = {
    "ES256": "ECDSA P-256",
    "ES384": "ECDSA P-384",
    "ES512": "ECDSA P-521",
    "PS256": "RSA-PSS",
    "ED25519": "Ed25519",
}


def _name_attr(name, oid) -> str | None:
    attrs = name.get_attributes_for_oid(oid)
    return attrs[0].value if attrs else None


def _fingerprint(cert) -> str:
    c = _crypto()
    digest = cert.fingerprint(c.hashes.SHA256()).hex().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def _spki(public_key) -> bytes:
    c = _crypto()
    return public_key.public_bytes(c.serialization.Encoding.DER, c.serialization.PublicFormat.SubjectPublicKeyInfo)


class Identity:
    """A certificate chain and the private key that goes with its first certificate.

    The key never leaves this object: ``sign`` is the only operation, ``repr`` and ``str`` show
    the subject only, and pickling is refused (so it cannot be shipped to a worker process).
    """

    def __init__(self, certs: list, key, *, source: str = "files"):
        c = _crypto()
        self.source = source
        self._key = key
        self.alg = self._algorithm(key)
        chain, ignored = self._order(certs, key)
        self.leaf = chain[0]
        self.root = chain[-1] if len(chain) > 1 and chain[-1].issuer == chain[-1].subject else None
        # The self-signed root is left out of the embedded chain, as C2PA recommends: verifiers
        # take it from their own trust list.
        self.chain = chain[:-1] if self.root is not None else chain
        self.ignored_certificates = ignored
        self._pem = "".join(x.public_bytes(c.serialization.Encoding.PEM).decode("ascii") for x in self.chain)

    # -- construction -------------------------------------------------------

    @staticmethod
    def _algorithm(key) -> str:
        c = _crypto()
        if isinstance(key, c.ec.EllipticCurvePrivateKey):
            alg = _ALG_BY_CURVE.get(key.curve.name)
            if alg is None:
                raise CredentialError(
                    f"The key uses the elliptic curve {key.curve.name}; C2PA supports P-256, P-384 and P-521."
                )
            return alg
        if isinstance(key, c.rsa.RSAPrivateKey):
            if key.key_size < 2048:
                raise CredentialError(f"The RSA key is only {key.key_size} bits long; C2PA needs at least 2048.")
            return "PS256"
        if isinstance(key, c.ed25519.Ed25519PrivateKey):
            return "ED25519"
        raise CredentialError("Unsupported key type. C2PA supports ECDSA (P-256/384/521), RSA-PSS and Ed25519 keys.")

    @staticmethod
    def _order(certs: list, key) -> tuple[list, int]:
        if not certs:
            raise CredentialError("No certificate was found.")
        wanted = _spki(key.public_key())
        leaf = next((x for x in certs if _spki(x.public_key()) == wanted), None)
        if leaf is None:
            raise CredentialError("The private key does not belong to any certificate in the file.")
        chain, pool = [leaf], [x for x in certs if x is not leaf]
        while chain[-1].issuer != chain[-1].subject:
            top = chain[-1]
            nxt = None
            for cand in pool:
                if cand.subject == top.issuer:
                    try:
                        top.verify_directly_issued_by(cand)
                    except Exception:  # noqa: BLE001 - not the issuer: keep looking
                        continue
                    nxt = cand
                    break
            if nxt is None:
                break
            chain.append(nxt)
            pool.remove(nxt)
        return chain, len(pool)

    # -- the only use of the key -------------------------------------------------

    def sign(self, data: bytes) -> bytes:
        """A raw (COSE-format) signature over ``data``."""
        c = _crypto()
        key = self._key
        if isinstance(key, c.ec.EllipticCurvePrivateKey):
            h = {"ES256": c.hashes.SHA256, "ES384": c.hashes.SHA384, "ES512": c.hashes.SHA512}[self.alg]()
            r, s = c.decode_dss_signature(key.sign(data, c.ec.ECDSA(h)))
            n = (key.curve.key_size + 7) // 8
            return r.to_bytes(n, "big") + s.to_bytes(n, "big")
        if isinstance(key, c.rsa.RSAPrivateKey):
            h = c.hashes.SHA256()
            return key.sign(data, c.padding.PSS(mgf=c.padding.MGF1(h), salt_length=h.digest_size), h)
        return key.sign(data)

    def chain_pem(self) -> str:
        return self._pem

    def __repr__(self) -> str:
        return f"Identity({self.organization or self.common_name or 'unnamed'!r}, {self.alg})"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("a signing identity cannot be pickled; load it in the process that signs")

    def __copy__(self):
        raise TypeError("a signing identity cannot be copied")

    # -- facts ---------------------------------------------------------------

    @property
    def organization(self) -> str | None:
        return _name_attr(self.leaf.subject, _crypto().NameOID.ORGANIZATION_NAME)

    @property
    def common_name(self) -> str | None:
        return _name_attr(self.leaf.subject, _crypto().NameOID.COMMON_NAME)

    @property
    def display_name(self) -> str:
        return self.organization or self.common_name or "unnamed signer"

    @property
    def issuer_name(self) -> str:
        c = _crypto()
        return (
            _name_attr(self.leaf.issuer, c.NameOID.ORGANIZATION_NAME)
            or _name_attr(self.leaf.issuer, c.NameOID.COMMON_NAME)
            or "unknown issuer"
        )

    @property
    def is_test(self) -> bool:
        c = _crypto()
        for cert in (self.leaf, self.root):
            if cert is not None and TEST_MARKER in (_name_attr(cert.subject, c.NameOID.ORGANIZATIONAL_UNIT_NAME) or ""):
                return True
        return False

    @property
    def self_signed(self) -> bool:
        return self.leaf.issuer == self.leaf.subject

    @property
    def not_before(self) -> dt.datetime:
        return self.leaf.not_valid_before_utc

    @property
    def not_after(self) -> dt.datetime:
        return self.leaf.not_valid_after_utc


def _load_certs(pem: bytes) -> list:
    c = _crypto()
    try:
        return list(c.x509.load_pem_x509_certificates(pem))
    except ValueError:
        return []


# A wrong passphrase decrypts to garbage, and about 1 time in 300 the garbage ends in valid padding; the library then
# complains about the key data instead of the password.  A PEM that says it is encrypted settles it.
_ENCRYPTED_PEM = re.compile(rb"-----BEGIN ENCRYPTED PRIVATE KEY-----|Proc-Type:\s*4,\s*ENCRYPTED")


def _load_key(pem: bytes, passphrase: bytes | None):
    c = _crypto()
    try:
        return c.serialization.load_pem_private_key(pem, passphrase or None)
    except TypeError:
        if passphrase:
            raise CredentialError("The key file is not encrypted, but a passphrase was given.") from None
        raise PassphraseRequired("The private key is encrypted. A passphrase is needed.") from None
    except ValueError as exc:
        text = str(exc).lower()
        if passphrase and (
            _ENCRYPTED_PEM.search(pem) or "password" in text or "decrypt" in text or "bad" in text or "mac" in text
        ):
            raise PassphraseRequired("The passphrase is wrong, or the key file is damaged.") from None
        raise CredentialError("The private key could not be read (is it a PEM-encoded key?).") from None
    except Exception:  # noqa: BLE001 - never echo library internals that might touch key bytes
        raise CredentialError("The private key could not be read.") from None


_PEM_KEY = re.compile(rb"-----BEGIN (?:[A-Z0-9 ]*)PRIVATE KEY-----")


def load_pem_identity(cert_pem: bytes, key_pem: bytes | None = None, passphrase: bytes | None = None) -> Identity:
    """An identity from PEM data.  If ``key_pem`` is None the key is looked for in ``cert_pem``."""
    _crypto()
    certs = _load_certs(cert_pem)
    if not certs:
        raise CredentialError("No PEM certificate was found in the certificate file.")
    if key_pem is None:
        if not _PEM_KEY.search(cert_pem):
            raise CredentialError("No private key was given, and the certificate file does not contain one.")
        key_pem = cert_pem
    elif not _PEM_KEY.search(key_pem):
        raise CredentialError("The key file does not contain a PEM private key.")
    return Identity(certs, _load_key(key_pem, passphrase))


def load_pkcs12_identity(data: bytes, passphrase: bytes | None = None) -> Identity:
    """An identity from a PKCS#12 (.p12 / .pfx) bundle."""
    _crypto()
    from cryptography.hazmat.primitives.serialization import pkcs12

    try:
        key, cert, extra = pkcs12.load_key_and_certificates(data, passphrase or None)
    except ValueError as exc:
        text = str(exc).lower()
        if passphrase is None and ("password" in text or "mac" in text):
            raise PassphraseRequired("This certificate bundle is protected. A passphrase is needed.") from None
        if passphrase is not None:
            raise PassphraseRequired("The passphrase is wrong, or the bundle is damaged.") from None
        raise CredentialError("The certificate bundle could not be read (is it a .p12 / .pfx file?).") from None
    except Exception:  # noqa: BLE001
        raise CredentialError("The certificate bundle could not be read (is it a .p12 / .pfx file?).") from None
    if key is None or cert is None:
        raise CredentialError("The bundle must contain a private key and its certificate.")
    return Identity([cert, *(extra or [])], key, source="pkcs12")


def load_identity(cert_data: bytes, key_data: bytes | None = None, passphrase: bytes | None = None) -> Identity:
    """Load PEM (certificate chain with the key in the same or a second file) or PKCS#12."""
    if b"-----BEGIN" not in cert_data and key_data is None:
        return load_pkcs12_identity(cert_data, passphrase)
    return load_pem_identity(cert_data, key_data, passphrase)


def load_anchors(pem: bytes) -> bytes:
    """Check a file of trust anchors (CA certificates) and return it as clean PEM."""
    c = _crypto()
    certs = _load_certs(pem)
    if not certs:
        raise CredentialError("No PEM certificate was found in the trust-anchor file.")
    return "".join(x.public_bytes(c.serialization.Encoding.PEM).decode("ascii") for x in certs).encode("ascii")


# --------------------------------------------------------------------------
# A private test identity (a throw-away CA and a signing certificate)
# --------------------------------------------------------------------------


@dataclass
class TestIdentity:
    __test__ = False  # not a pytest class

    identity: Identity
    chain_pem: bytes  # signing certificate, to give to imgstrip
    key_pem: bytes  # private key, to give to imgstrip
    root_pem: bytes  # the test CA: add it to a viewer's trust anchors to see "Trusted"


def make_test_identity(
    organization: str = "Test Newsroom",
    name: str = "Test Signer",
    *,
    days: int = 365,
    passphrase: bytes | None = None,
    now: dt.datetime | None = None,
) -> TestIdentity:
    """Create a throw-away CA and signing certificate, clearly marked as a test.

    The C2PA SDK refuses a self-signed signing certificate, so a trial needs a two-step chain.
    Credentials made with it are cryptographically valid but untrusted: public viewers show
    them as "Unverified" unless the viewer has added the test CA as a trust anchor.
    """
    c = _crypto()
    if not 1 <= days <= 3650:
        raise CredentialError("A test certificate can be valid for 1 to 3650 days.")
    now = (now or dt.datetime.now(dt.timezone.utc)).replace(microsecond=0)
    start = now - dt.timedelta(minutes=5)

    def subject(cn: str, org: str):
        return c.x509.Name(
            [
                c.x509.NameAttribute(c.NameOID.ORGANIZATION_NAME, org[:64]),
                c.x509.NameAttribute(c.NameOID.ORGANIZATIONAL_UNIT_NAME, TEST_MARKER),
                c.x509.NameAttribute(c.NameOID.COMMON_NAME, cn[:64]),
            ]
        )

    def usage(**kw):
        base = dict.fromkeys(
            (
                "digital_signature",
                "content_commitment",
                "key_encipherment",
                "data_encipherment",
                "key_agreement",
                "key_cert_sign",
                "crl_sign",
                "encipher_only",
                "decipher_only",
            ),
            False,
        )
        base.update(kw)
        return c.x509.KeyUsage(**base)

    ca_key = c.ec.generate_private_key(c.ec.SECP256R1())
    ca_name = subject(f"{organization[:40]} test CA", organization)
    ca = (
        c.x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(c.x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(now + dt.timedelta(days=days + 1))
        .add_extension(c.x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(usage(key_cert_sign=True, crl_sign=True), critical=True)
        .add_extension(c.x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, c.hashes.SHA256())
    )
    key = c.ec.generate_private_key(c.ec.SECP256R1())
    leaf = (
        c.x509.CertificateBuilder()
        .subject_name(subject(name, organization))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(c.x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(c.x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(usage(digital_signature=True), critical=True)
        .add_extension(c.x509.ExtendedKeyUsage([c.ExtendedKeyUsageOID.EMAIL_PROTECTION]), critical=False)
        .add_extension(c.x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(c.x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, c.hashes.SHA256())
    )
    pem = c.serialization.Encoding.PEM
    encryption = c.serialization.BestAvailableEncryption(passphrase) if passphrase else c.serialization.NoEncryption()
    key_pem = key.private_bytes(pem, c.serialization.PrivateFormat.PKCS8, encryption)
    chain_pem = leaf.public_bytes(pem)
    root_pem = ca.public_bytes(pem)
    return TestIdentity(Identity([leaf, ca], key, source="test"), chain_pem, key_pem, root_pem)


# --------------------------------------------------------------------------
# Assessing an identity: what will public viewers make of it?
# --------------------------------------------------------------------------


@dataclass
class Assessment:
    organization: str | None
    common_name: str | None
    issuer: str
    valid_from: str
    valid_until: str
    key: str
    algorithm: str
    fingerprint: str
    chain_length: int
    test: bool
    trust: str  # trusted | unverified
    state: str | None  # what a probe signature read back as: Valid | Trusted
    notices: list[Notice]

    @property
    def can_sign(self) -> bool:
        return not any(n.severity == "error" for n in self.notices)

    @property
    def signer(self) -> str:
        return self.organization or self.common_name or "unnamed signer"

    def as_dict(self) -> dict:
        return {
            "signer": self.signer,
            "organization": self.organization,
            "common_name": self.common_name,
            "issuer": self.issuer,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "key": self.key,
            "algorithm": self.algorithm,
            "fingerprint": self.fingerprint,
            "chain_length": self.chain_length,
            "test": self.test,
            "trust": self.trust,
            "state": self.state,
            "can_sign": self.can_sign,
            "notices": [n.as_dict() for n in self.notices],
        }


EXPIRY_WARNING_DAYS = 30


def _date(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%d")


def _sdk_rejection(message: str) -> str:
    low = message.lower()
    if "certificate is invalid" in low:
        return (
            "The C2PA library rejected this certificate. It is not a valid signing certificate "
            "(for example it is self-signed, or it lacks the digital-signature key usage)."
        )
    if "eku" in low or "extended key usage" in low:
        return "The certificate lacks the extended key usage C2PA requires for signing (e-mail protection)."
    if "expired" in low:
        return "The C2PA library says the certificate has expired."
    return f"The C2PA library refused this identity: {message}"


def assess(
    identity: Identity,
    anchors: bytes | None = None,
    *,
    now: dt.datetime | None = None,
    probe: bool = True,
) -> Assessment:
    """Judge a signing identity before it is used, in the words a journalist needs.

    The notices follow what a public viewer will see.  With ``probe`` the identity signs a tiny
    throw-away image, so "the SDK accepts this certificate" is checked, not guessed, and so is
    whether the result reads as trusted against ``anchors`` (when given).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    notices: list[Notice] = []
    leaf = identity.leaf

    if identity.self_signed:
        notices.append(
            Notice(
                "self_signed",
                "error",
                "This certificate is self-signed, and the C2PA library refuses to sign with one. Use a certificate "
                "issued by a certificate authority. For a trial, create a test identity.",
            )
        )
    if identity.not_after < now:
        notices.append(
            Notice(
                "expired",
                "error",
                f"This certificate expired on {_date(identity.not_after)}. Credentials signed with it would show as invalid.",
            )
        )
    elif identity.not_before > now:
        notices.append(
            Notice(
                "not_yet_valid",
                "error",
                f"This certificate is not valid until {_date(identity.not_before)}. Check this computer's clock.",
            )
        )
    elif identity.not_after - now <= dt.timedelta(days=EXPIRY_WARNING_DAYS):
        notices.append(
            Notice(
                "expires_soon",
                "warning",
                f"This certificate expires on {_date(identity.not_after)}. After that, viewers show every credential "
                "signed with it as expired.",
            )
        )
    c = _crypto()
    try:
        bc = leaf.extensions.get_extension_for_class(c.x509.BasicConstraints).value
        if bc.ca:
            notices.append(
                Notice(
                    "ca_certificate", "error", "This is a certificate authority certificate, not a signing certificate."
                )
            )
    except c.x509.ExtensionNotFound:
        pass
    try:
        ku = leaf.extensions.get_extension_for_class(c.x509.KeyUsage).value
        if not ku.digital_signature:
            notices.append(Notice("key_usage", "error", "The certificate is not allowed to make digital signatures."))
    except c.x509.ExtensionNotFound:
        pass

    state = None
    trust = "unverified"
    if probe and not any(n.severity == "error" for n in notices):
        try:
            state = _probe(identity, anchors)
        except CredentialsUnavailable:
            raise
        except CredentialError as exc:
            notices.append(Notice("rejected", "error", str(exc)))
    if state == "Trusted":
        trust = "trusted"

    if identity.is_test:
        notices.append(
            Notice(
                "test_identity",
                "warning",
                "You are using a test/development certificate. The image will be signed, but public viewers will see an "
                "'Unverified' warning. For publication, use a certificate issued by a certificate authority on the "
                "C2PA Trust List.",
            )
        )
    if trust == "unverified" and state is not None and not identity.is_test:
        if anchors:
            notices.append(
                Notice(
                    "untrusted",
                    "warning",
                    "This certificate does not chain to the trust anchors you supplied, so viewers that use that list "
                    "will show these credentials as 'Unverified'.",
                )
            )
        else:
            notices.append(
                Notice(
                    "trust_unknown",
                    "info",
                    "imgstrip works offline, so it cannot check this certificate against the C2PA Trust List. Public "
                    "viewers show 'Unverified' unless the issuer is on that list, or the viewer has added your root "
                    "certificate themselves.",
                )
            )
    if trust == "trusted":
        notices.append(Notice("trusted", "info", "This certificate chains to the trust anchors you supplied."))
    if identity.ignored_certificates:
        notices.append(
            Notice(
                "ignored_certificates",
                "info",
                f"{identity.ignored_certificates} certificate(s) in the file are not part of this chain and were ignored.",
            )
        )
    if not any(n.severity == "error" for n in notices):
        notices.append(
            Notice(
                "no_timestamp",
                "info",
                "Credentials made here carry no trusted time-stamp, because imgstrip never contacts the internet. "
                f"Viewers treat them as invalid once this certificate expires ({_date(identity.not_after)}).",
            )
        )
    order = {"error": 0, "warning": 1, "info": 2}
    notices.sort(key=lambda n: order[n.severity])
    return Assessment(
        organization=identity.organization,
        common_name=identity.common_name,
        issuer=identity.issuer_name,
        valid_from=_date(identity.not_before),
        valid_until=_date(identity.not_after),
        key=_KEY_TEXT[identity.alg],
        algorithm=identity.alg,
        fingerprint=_fingerprint(leaf),
        chain_length=len(identity.chain) + (1 if identity.root is not None else 0),
        test=identity.is_test,
        trust=trust,
        state=state,
        notices=notices,
    )


def _probe(identity: Identity, anchors: bytes | None) -> str:
    """Sign a 4x4 image with ``identity`` and read it back; returns the validation state."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("L", (4, 4), 128).save(buf, "PNG")
    clean = buf.getvalue()
    evidence = Evidence(
        integrity="identical",
        operations=[],
        psnr_db=None,
        source_pixels_sha256="0" * 64,
        source_sha256="0" * 64,
        frames=1,
    )
    signed = _sign_bytes(
        clean, "PNG", identity, evidence, Config(), now=dt.datetime.now(dt.timezone.utc), instance=None
    )
    with _context(anchors) as ctx, _sdk().Reader("image/png", io.BytesIO(signed), context=ctx) as reader:
        return reader.get_validation_state() or "Invalid"


# --------------------------------------------------------------------------
# Evidence: what happened to the pixels
# --------------------------------------------------------------------------


@dataclass
class Evidence:
    """Facts gathered next to the original file; small and picklable, holds no pixels."""

    integrity: str  # identical | equivalent | altered
    operations: list[str]
    psnr_db: float | None
    source_pixels_sha256: str
    source_sha256: str
    frames: int
    source_credentials: dict | None = None  # what the original's own Content Credentials said, if any


def _digest(frames: list[np.ndarray]) -> str:
    h = hashlib.sha256()
    h.update(b"imgstrip/pixels/v1\x00" + struct.pack(">I", len(frames)))
    for a in frames:
        a = np.ascontiguousarray(a)
        h.update(struct.pack(">III", *(list(a.shape) + [1] * (3 - a.ndim))[:3]) + a.dtype.str.encode() + b"\x00")
        h.update(a.view(np.uint8).reshape(-1))
    return h.hexdigest()


def _psnr(a: list[np.ndarray], b: list[np.ndarray]) -> float | None:
    if len(a) != len(b) or any(x.shape != y.shape or x.dtype != y.dtype for x, y in zip(a, b, strict=True)):
        return None
    sse, count = 0.0, 0
    for x, y in zip(a, b, strict=True):
        if x.dtype not in (np.uint8, np.uint16):
            return None
        scale = 1.0 if x.dtype == np.uint8 else 1 / 257
        rows = max(1, (1 << 22) // max(1, x[0].size))
        for top in range(0, x.shape[0], rows):
            d = x[top : top + rows].astype(np.float64) - y[top : top + rows].astype(np.float64)
            sse += float((d * d).sum()) * scale * scale
        count += x.size
    if not count or sse == 0:
        return None
    return round(10 * math.log10(255.0**2 / (sse / count)), 1)


def _operations(result: Result, options: Options, source_frames: int) -> list[str]:
    ops = set()
    if result.input_format != result.output_format:
        ops.add("format-converted")
    if not result.verified_exact and result.dct_scrub != stego.DCT_SCRUBBED:
        ops.add("re-encoded")
    if any(a.startswith("applied EXIF orientation") for a in result.actions):
        ops.add("orientation-applied")
    if any(a.startswith("converted colours from ICC") or a.startswith("converted Lab") for a in result.actions):
        ops.add("colour-converted")
    if result.prnu_applied:
        ops.add("sensor-fingerprint-disrupted")
    if result.lsb_scrub in (stego.SCRUB_APPLIED, stego.SCRUB_PARTIAL):  # what was done, not what was asked
        ops.add("lsb-scrubbed")
    if result.dct_scrub == stego.DCT_SCRUBBED:
        ops.add("dct-scrubbed")
    if result.frames != source_frames:
        ops.add("frames-dropped")
    return sorted(ops)


def gather_evidence(source: bytes, result: Result, options: Options) -> Evidence:
    """Compare what was received with what imgstrip produced, and record what the original claimed.

    This decodes both files again; it is only done when credentials are requested.
    """
    want = canonical_frames(source, options.max_pixels, options.sandbox)
    got = canonical_frames(result.data, options.max_pixels, options.sandbox)
    identical = len(want) == len(got) and all(
        x.shape == y.shape and x.dtype == y.dtype and np.array_equal(x, y) for x, y in zip(want, got, strict=True)
    )
    ops = _operations(result, options, len(want))
    deliberate = any(op in ops for op in ("sensor-fingerprint-disrupted", "lsb-scrubbed", "dct-scrubbed"))
    if identical:
        integrity, psnr = "identical", None
    else:
        integrity = "altered" if deliberate else "equivalent"
        psnr = _psnr(want, got)
    return Evidence(
        integrity=integrity,
        operations=ops,
        psnr_db=psnr,
        source_pixels_sha256=_digest(want),
        source_sha256=hashlib.sha256(source).hexdigest(),
        frames=len(want),
        source_credentials=read_source_credentials(source, result),
    )


_AI_SOURCE_TYPES = (
    "trainedalgorithmicmedia",
    "compositewithtrainedalgorithmicmedia",
    "algorithmicmedia",
    "compositesynthetic",
    "trainedalgorithmicdata",
)


def read_source_credentials(source: bytes, result: Result) -> dict | None:
    """What the original's own Content Credentials said (they are removed, like all metadata).

    Reported to the person cleaning the file, never copied into the new credential, except that
    a declaration of AI generation is carried over: dropping that silently would let a sanitised
    copy pass as a camera original.
    """
    report = result.input_report
    if report is None or not any(is_c2pa_finding(f) for f in report.findings):
        return None
    info: dict = {"present": True, "readable": False, "valid": None, "signer": None, "declared_ai": False}
    try:
        cred = read_credential(source)
    except CredentialsUnavailable:
        raise
    except Exception:  # noqa: BLE001 - the original's credentials are only reported, never trusted
        return info
    info["readable"] = cred.state is not None
    info["valid"] = None if cred.state is None else cred.state in ("Valid", "Trusted")
    info["signer"] = cred.signer
    info["declared_ai"] = cred.declared_ai
    return info


def source_warnings(evidence: Evidence) -> list[str]:
    """What the person cleaning should know about the original's own Content Credentials."""
    src = evidence.source_credentials
    if not src:
        return []
    who = f" (signed by {src['signer']})" if src.get("signer") else ""
    out = [
        f"the original carried Content Credentials{who}; they were removed, and the new credential vouches only for "
        "your processing, not for where the image came from"
    ]
    if src.get("declared_ai"):
        out.append(
            "the original's Content Credentials declared the image AI-generated; the new credential records that"
        )
    return out


# --------------------------------------------------------------------------
# The statement and the manifest
# --------------------------------------------------------------------------


@dataclass
class Config:
    date_precision: str = "day"  # day | minute: how exactly the date is recorded
    detail: str = "minimal"  # minimal | full: also list the operations and measured fidelity
    commit_source: bool = False  # add a salted commitment to the original's pixels (see make_receipt)
    anchors: bytes | None = None  # trust anchors used only to classify the read-back
    now: dt.datetime | None = None

    def validate(self) -> None:
        if self.date_precision not in DATE_PRECISIONS:
            raise CredentialError(f"date precision must be one of {', '.join(DATE_PRECISIONS)}")
        if self.detail not in DETAILS:
            raise CredentialError(f"detail must be one of {', '.join(DETAILS)}")


def statement_text(integrity: str) -> str:
    return f"{_REMOVAL_SENTENCE} {_PIXEL_SENTENCE[integrity]} {_SCOPE_SENTENCE}"


def _when(now: dt.datetime, precision: str) -> str:
    now = now.astimezone(dt.timezone.utc)
    return now.strftime("%Y-%m-%d") if precision == "day" else now.strftime("%Y-%m-%dT%H:%MZ")


def new_commitment(evidence: Evidence) -> tuple[bytes, str]:
    """A random salt and the commitment HMAC(salt, source pixel digest), base64 encoded."""
    salt = secrets.token_bytes(32)
    return salt, _commitment(salt, evidence.source_pixels_sha256)


def _commitment(salt: bytes, pixels_sha256: str) -> str:
    mac = hmac.new(salt, _COMMITMENT_DOMAIN + bytes.fromhex(pixels_sha256), hashlib.sha256).digest()
    return base64.b64encode(mac).decode("ascii")


def build_assertion(evidence: Evidence, config: Config, now: dt.datetime, commitment: str | None) -> dict:
    """The content of the ``org.imgstrip.sanitization`` assertion (a fixed vocabulary)."""
    data: dict = {
        "version": ASSERTION_VERSION,
        "statement": statement_text(evidence.integrity),
        "sanitized": True,
        "date": _when(now, config.date_precision),
        "pixel_integrity": evidence.integrity,
        "scope": "processing",
    }
    if config.detail == "full":
        data["operations"] = list(evidence.operations)
        if evidence.psnr_db is not None:
            data["psnr_db"] = evidence.psnr_db
    if evidence.source_credentials and evidence.source_credentials.get("declared_ai"):
        data["source_declared_ai_generated"] = True
    if commitment:
        data["source_commitment"] = {"alg": COMMITMENT_ALG, "value": commitment}
    return data


def _actions(evidence: Evidence, config: Config) -> list[dict]:
    """C2PA actions after the automatic ``c2pa.opened``: honest for viewers that skip our assertion."""
    names = ["c2pa.edited.metadata"]
    if config.detail == "full":
        ops = set(evidence.operations)
        if "format-converted" in ops:
            names.append("c2pa.converted")
        if "re-encoded" in ops:
            names.append("c2pa.transcoded")
        if "orientation-applied" in ops:
            names.append("c2pa.orientation")
    elif evidence.integrity == "equivalent":
        names.append("c2pa.transcoded")
    if evidence.integrity == "altered":
        names.append("c2pa.edited")
    return [{"action": n, "description": _ACTION_DESCRIPTIONS[n]} for n in names]


def _instance_id() -> str:
    h = secrets.token_hex(16)
    return f"xmp:iid:{h[:8]}-{h[8:12]}-4{h[13:16]}-8{h[17:20]}-{h[20:32]}"


def _manifest_definition(fmt: str, assertion: dict, instance: str | None) -> dict:
    return {
        "claim_generator_info": [{"name": GENERATOR, "version": __version__}],
        "title": CLAIM_TITLE,
        "ingredients": [
            {
                "title": INGREDIENT_TITLE,
                "relationship": "parentOf",
                "instance_id": instance or _instance_id(),
                "format": MIME[fmt],
            }
        ],
        "assertions": [{"label": LABEL, "data": assertion}],
    }


# --------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------


def _sign_bytes(
    clean: bytes,
    fmt: str,
    identity: Identity,
    evidence: Evidence,
    config: Config,
    *,
    now: dt.datetime,
    instance: str | None,
    commitment: str | None = None,
) -> bytes:
    c2pa = _sdk()
    assertion = build_assertion(evidence, config, now, commitment)
    definition = _manifest_definition(fmt, assertion, instance)
    try:
        with _context(None) as ctx:
            with c2pa.Signer.from_callback(
                identity.sign, getattr(c2pa.C2paSigningAlg, identity.alg), identity.chain_pem()
            ) as signer:
                with c2pa.Builder(definition, ctx) as builder:
                    builder.set_intent(c2pa.C2paBuilderIntent.EDIT)
                    for action in _actions(evidence, config):
                        builder.add_action(action)
                    out = io.BytesIO()
                    builder.sign(signer, MIME[fmt], io.BytesIO(clean), out)
                    return out.getvalue()
    except CredentialError:
        raise
    except c2pa.C2paError as exc:
        raise CredentialError(_sdk_rejection(_sdk_message(exc))) from None
    except Exception as exc:  # noqa: BLE001 - never let an SDK exception carry anything else
        raise CredentialError(f"Signing failed ({type(exc).__name__}).") from None


def unsignable_reason(fmt: str | None) -> str | None:
    """Why files of this format cannot carry a credential, or None if they can."""
    if fmt in SIGNABLE:
        return None
    hint = " Use --format png for a lossless file, or jpeg/webp." if fmt in ("BMP", "TIFF") else ""
    return f"{fmt or 'This'} files cannot carry Content Credentials.{hint}"


@dataclass
class Signed:
    data: bytes
    credential: Credential
    receipt: dict | None = None


def sign(
    clean: bytes,
    evidence: Evidence,
    identity: Identity,
    config: Config | None = None,
    *,
    source_sha256: str | None = None,
) -> Signed:
    """Attach a Content Credential to an already cleaned and verified image.

    Raises CredentialError (and returns nothing) unless the signed file passes every check in
    ``verify_signed``.
    """
    config = config or Config()
    config.validate()
    _sdk()
    fmt = sniff_format(clean)
    reason = unsignable_reason(fmt)
    if reason:
        raise CredentialError(reason)
    now = config.now or dt.datetime.now(dt.timezone.utc)
    problems = [n for n in assess(identity, config.anchors, now=now, probe=False).notices if n.severity == "error"]
    if problems:
        raise CredentialError(problems[0].message)
    instance = _instance_id()
    salt = commitment = None
    if config.commit_source:
        salt, commitment = new_commitment(evidence)
    signed = _sign_bytes(clean, fmt, identity, evidence, config, now=now, instance=instance, commitment=commitment)
    expected = build_assertion(evidence, config, now, commitment)
    credential = verify_signed(signed, clean, fmt, identity, expected, config, instance, evidence)
    receipt = None
    if config.commit_source:
        receipt = make_receipt(evidence, salt, commitment, signed, credential, now, source_sha256)
    return Signed(signed, credential, receipt)


# --------------------------------------------------------------------------
# Verifying what was just written (fail closed)
# --------------------------------------------------------------------------

_CLAIM_KEYS = {
    "alg",
    "claim_generator_info",
    "created_assertions",
    "gathered_assertions",
    "dc:title",
    "instanceID",
    "signature",
}
_FORBIDDEN_ASSERTIONS = ("c2pa.thumbnail", "c2pa.metadata", "stds.", "cawg.", "c2pa.location", "c2pa.depthmap")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_COSE_ALG = {"ES256": -7, "ES384": -35, "ES512": -36, "PS256": -37, "PS384": -38, "PS512": -39, "ED25519": -8}


def _cbor_text(s: str) -> bytes:
    raw = s.encode("utf-8")
    n = len(raw)
    return (bytes([0x60 | n]) if n < 24 else bytes([0x78, n])) + raw


def _sig_structure(protected: bytes, payload: bytes) -> bytes:
    """The bytes a COSE_Sign1 signature covers (RFC 9052 section 4.4), payload detached."""
    return (
        b"\x84"
        + _cbor_text("Signature1")
        + jumbf.cbor_encode_bstr(protected)
        + jumbf.cbor_encode_bstr(b"")
        + jumbf.cbor_encode_bstr(payload)
    )


def check_cose_signature(manifest: jumbf.Manifest, expect_chain: list | None = None) -> object:
    """Verify a manifest's signature with ``cryptography`` alone, without the SDK.

    Returns the signing certificate.  With ``expect_chain`` the embedded certificates must be
    byte-identical to it.
    """
    c = _crypto()
    sign1 = manifest.signature.value
    protected, unprotected, payload, signature = sign1
    if not isinstance(protected, bytes) or not isinstance(unprotected, dict) or not isinstance(signature, bytes):
        raise CredentialError("The signature structure is malformed.")
    if payload is not None:
        raise CredentialError("The signature is not detached from the claim.")
    try:
        header = jumbf.cbor_decode(protected)
    except jumbf.JumbfError as exc:
        raise CredentialError(f"The signature header is malformed ({exc}).") from None
    if not isinstance(header, dict) or set(header) - {1, 33}:
        raise CredentialError("The signature's protected header holds unexpected fields.")
    alg = header.get(1)
    unknown = set(unprotected) - {33, "pad"}
    if unknown:
        raise CredentialError("The signature's unprotected header holds unexpected fields.")
    chain = unprotected.get(33, header.get(33))
    chain = [chain] if isinstance(chain, bytes) else chain
    if not isinstance(chain, list) or not chain or not all(isinstance(x, bytes) for x in chain):
        raise CredentialError("The signature carries no certificate chain.")
    try:
        certs = [c.x509.load_der_x509_certificate(x) for x in chain]
    except ValueError:
        raise CredentialError("The signature's certificate chain is malformed.") from None
    if expect_chain is not None:
        want = [x.public_bytes(c.serialization.Encoding.DER) for x in expect_chain]
        if list(chain) != want:
            raise CredentialError("The credential carries a different certificate chain than the one used to sign.")
    leaf = certs[0]
    tbs = _sig_structure(protected, manifest.claim_bytes)
    try:
        pub = leaf.public_key()  # a certificate can parse yet hold a damaged key
        if isinstance(pub, c.ec.EllipticCurvePublicKey):
            n = (pub.curve.key_size + 7) // 8
            if alg not in (-7, -35, -36) or len(signature) != 2 * n:
                raise CredentialError("The signature does not match the key type.")
            h = {-7: c.hashes.SHA256, -35: c.hashes.SHA384, -36: c.hashes.SHA512}[alg]()
            der = c.encode_dss_signature(int.from_bytes(signature[:n], "big"), int.from_bytes(signature[n:], "big"))
            pub.verify(der, tbs, c.ec.ECDSA(h))
        elif isinstance(pub, c.rsa.RSAPublicKey):
            if alg not in (-37, -38, -39):
                raise CredentialError("The signature does not match the key type.")
            h = {-37: c.hashes.SHA256, -38: c.hashes.SHA384, -39: c.hashes.SHA512}[alg]()
            pub.verify(signature, tbs, c.padding.PSS(mgf=c.padding.MGF1(h), salt_length=h.digest_size), h)
        elif isinstance(pub, c.ed25519.Ed25519PublicKey):
            if alg != -8:
                raise CredentialError("The signature does not match the key type.")
            pub.verify(signature, tbs)
        else:
            raise CredentialError("The signing key type is not supported.")
    except CredentialError:
        raise
    except Exception:  # noqa: BLE001 - InvalidSignature and friends
        raise CredentialError("The signature does not verify.") from None
    return leaf


def check_manifest_shape(store: jumbf.Store, fmt: str) -> tuple[jumbf.Manifest, dict]:
    """Require exactly the manifest imgstrip writes; returns the manifest and our assertion.

    Anything beyond the fixed vocabulary (an extra assertion, a thumbnail, another manifest, an
    odd claim field) raises CredentialError: a credential that fails this is not ours.
    """
    if len(store.manifests) != 1:
        raise CredentialError("The credential holds more than one manifest.")
    m = store.manifests[0]
    claim = m.claim
    if set(claim) - _CLAIM_KEYS or not {"alg", "claim_generator_info", "dc:title", "instanceID", "signature"} <= set(
        claim
    ):
        raise CredentialError("The claim holds unexpected fields.")
    if claim["alg"] != "sha256" or claim["dc:title"] != CLAIM_TITLE:
        raise CredentialError("The claim has unexpected values.")
    gen = claim["claim_generator_info"]
    if not (
        isinstance(gen, dict)
        and gen.get("name") == GENERATOR
        and isinstance(gen.get("version"), str)
        and re.fullmatch(r"[0-9A-Za-z.+-]{1,32}", gen["version"])
        and set(gen) <= {"name", "version", "org.contentauth.c2pa_rs"}
        and isinstance(gen.get("org.contentauth.c2pa_rs", ""), str)
    ):
        raise CredentialError("The claim generator is not imgstrip.")
    if not (isinstance(claim["instanceID"], str) and re.fullmatch(r"xmp[.:]iid:" + _UUID.pattern, claim["instanceID"])):
        raise CredentialError("The claim's instance id is malformed.")
    labels = set(m.assertions)
    hard = {"c2pa.hash.bmff.v3"} if fmt in ("AVIF", "HEIF") else {"c2pa.hash.data"}
    required = {LABEL, "c2pa.actions.v2", "c2pa.ingredient.v3"} | hard
    if labels != required:
        extra = sorted(labels - required)
        raise CredentialError(
            "The credential holds unexpected assertions: " + ", ".join(extra)
            if extra
            else "The credential is missing an assertion."
        )
    for label in labels:
        if label.startswith(_FORBIDDEN_ASSERTIONS):
            raise CredentialError(f"The credential holds a forbidden assertion ({label}).")
    # The ingredient: a label for "the file as received", with nothing about it.
    ing = m.assertions["c2pa.ingredient.v3"]
    if not (
        isinstance(ing, dict)
        and set(ing) == {"relationship", "dc:title", "dc:format", "instanceID"}
        and ing["relationship"] == "parentOf"
        and ing["dc:title"] == INGREDIENT_TITLE
        and ing["dc:format"] == MIME[fmt]
        and isinstance(ing["instanceID"], str)
        and re.fullmatch(r"xmp[.:]iid:" + _UUID.pattern, ing["instanceID"])
    ):
        raise CredentialError("The credential's ingredient holds unexpected data.")
    # The hard binding: only hashes and the exclusion of the manifest itself.
    binding = m.assertions[next(iter(hard))]
    if not isinstance(binding, dict) or binding.get("alg") != "sha256" or not isinstance(binding.get("hash"), bytes):
        raise CredentialError("The credential's hard binding is malformed.")
    if len(binding["hash"]) != 32 or set(binding) - {"exclusions", "name", "alg", "hash", "pad", "pad1", "pad2"}:
        raise CredentialError("The credential's hard binding holds unexpected data.")
    sanitization = m.assertions[LABEL]
    return m, sanitization


def _check_actions(m: jumbf.Manifest, evidence: Evidence, config: Config) -> None:
    actions = m.assertions["c2pa.actions.v2"]
    items = actions.get("actions") if isinstance(actions, dict) else None
    if not (
        isinstance(actions, dict) and set(actions) <= {"actions", "allActionsIncluded"} and isinstance(items, list)
    ):
        raise CredentialError("The credential's actions are malformed.")
    want = _actions(evidence, config)
    if len(items) != len(want) + 1:
        raise CredentialError("The credential lists unexpected actions.")
    first = items[0]
    ok = (
        isinstance(first, dict)
        and first.get("action") == "c2pa.opened"
        and set(first) <= {"action", "parameters"}
        and isinstance(first.get("parameters"), dict)
        and set(first["parameters"]) == {"ingredients"}
        and isinstance(first["parameters"]["ingredients"], list)
        and len(first["parameters"]["ingredients"]) == 1
    )
    if not ok:
        raise CredentialError("The credential's first action is not the expected one.")
    for got, wanted in zip(items[1:], want, strict=True):
        if got != wanted:
            raise CredentialError("The credential lists unexpected actions.")


def _expected_new_findings(clean: bytes, signed: bytes, fmt: str) -> None:
    before = inspect_bytes(clean)
    after = inspect_bytes(signed)
    if after.error:
        raise CredentialError(f"The signed file failed structural verification: {after.error}")
    if after.format != fmt:
        raise CredentialError(f"The signed file is not a valid {fmt} file.")
    new = [f for f in after.findings if f not in before.findings]
    gone = [f for f in before.findings if f not in after.findings]
    if gone:
        raise CredentialError("Signing changed the file's structure.")
    credentials = [f for f in new if is_c2pa_finding(f)]
    others = [f for f in new if not is_c2pa_finding(f)]
    if others:
        raise CredentialError("Signing added more than the credential: " + "; ".join(map(str, others)))
    if len(credentials) != 1:
        raise CredentialError("The signed file does not hold exactly one credential.")


def verify_signed(
    signed: bytes,
    clean: bytes,
    fmt: str,
    identity: Identity,
    expected: dict,
    config: Config,
    instance: str | None = None,
    evidence: Evidence | None = None,
) -> Credential:
    """Every check a freshly signed file must pass before it is released."""
    # 1. Nothing but the credential was added, and the file is still a valid image.
    _expected_new_findings(clean, signed, fmt)
    # 2. The picture is untouched by signing.
    try:
        a, b = canonical_frames(clean), canonical_frames(signed)
    except StripError as exc:
        raise CredentialError(f"The signed file cannot be decoded: {exc}") from None
    if len(a) != len(b) or any(
        x.shape != y.shape or x.dtype != y.dtype or not np.array_equal(x, y) for x, y in zip(a, b, strict=True)
    ):
        raise CredentialError("Signing changed the pixels.")
    # 3. Every byte of the manifest store is accounted for, and it is exactly what was asked for.
    try:
        store = jumbf.parse_store(jumbf.extract_store(signed, fmt))
    except jumbf.JumbfError as exc:
        raise CredentialError(f"The credential is malformed: {exc}") from None
    manifest, assertion = check_manifest_shape(store, fmt)
    if assertion != expected:
        raise CredentialError("The credential does not say what was requested.")
    if instance is not None:
        if manifest.assertions["c2pa.ingredient.v3"]["instanceID"] != instance:
            raise CredentialError("The credential's ingredient id is not the one that was requested.")
    # 4. The signature checks out, independently of the SDK, and uses our certificate chain.
    check_cose_signature(manifest, identity.chain)
    # 5. The SDK agrees: valid signature, hard binding intact, nothing but "untrusted" to complain about.
    credential = read_credential(signed, config.anchors)
    if credential.state not in ("Valid", "Trusted"):
        raise CredentialError(
            "The signed file did not validate: " + "; ".join(credential.problems or ["unknown reason"])
        )
    if credential.problems:
        raise CredentialError("The signed file validated with problems: " + "; ".join(credential.problems))
    if not credential.imgstrip:
        raise CredentialError("The signed file is not recognised as an imgstrip credential.")
    if evidence is not None:
        _check_actions(manifest, evidence, config)
    return credential


# --------------------------------------------------------------------------
# Reading credentials (any file)
# --------------------------------------------------------------------------


@dataclass
class Credential:
    """What a file's Content Credential says and how far it can be believed."""

    present: bool
    format: str | None = None
    state: str | None = None  # Invalid | Valid | Trusted (as the SDK reads it, with the anchors given)
    signer: str | None = None
    signer_name: str | None = None
    imgstrip: bool = False  # a credential of exactly the shape imgstrip writes
    sanitization: dict | None = None  # its assertion, when imgstrip is True
    pixels_only: bool = False  # nothing in the file but the picture and this one credential
    declared_ai: bool = False
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    other_findings: list[Finding] = field(default_factory=list)

    @property
    def label(self) -> str:
        """The wording a public viewer would use."""
        if not self.present:
            return "No Content Credentials"
        if self.state == "Trusted":
            return "Trusted"
        if self.state == "Valid":
            return "Unverified (signed, signer not trusted)"
        return "Invalid"

    def as_dict(self) -> dict:
        return {
            "present": self.present,
            "format": self.format,
            "state": self.state,
            "label": self.label,
            "signer": self.signer,
            "signer_name": self.signer_name,
            "imgstrip": self.imgstrip,
            "sanitization": self.sanitization,
            "pixels_only": self.pixels_only,
            "declared_ai": self.declared_ai,
            "problems": list(self.problems),
            "notes": list(self.notes),
            "other": [{"category": f.category, "label": f.label, "detail": f.detail} for f in self.other_findings],
        }


def _codes(results: dict | None) -> tuple[list[str], list[str]]:
    """(success codes, failure codes) of the active manifest."""
    active = (results or {}).get("activeManifest", {}) or {}
    ok = [s.get("code", "") for s in active.get("success", [])]
    bad = [s.get("code", "") for s in active.get("failure", [])]
    return ok, bad


def _declares_ai(manifest: dict) -> bool:
    text = json.dumps(manifest.get("assertions", []), default=str).lower()
    return any(t in text for t in _AI_SOURCE_TYPES)


def read_credential(data: bytes, anchors: bytes | None = None) -> Credential:
    """Read and judge the Content Credential of any image file, without touching the network.

    The result says what the SDK validated and, separately, whether the file is *only* a
    picture plus one credential of the exact shape imgstrip writes.
    """
    c2pa = _sdk()
    fmt = sniff_format(data)
    report = inspect_bytes(data)
    cred = Credential(present=False, format=fmt)
    creds = [f for f in report.findings if is_c2pa_finding(f)]
    if not creds:
        return cred
    cred.present = True
    cred.other_findings = [f for f in report.findings if not is_c2pa_finding(f)]
    mime = MIME.get(fmt or "", {"TIFF": "image/tiff"}.get(fmt or "", "image/jpeg"))
    try:
        with _context(anchors) as ctx, c2pa.Reader(mime, io.BytesIO(data), context=ctx) as reader:
            cred.state = reader.get_validation_state() or "Invalid"
            ok, bad = _codes(reader.get_validation_results())
            manifest = reader.get_active_manifest() or {}
    except c2pa.C2paError as exc:
        message = _sdk_message(exc)
        if "remote" in message.lower():
            cred.notes.append("The credential is stored on a remote server; imgstrip does not fetch it.")
        cred.problems.append(f"the credential could not be read ({message})")
        cred.state = "Invalid"
        return cred
    except Exception:  # noqa: BLE001 - the SDK's wrapper raises plain KeyError/TypeError on some damaged manifests
        cred.problems.append("the credential could not be read (it is damaged)")
        cred.state = "Invalid"
        return cred
    info = manifest.get("signature_info") or {}
    cred.signer = info.get("issuer")
    cred.signer_name = info.get("common_name")
    cred.declared_ai = _declares_ai(manifest)
    # "untrusted" is the expected verdict for any signer the viewer has no anchor for; every
    # other failure code is a real problem.
    cred.problems += [code for code in bad if code != "signingCredential.untrusted"]
    if cred.state == "Invalid" and not cred.problems:
        cred.problems = ["the signature is invalid"]
    if cred.state != "Invalid":
        hard = "assertion.bmffHash.match" if fmt in ("AVIF", "HEIF") else "assertion.dataHash.match"
        if hard not in ok:
            cred.problems.append("the credential is not bound to this image's data")
    # Strict byte accounting, and the imgstrip-shape check.
    try:
        store = jumbf.parse_store(jumbf.extract_store(data, fmt or ""))
        m, assertion = check_manifest_shape(store, fmt or "")
        check_cose_signature(m)
    except (jumbf.JumbfError, CredentialError) as exc:
        cred.notes.append(f"not an imgstrip credential: {exc}")
    else:
        cred.imgstrip = _assertion_ok(assertion) and not cred.problems
        if cred.imgstrip:
            cred.sanitization = assertion
        else:
            cred.notes.append("not an imgstrip credential: the sanitisation assertion is not in the expected form")
    cred.pixels_only = cred.imgstrip and not cred.other_findings and report.error is None
    return cred


_PSNR_OK = (int, float)


def _assertion_ok(a: object) -> bool:
    """Is this a well-formed ``org.imgstrip.sanitization`` assertion (and nothing more)?"""
    if not isinstance(a, dict):
        return False
    base = {"version", "statement", "sanitized", "date", "pixel_integrity", "scope"}
    optional = {"operations", "psnr_db", "source_declared_ai_generated", "source_commitment"}
    if not base <= set(a) or set(a) - base - optional:
        return False
    if a["version"] != ASSERTION_VERSION or a["sanitized"] is not True or a["scope"] != "processing":
        return False
    if a["pixel_integrity"] not in INTEGRITY or a["statement"] != statement_text(a["pixel_integrity"]):
        return False
    if not (isinstance(a["date"], str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}Z)?", a["date"])):
        return False
    if "operations" in a and not (
        isinstance(a["operations"], list) and all(isinstance(o, str) and o in OPERATIONS for o in a["operations"])
    ):
        return False
    if "psnr_db" in a and not (
        isinstance(a["psnr_db"], _PSNR_OK) and not isinstance(a["psnr_db"], bool) and 0 < a["psnr_db"] < 200
    ):
        return False
    if "source_declared_ai_generated" in a and a["source_declared_ai_generated"] is not True:
        return False
    if "source_commitment" in a:
        sc = a["source_commitment"]
        if not (
            isinstance(sc, dict)
            and set(sc) == {"alg", "value"}
            and sc["alg"] == COMMITMENT_ALG
            and isinstance(sc["value"], str)
        ):
            return False
        try:
            if len(base64.b64decode(sc["value"], validate=True)) != 32:
                return False
        except ValueError:
            return False
    return True


# --------------------------------------------------------------------------
# Receipts and audits: proving "unaltered from the file we received" later
# --------------------------------------------------------------------------


def make_receipt(
    evidence: Evidence,
    salt: bytes,
    commitment: str,
    signed: bytes,
    credential: Credential,
    now: dt.datetime,
    source_sha256: str | None = None,
) -> dict:
    """A private record that lets the signer open the commitment later.

    It holds the salt and the hash of the original, so it must be kept as secret as the
    original itself and never published.  Without it nobody can link the published image to
    the original through the credential (the commitment is a keyed hash).
    """
    return {
        "signed_sha256": hashlib.sha256(signed).hexdigest(),
        "source_sha256": source_sha256 or evidence.source_sha256,
        "source_pixels_sha256": evidence.source_pixels_sha256,
        "salt": base64.b64encode(salt).decode("ascii"),
        "commitment": commitment,
        "pixel_integrity": evidence.integrity,
        "created": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def receipts_document(receipts: list[dict]) -> dict:
    return {
        "imgstrip_receipts": RECEIPT_VERSION,
        "warning": "PRIVATE. This file lets its holder prove which original each signed image came from. Never publish it.",
        "receipts": receipts,
    }


@dataclass
class Audit:
    """The result of checking a signed image against the original and its receipt."""

    credential: Credential
    receipt_found: bool = False
    commitment_ok: bool | None = None  # the original is the file this credential was made from
    claimed: str | None = None
    measured: str | None = None  # identical | equivalent, from comparing the two files now
    consistent: bool | None = None  # the claim about the pixels is true
    psnr_db: float | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(
            self.credential.imgstrip
            and self.receipt_found
            and self.commitment_ok
            and self.consistent
            and self.credential.state in ("Valid", "Trusted")
        )


def audit(
    signed: bytes,
    original: bytes,
    receipts: dict | list[dict],
    anchors: bytes | None = None,
    max_pixels: int = DEFAULT_MAX_PIXELS,
) -> Audit:
    """Check a signed image against the original file and the signer's receipt.

    This confirms (1) the credential is intact, (2) the original is the file it was made
    from (using the secret salt in the receipt), and (3) whether its claim about the pixels
    (identical, equivalent, altered) is true, by comparing the two files afresh.
    """
    credential = read_credential(signed, anchors)
    result = Audit(credential)
    if not credential.imgstrip:
        result.problems.append("the file has no valid imgstrip credential")
        return result
    items = receipts.get("receipts", []) if isinstance(receipts, dict) else receipts
    digest = hashlib.sha256(signed).hexdigest()
    mine = [r for r in items if isinstance(r, dict) and r.get("signed_sha256") == digest]
    result.claimed = credential.sanitization["pixel_integrity"]
    commit = credential.sanitization.get("source_commitment")
    if commit is None:
        result.problems.append("the credential holds no source commitment, so it cannot be audited")
        return result
    if not mine:
        result.problems.append("no receipt matches this signed file")
        return result
    result.receipt_found = True
    receipt = mine[0]
    want = canonical_frames(original, max_pixels)
    pixels = _digest(want)
    try:
        salt = base64.b64decode(receipt["salt"], validate=True)
        result.commitment_ok = hmac.compare_digest(_commitment(salt, pixels), commit["value"])
    except (KeyError, TypeError, ValueError):
        result.problems.append("the receipt is malformed")
        return result
    if not result.commitment_ok:
        result.problems.append("this is not the original the credential was made from")
        return result
    got = canonical_frames(signed, max_pixels)
    same = len(want) == len(got) and all(
        x.shape == y.shape and x.dtype == y.dtype and np.array_equal(x, y) for x, y in zip(want, got, strict=True)
    )
    result.measured = "identical" if same else "equivalent"
    result.psnr_db = None if same else _psnr(want, got)
    # Only the strongest claim can be false: "equivalent" and "altered" promise nothing about identity.
    result.consistent = same if result.claimed == "identical" else True
    if not result.consistent:
        result.problems.append("the credential says the pixels are identical, but the two files differ")
    return result
