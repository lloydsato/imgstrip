"""Helpers for the Content Credentials tests: certificate factories and a raw-SDK signer.

The raw signer builds credentials *without* imgstrip's own manifest rules, so tests can create
the foreign and hostile credentials that imgstrip must recognise, report or refuse.
"""

from __future__ import annotations

import datetime as dt
import io

import numpy as np
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from imgstrip import credentials as cr
from imgstrip import fixtures

NOW = dt.datetime.now(dt.timezone.utc)
MIME = cr.MIME


def _name(cn: str, org: str | None = "Test Newsroom", ou: str | None = None) -> x509.Name:
    attrs = []
    if org:
        attrs.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, org))
    if ou:
        attrs.append(x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, ou))
    attrs.append(x509.NameAttribute(NameOID.COMMON_NAME, cn))
    return x509.Name(attrs)


def _usage(**kw) -> x509.KeyUsage:
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
    return x509.KeyUsage(**base)


def hash_for(key):
    if isinstance(key, ec.EllipticCurvePrivateKey):
        return {256: hashes.SHA256, 384: hashes.SHA384, 521: hashes.SHA512}[key.curve.key_size]()
    return None if isinstance(key, ed25519.Ed25519PrivateKey) else hashes.SHA256()


def sign_cert(builder, key):
    h = hash_for(key)
    return builder.sign(key, h)


class Pki:
    """A private CA and a way to issue signing certificates from it."""

    def __init__(self, org: str = "Test CA Org", ou: str | None = None):
        self.key = ec.generate_private_key(ec.SECP256R1())
        name = _name(f"{org} root", org, ou)
        self.cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(NOW - dt.timedelta(days=2))
            .not_valid_after(NOW + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(_usage(key_cert_sign=True, crl_sign=True), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()), critical=False)
            .sign(self.key, hashes.SHA256())
        )

    @property
    def root_pem(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.PEM)

    def issue(
        self,
        key=None,
        *,
        org: str | None = "Test Newsroom",
        cn: str = "Jane Reporter",
        ou: str | None = None,
        days: int = 365,
        not_before: dt.datetime | None = None,
        not_after: dt.datetime | None = None,
        eku: list | None = (ExtendedKeyUsageOID.EMAIL_PROTECTION,),
        usage: x509.KeyUsage | None = None,
        ca: bool = False,
    ) -> cr.Identity:
        key = key or ec.generate_private_key(ec.SECP256R1())
        b = (
            x509.CertificateBuilder()
            .subject_name(_name(cn, org, ou))
            .issuer_name(self.cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before or NOW - dt.timedelta(minutes=5))
            .not_valid_after(not_after or NOW + dt.timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
            .add_extension(
                usage or (_usage(key_cert_sign=True) if ca else _usage(digital_signature=True)), critical=True
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), critical=False)
        )
        if eku:
            b = b.add_extension(x509.ExtendedKeyUsage(list(eku)), critical=False)
        leaf = b.sign(self.key, hashes.SHA256())
        return cr.Identity([leaf, self.cert], key)

    def issue_with_certs(self, **kw):
        ident = self.issue(**kw)
        return ident, [ident.leaf, self.cert]


def self_signed(key=None, days: int = 30) -> cr.Identity:
    key = key or ec.generate_private_key(ec.SECP256R1())
    name = _name("self signed")
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - dt.timedelta(days=1))
        .not_valid_after(NOW + dt.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_usage(digital_signature=True), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION]), critical=False)
        .sign(key, hash_for(key))
    )
    return cr.Identity([cert], key)


def pem_chain(identity: cr.Identity, *extra) -> bytes:
    certs = [identity.leaf, *extra]
    return b"".join(c.public_bytes(serialization.Encoding.PEM) for c in certs)


def key_pem(identity: cr.Identity, passphrase: bytes | None = None) -> bytes:
    enc = serialization.BestAvailableEncryption(passphrase) if passphrase else serialization.NoEncryption()
    return identity._key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc)


def key_der(identity: cr.Identity) -> bytes:
    return identity._key.private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


KEYGENS = {
    "ES256": lambda: ec.generate_private_key(ec.SECP256R1()),
    "ES384": lambda: ec.generate_private_key(ec.SECP384R1()),
    "ES512": lambda: ec.generate_private_key(ec.SECP521R1()),
    "PS256": lambda: rsa.generate_private_key(65537, 2048),
    "ED25519": lambda: ed25519.Ed25519PrivateKey.generate(),
}


# --------------------------------------------------------------------------
# Raw SDK signing: any manifest, no imgstrip rules
# --------------------------------------------------------------------------


def raw_sign(
    identity: cr.Identity,
    data: bytes,
    fmt: str,
    definition: dict,
    *,
    intent=None,
    actions=(),
    ingredient_stream: bytes | None = None,
    thumbnails: bool = False,
    verify_after_sign: bool = False,
) -> bytes:
    """Sign ``data`` with whatever manifest ``definition`` describes."""
    import c2pa

    settings = cr.sdk_settings()
    settings["builder"]["thumbnail"]["enabled"] = thumbnails
    settings["verify"]["verify_after_sign"] = verify_after_sign
    ctx = c2pa.Context(settings=c2pa.Settings.from_dict(settings))
    try:
        with c2pa.Signer.from_callback(
            identity.sign, getattr(c2pa.C2paSigningAlg, identity.alg), identity.chain_pem()
        ) as signer:
            with c2pa.Builder(definition, ctx) as builder:
                if intent is not None:
                    builder.set_intent(*intent)
                for a in actions:
                    builder.add_action(a)
                if ingredient_stream is not None:
                    builder.add_ingredient(
                        {"title": "x", "relationship": "parentOf"}, MIME[fmt], io.BytesIO(ingredient_stream)
                    )
                out = io.BytesIO()
                builder.sign(signer, MIME[fmt], io.BytesIO(data), out)
                return out.getvalue()
    finally:
        ctx.close()


def foreign_credential(
    identity: cr.Identity, data: bytes, fmt: str, *, ai: bool = False, title: str = "camera shot"
) -> bytes:
    """A camera-style credential: a created action, GPS-ish metadata, optionally 'AI generated'."""
    import c2pa

    definition = {
        "claim_generator_info": [{"name": "Camera Firmware", "version": "9.9"}],
        "title": title,
        "assertions": [
            {
                "label": "c2pa.metadata",
                "data": {"@context": {"exif": "http://ns.adobe.com/exif/1.0/"}, "exif:GPSLatitude": "48.8584"},
            },
        ],
    }
    source = c2pa.C2paDigitalSourceType.TRAINED_ALGORITHMIC_MEDIA if ai else c2pa.C2paDigitalSourceType.DIGITAL_CAPTURE
    return raw_sign(identity, data, fmt, definition, intent=(c2pa.C2paBuilderIntent.CREATE, source))


def imgstrip_definition(fmt: str, assertion: dict, **overrides) -> dict:
    """The manifest imgstrip itself writes, for tests that vary one thing at a time."""
    d = cr._manifest_definition(fmt, assertion, None)
    d.update(overrides)
    return d


def edit_intent():
    import c2pa

    return (c2pa.C2paBuilderIntent.EDIT,)


# --------------------------------------------------------------------------
# Pictures
# --------------------------------------------------------------------------


def smooth(width: int = 96, height: int = 72) -> np.ndarray:
    yy, xx = np.mgrid[0:height, 0:width]
    # no wrap-around: lossy encoders (and imgstrip's own tolerance check) need a photographic, smooth picture
    return np.stack([xx * 2, yy * 3, xx + yy + 20], -1).clip(0, 255).astype(np.uint8)


def photo_bytes(fmt: str = "PNG", **kw) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(smooth()).save(buf, fmt, **kw)
    return buf.getvalue()


def sensor_photo(width: int = 160, height: int = 120) -> np.ndarray:
    return fixtures.photo(width, height)


def assert_one_credential_and_nothing_else(data: bytes) -> None:
    from imgstrip.inspect import inspect_bytes, is_c2pa_finding

    report = inspect_bytes(data)
    assert report.error is None, report.error
    creds = [f for f in report.findings if is_c2pa_finding(f)]
    others = [f for f in report.findings if not is_c2pa_finding(f)]
    assert len(creds) == 1 and not others, report.findings
