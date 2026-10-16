"""Signing identities: loading, key handling, every key type, and what the assessment tells a user."""

from __future__ import annotations

import copy
import datetime as dt
import pickle

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID

from c2pa_support import KEYGENS, NOW, Pki, key_der, key_pem, pem_chain, photo_bytes, self_signed
from imgstrip import jumbf

# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def test_pem_certificate_and_key_in_separate_files(creds, plain_signer, pki):
    ident = creds.load_pem_identity(pem_chain(plain_signer), key_pem(plain_signer))
    assert ident.display_name == "Test Newsroom" and ident.common_name == "Jane Reporter" and ident.alg == "ES256"
    assert ident.leaf == plain_signer.leaf


def test_key_in_the_certificate_file(creds, plain_signer):
    both = pem_chain(plain_signer) + key_pem(plain_signer)
    assert creds.load_identity(both).leaf == plain_signer.leaf


def test_missing_key_is_explained(creds, plain_signer):
    with pytest.raises(creds.CredentialError, match="No private key was given"):
        creds.load_identity(pem_chain(plain_signer))


def test_key_that_belongs_to_another_certificate(creds, plain_signer, pki):
    other = pki.issue()
    with pytest.raises(creds.CredentialError, match="does not belong"):
        creds.load_identity(pem_chain(plain_signer), key_pem(other))


@pytest.mark.parametrize(
    "cert, key",
    [
        (b"", None),
        (b"hello", b"world"),
        (b"-----BEGIN CERTIFICATE-----\nnope\n-----END CERTIFICATE-----\n", None),
        (b"\x00" * 64, None),
    ],
)
def test_garbage_is_a_clear_error_never_a_crash(creds, plain_signer, cert, key):
    with pytest.raises(creds.CredentialError):
        creds.load_identity(cert, key)
    with pytest.raises(creds.CredentialError):
        creds.load_identity(
            pem_chain(plain_signer), b"-----BEGIN PRIVATE KEY-----\nbm9wZQ==\n-----END PRIVATE KEY-----\n"
        )


def test_encrypted_keys_and_passphrases(creds, plain_signer):
    chain, enc = pem_chain(plain_signer), key_pem(plain_signer, b"correct horse")
    with pytest.raises(creds.PassphraseRequired, match="encrypted"):
        creds.load_identity(chain, enc)
    with pytest.raises(creds.PassphraseRequired, match="wrong"):
        creds.load_identity(chain, enc, b"battery staple")
    assert creds.load_identity(chain, enc, b"correct horse").leaf == plain_signer.leaf
    with pytest.raises(creds.CredentialError, match="not encrypted") as exc:
        creds.load_identity(chain, key_pem(plain_signer), b"unneeded")
    assert not isinstance(exc.value, creds.PassphraseRequired)


def test_pkcs12_bundles(creds, plain_signer, pki):
    bundle = pkcs12.serialize_key_and_certificates(
        b"id", plain_signer._key, plain_signer.leaf, [pki.cert], serialization.BestAvailableEncryption(b"pw")
    )
    assert creds.load_identity(bundle, None, b"pw").leaf == plain_signer.leaf
    with pytest.raises(creds.PassphraseRequired):
        creds.load_identity(bundle)
    with pytest.raises(creds.PassphraseRequired):
        creds.load_identity(bundle, None, b"nope")
    plain = pkcs12.serialize_key_and_certificates(
        b"id", plain_signer._key, plain_signer.leaf, [pki.cert], serialization.NoEncryption()
    )
    assert creds.load_identity(plain).alg == "ES256"
    keyless = pkcs12.serialize_key_and_certificates(b"id", None, plain_signer.leaf, [], serialization.NoEncryption())
    with pytest.raises(creds.CredentialError, match="private key"):
        creds.load_identity(keyless)
    # A file that is not a bundle at all must not send the user looking for a passphrase.
    with pytest.raises(creds.CredentialError, match="could not be read") as err:
        creds.load_identity(b"this is not a certificate")
    assert not isinstance(err.value, creds.PassphraseRequired)


def test_chain_is_ordered_and_the_root_is_left_out(creds, pki):
    ident = pki.issue()
    other = Pki("Other CA").issue()  # an unrelated identity's certificate in the same file
    shuffled = [pki.cert, other.leaf, ident.leaf]
    pem = b"".join(c.public_bytes(serialization.Encoding.PEM) for c in shuffled)
    loaded = creds.load_pem_identity(pem, key_pem(ident))
    assert loaded.leaf == ident.leaf
    assert loaded.root == pki.cert
    assert loaded.chain == [ident.leaf], "the self-signed root is the verifier's to supply"
    assert loaded.ignored_certificates == 1
    assert loaded.chain_pem().count("BEGIN CERTIFICATE") == 1


def test_intermediate_certificates_stay_in_the_chain(creds):
    root = Pki("Root Org")
    inter_key = ec.generate_private_key(ec.SECP256R1())
    inter = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "Intermediate")]))
        .issuer_name(root.cert.subject)
        .public_key(inter_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - dt.timedelta(days=1))
        .not_valid_after(NOW + dt.timedelta(days=1000))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(root.key, hashes.SHA256())
    )
    inter_pki = Pki.__new__(Pki)
    inter_pki.key, inter_pki.cert = inter_key, inter
    ident = inter_pki.issue()
    certs = [ident.leaf, inter, root.cert]
    loaded = creds.Identity(certs, ident._key)
    assert loaded.chain == [ident.leaf, inter] and loaded.root == root.cert


def test_trust_anchor_files_are_validated(creds, pki):
    assert b"BEGIN CERTIFICATE" in creds.load_anchors(pki.root_pem + b"\nnoise\n")
    with pytest.raises(creds.CredentialError):
        creds.load_anchors(b"not a certificate")


# --------------------------------------------------------------------------
# The key never leaves the identity
# --------------------------------------------------------------------------


def test_the_key_is_not_in_repr_str_or_pickles(creds, plain_signer):
    der = key_der(plain_signer)
    numbers = plain_signer._key.private_numbers().private_value.to_bytes(32, "big")
    for text in (repr(plain_signer), str(plain_signer), f"{plain_signer!r}"):
        assert "PRIVATE" not in text and "Test Newsroom" in text
    assert der not in repr(plain_signer).encode() and numbers.hex() not in repr(plain_signer)
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(plain_signer)
    with pytest.raises(TypeError):
        copy.copy(plain_signer)
    with pytest.raises(TypeError):
        copy.deepcopy(plain_signer)


def test_signing_errors_never_carry_key_material(creds, plain_signer, monkeypatch):
    der = key_der(plain_signer)

    def explode(data):
        raise RuntimeError("boom " + der.hex())

    monkeypatch.setattr(type(plain_signer), "sign", lambda self, data: explode(data))
    from imgstrip.core import Options, strip_image

    source = photo_bytes("PNG")
    result = strip_image(source, Options())
    evidence = creds.gather_evidence(source, result, Options())
    with pytest.raises(creds.CredentialError) as exc:
        creds.sign(result.data, evidence, plain_signer)
    assert der.hex() not in str(exc.value) and "boom" not in str(exc.value)


# --------------------------------------------------------------------------
# Every supported key type signs, and the result verifies without the SDK
# --------------------------------------------------------------------------


@pytest.mark.parametrize("alg", list(KEYGENS))
def test_each_key_type_signs_and_is_verified_independently(creds, pki, alg):
    ident = pki.issue(KEYGENS[alg]())
    assert ident.alg == alg
    from imgstrip.core import Options, strip_image

    source = photo_bytes("PNG")
    result = strip_image(source, Options())
    evidence = creds.gather_evidence(source, result, Options())
    signed = creds.sign(result.data, evidence, ident)
    manifest = jumbf.parse_store(jumbf.extract_store(signed.data, "PNG")).manifests[0]
    leaf = creds.check_cose_signature(manifest, ident.chain)  # cryptography only
    assert leaf == ident.leaf
    assert signed.credential.state in ("Valid", "Trusted") and signed.credential.imgstrip


def test_unsupported_keys_are_refused_with_the_reason(creds, pki):
    weak = rsa.generate_private_key(65537, 1024)
    with pytest.raises(creds.CredentialError, match="1024 bits"):
        pki.issue(weak)
    odd = ec.generate_private_key(ec.SECP256K1())
    with pytest.raises(creds.CredentialError, match="secp256k1"):
        pki.issue(odd)
    from cryptography.hazmat.primitives.asymmetric import ed448

    with pytest.raises(creds.CredentialError, match="Unsupported key type"):
        creds.Identity([pki.issue().leaf], ed448.Ed448PrivateKey.generate())


# --------------------------------------------------------------------------
# The test identity
# --------------------------------------------------------------------------


def test_test_identity_is_a_marked_two_step_chain(creds, signer):
    ident = signer.identity
    assert ident.is_test and not ident.self_signed
    assert ident.root is not None and ident.chain == [ident.leaf]
    assert signer.root_pem.startswith(b"-----BEGIN CERTIFICATE-----")
    assert creds.TEST_MARKER in ident.leaf.subject.rfc4514_string()
    # the files a user would be given load back into an equivalent identity
    again = creds.load_identity(signer.chain_pem + signer.root_pem, signer.key_pem)
    assert again.leaf == ident.leaf and again.is_test


def test_test_identity_can_be_passphrase_protected_and_is_bounded(creds):
    t = creds.make_test_identity("Org", "Name", passphrase=b"pw")
    with pytest.raises(creds.PassphraseRequired):
        creds.load_identity(t.chain_pem + t.root_pem, t.key_pem)
    assert creds.load_identity(t.chain_pem + t.root_pem, t.key_pem, b"pw").is_test
    for days in (0, 99999):
        with pytest.raises(creds.CredentialError):
            creds.make_test_identity(days=days)


def test_test_identities_are_unique(creds):
    a, b = creds.make_test_identity(), creds.make_test_identity()
    assert a.identity.leaf != b.identity.leaf and a.root_pem != b.root_pem


# --------------------------------------------------------------------------
# Assessment: what a public viewer will make of it
# --------------------------------------------------------------------------


def codes(assessment):
    return {n.code: n.severity for n in assessment.notices}


def test_test_identity_gets_the_yellow_unverified_warning(creds, signer):
    a = creds.assess(signer.identity)
    assert a.state == "Valid" and a.trust == "unverified" and a.can_sign and a.test
    notice = next(n for n in a.notices if n.code == "test_identity")
    assert notice.severity == "warning"
    for phrase in ("test/development certificate", "'Unverified' warning", "C2PA Trust List"):
        assert phrase in notice.message
    assert codes(a)["no_timestamp"] == "info"


def test_trust_anchors_make_a_chain_trusted(creds, signer):
    a = creds.assess(signer.identity, signer.root_pem)
    assert a.state == "Trusted" and a.trust == "trusted"
    assert codes(a)["trusted"] == "info"
    assert "untrusted" not in codes(a) and "trust_unknown" not in codes(a)
    assert codes(a)["test_identity"] == "warning", "a viewer without your anchors still sees Unverified"


def test_an_ordinary_identity_without_anchors_is_unknown_not_alarming(creds, plain_signer):
    a = creds.assess(plain_signer)
    assert not a.test and a.state == "Valid"
    assert codes(a)["trust_unknown"] == "info"
    assert "test_identity" not in codes(a) and a.can_sign
    assert a.signer == "Test Newsroom" and a.issuer == "Test CA Org"
    assert a.key == "ECDSA P-256" and a.algorithm == "ES256" and a.fingerprint.count(":") == 31


def test_anchors_that_do_not_cover_the_chain_warn(creds, plain_signer):
    a = creds.assess(plain_signer, Pki("Someone else").root_pem)
    assert a.trust == "unverified" and codes(a)["untrusted"] == "warning"
    assert creds.assess(plain_signer, plain_signer.root.public_bytes(serialization.Encoding.PEM)).trust == "trusted"


def test_self_signed_certificates_are_refused_with_advice(creds):
    a = creds.assess(self_signed())
    assert not a.can_sign and codes(a)["self_signed"] == "error"
    message = next(n.message for n in a.notices if n.code == "self_signed")
    assert "refuses to sign" in message and "test identity" in message


def test_expired_and_future_certificates_are_refused(creds, pki):
    expired = pki.issue(not_before=NOW - dt.timedelta(days=400), not_after=NOW - dt.timedelta(days=1))
    a = creds.assess(expired)
    assert not a.can_sign and codes(a)["expired"] == "error" and "expired on" in a.notices[0].message
    future = pki.issue(not_before=NOW + dt.timedelta(days=3), not_after=NOW + dt.timedelta(days=300))
    a = creds.assess(future)
    assert not a.can_sign and codes(a)["not_yet_valid"] == "error"


def test_certificates_about_to_expire_warn_and_say_what_happens(creds, pki):
    soon = pki.issue(not_after=NOW + dt.timedelta(days=9))
    a = creds.assess(soon)
    assert a.can_sign and codes(a)["expires_soon"] == "warning"
    assert "show every credential signed with it as expired" in next(
        n.message for n in a.notices if n.code == "expires_soon"
    )
    # the same certificate, judged at a later date, has expired
    later = creds.assess(soon, now=NOW + dt.timedelta(days=10), probe=False)
    assert not later.can_sign and codes(later)["expired"] == "error"


def test_the_missing_timestamp_is_always_explained(creds, plain_signer):
    msg = next(n.message for n in creds.assess(plain_signer).notices if n.code == "no_timestamp")
    assert "never contacts the internet" in msg and "invalid once this certificate expires" in msg


def test_certificate_authority_certificates_cannot_sign(creds, pki):
    ca_as_leaf = pki.issue(ca=True)
    a = creds.assess(ca_as_leaf, probe=False)
    assert not a.can_sign and codes(a)["ca_certificate"] == "error"


def test_certificates_that_may_not_make_signatures_are_refused(creds, pki):
    from c2pa_support import _usage

    no_sig = pki.issue(usage=_usage(key_encipherment=True))
    a = creds.assess(no_sig, probe=False)
    assert not a.can_sign and codes(a)["key_usage"] == "error"


def test_the_sdks_own_rejection_is_reported_in_plain_words(creds, pki):
    """The probe signs a throw-away image, so a profile the SDK rejects is found before any real file."""
    no_eku = pki.issue(eku=[ExtendedKeyUsageOID.CODE_SIGNING])
    a = creds.assess(no_eku)
    assert not a.can_sign
    assert "extended key usage" in codes_message(a, "rejected") or "rejected" in codes_message(a, "rejected").lower()


def codes_message(assessment, code):
    return next(n.message for n in assessment.notices if n.code == code)


def test_assessment_serialises_without_secrets(creds, signer):
    d = creds.assess(signer.identity).as_dict()
    import json

    text = json.dumps(d)
    assert "PRIVATE" not in text and key_der(signer.identity).hex() not in text
    assert d["signer"] == "Test Newsroom" and d["can_sign"] is True
    assert {"notices", "fingerprint", "valid_until", "key", "trust", "test"} <= set(d)


def test_notices_are_ordered_errors_then_warnings_then_info(creds, pki):
    a = creds.assess(pki.issue(not_after=NOW + dt.timedelta(days=5), ou=creds.TEST_MARKER))
    order = [n.severity for n in a.notices]
    assert order == sorted(order, key={"error": 0, "warning": 1, "info": 2}.get)
