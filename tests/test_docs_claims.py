"""What the documentation and the interface claim must match what was measured.

Finding F4 of the review: the documentation said in one line that re-encoding "destroys" JPEG-domain
steganography and only later that this is "only partly true".  A reader who stops at the first line draws the
wrong conclusion about the default mode, so no sentence may claim it.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXTS = [
    ROOT / "docs" / "README.template.md",
    ROOT / "docs" / "REFERENCE.md",
    ROOT / "docs" / "DCT_WORKFLOW.md",
    ROOT / "PRODUCT.md",
    *sorted((ROOT / "src" / "imgstrip").rglob("*.py")),
    *sorted((ROOT / "src" / "imgstrip" / "web" / "static").glob("*.html")),
    *sorted((ROOT / "src" / "imgstrip" / "web" / "static").glob("*.js")),
]
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")
_REENCODE_DESTROYS = re.compile(r"re-?encod\w*[^.]*\b(destroy|remove|clear|erase)s?\b[^.]*(steg|dct|coefficient)", re.I)
_NEGATED = re.compile(r"n't\b|\b(not|only|partly|degrades?|cannot|never)\b", re.I)


def _claims(sentence: str) -> bool:
    """True when the sentence says re-encoding removes DCT steganography, with no qualifier before the verb."""
    match = _REENCODE_DESTROYS.search(sentence)
    return bool(match) and not _NEGATED.search(sentence[match.start() : match.start(1)])


def test_no_sentence_claims_that_re_encoding_removes_dct_steganography():
    bad = []
    for path in TEXTS:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for sentence in _SENTENCE.findall(line):
                if _claims(sentence):
                    bad.append(f"{path.relative_to(ROOT)}:{lineno}: {sentence.strip()[:160]}")
    assert not bad, "\n".join(bad)


def test_the_checker_itself_catches_the_old_wording():
    assert _claims('Re-encoding also destroys JPEG-domain steganography and the quantisation-table "fingerprint".')
    assert _claims("The default re-encodes JPEGs, which also destroys DCT-domain steganography, so it is not on.")
    assert not _claims("Re-encoding only degrades JPEG-domain steganography, it does not remove it.")
    assert not _claims("Re-encoding doesn't reliably clear it either: tools that change zero coefficients survive.")
