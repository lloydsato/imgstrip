"""What imgstrip can honestly say about data hidden in the pixel values.

Steganalysis is statistical and covers one family of methods, so a scan can only
ever *find* something; it can never show that nothing is there.  Every consumer
(command line, JSON, web app, credentials) therefore uses the same four states,
and the wording lives here so the surfaces cannot drift apart:

``detected``      a pixel test flagged likely or possible LSB steganography.
``scrubbed``      the least-significant bit of every colour sample was randomised
                  while cleaning, which destroys LSB-replacement payloads.
``not_detected``  the pixel tests ran and found nothing.  Not proof of absence.
``not_scanned``   nothing was tested (not requested, or the format has no pixel
                  values the test can read).

This module has no dependencies on the rest of the package so that the cleaning
engine can report what it actually did without importing the analysis code.
"""

from __future__ import annotations

from dataclasses import dataclass

DETECTED = "detected"
SCRUBBED = "scrubbed"
NOT_DETECTED = "not_detected"
NOT_SCANNED = "not_scanned"
STATES = (DETECTED, SCRUBBED, NOT_DETECTED, NOT_SCANNED)

# What the engine did about --scrub-lsb (Result.lsb_scrub).
SCRUB_OFF = "off"  # not requested
SCRUB_APPLIED = "applied"  # every frame was scrubbed
SCRUB_PARTIAL = "partial"  # some frames or image modes could not be scrubbed
SCRUB_NOT_APPLIED = "not_applied"  # requested, but nothing could be scrubbed

# What the engine did about --scrub-dct (Result.dct_scrub): data hidden in a JPEG's quantised DCT coefficients,
# a different place from the pixel values the four states above talk about.
DCT_OFF = "off"  # not requested
DCT_CANONICAL = "canonical"  # coefficients kept, everything around them rebuilt in one canonical form
DCT_SCRUBBED = "scrubbed"  # coefficient parities randomised, +-1 coefficients removed

LIKELY = "likely"
POSSIBLE = "possible"

_HEADLINES = {
    DETECTED: "detected",
    SCRUBBED: "scrubbed",
    NOT_DETECTED: "not detected",
    NOT_SCANNED: "not scanned",
}

_CAVEATS = {
    DETECTED: (
        "This is statistical evidence, not a decoded message, and it can be a false alarm. "
        "A file whose bits were randomised by LSB scrubbing reads the same way."
    ),
    SCRUBBED: (
        "This destroys LSB-replacement payloads. It does not cover other ways of hiding data, "
        "so it is not proof that nothing is hidden."
    ),
    NOT_DETECTED: (
        "This is not proof that nothing is hidden: small payloads and methods other than "
        "LSB replacement can go undetected."
    ),
    NOT_SCANNED: "Nothing was tested, so nothing can be said about data hidden in the pixel values.",
}


@dataclass(frozen=True)
class Verdict:
    state: str
    level: str | None = None  # detected only: LIKELY or POSSIBLE
    detail: str = ""  # the evidence, the reason, or the coverage, in one clause
    complete: bool = True  # False: only part of the image (some frames or modes) was covered
    source: str | None = None  # scrubbed only: what scanning the original found (a state), if it was scanned

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"unknown steganography state {self.state!r}")

    @property
    def headline(self) -> str:
        """A short label: 'detected (likely)', 'scrubbed', 'not detected', 'not scanned'."""
        text = _HEADLINES[self.state]
        if self.state == DETECTED and self.level:
            return f"{text} ({self.level})"
        if self.state == SCRUBBED and not self.complete:
            return "partly scrubbed"
        return text

    @property
    def summary(self) -> str:
        """One sentence saying what was found or done, without the caveat."""
        detail = f" ({self.detail})" if self.detail else ""
        if self.state == DETECTED:
            return f"pixel statistics indicate LSB steganography{detail}"
        if self.state == SCRUBBED:
            text = "the least-significant bit of every colour sample was randomised"
            if not self.complete:
                text = "the least-significant bit was randomised where the image allowed it"
            if self.source == DETECTED:
                text += "; the original was flagged for LSB steganography"
            return text + detail
        if self.state == NOT_DETECTED:
            return f"the LSB tests found nothing{detail}"
        return self.detail or "the pixels were not scanned"

    @property
    def caveat(self) -> str:
        return _CAVEATS[self.state]

    @property
    def payload_kept(self) -> bool:
        """A payload was flagged and nothing scrubbed it: cleaning kept it."""
        return self.state == DETECTED

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "level": self.level,
            "headline": self.headline,
            "summary": self.summary,
            "caveat": self.caveat,
            "complete": self.complete,
            "source": self.source,
        }


def not_scanned(reason: str) -> Verdict:
    return Verdict(NOT_SCANNED, detail=reason)


def for_output(scan: Verdict | None, lsb_scrub: str, copied: bool) -> Verdict:
    """The verdict for a cleaned file, from what scanning the original found and what the engine did.

    ``scan`` is the verdict on the original's pixels (None when it was not scanned).  ``lsb_scrub`` is
    what the engine reports it did (``Result.lsb_scrub``), never what was merely requested.  ``copied`` is
    true when the pixels were stored losslessly, false after lossy re-encoding.
    """
    if lsb_scrub in (SCRUB_APPLIED, SCRUB_PARTIAL):
        source = scan.state if scan is not None and scan.state in (DETECTED, NOT_DETECTED) else None
        return Verdict(SCRUBBED, complete=lsb_scrub == SCRUB_APPLIED, source=source)
    if scan is not None and scan.state in (DETECTED, NOT_DETECTED):
        return scan
    reason = "the pixels were not scanned"
    if scan is not None and scan.detail:
        reason = scan.detail
    if lsb_scrub == SCRUB_NOT_APPLIED:
        reason += "; LSB scrubbing was requested but could not be applied to this image"
    elif copied:
        reason += "; the pixel values are stored losslessly, so data hidden in them can survive (LSB scrubbing destroys LSB payloads)"
    else:
        reason += "; the picture was re-encoded, which is not a guarantee that data hidden in its pixels is gone"
    return not_scanned(reason)


_DCT_HEADLINES = {DCT_CANONICAL: "canonicalised", DCT_SCRUBBED: "scrubbed"}

_DCT_CAVEATS = {
    DCT_CANONICAL: (
        "This removes data hidden in how the file is encoded (table layouts, unused tables, padding, restart "
        "markers, blocks outside the picture). The coefficients themselves are kept, so steganography stored in "
        "them (JSteg, F5, steghide, J-UNIWARD and similar) survives. Use --scrub-dct to destroy it."
    ),
    DCT_SCRUBBED: (
        "This destroys payloads hidden in the parity of the DCT coefficients (JSteg, JPHide, OutGuess, steghide, "
        "F5, nsF5, J-UNIWARD and similar). It does not cover watermarks built to survive re-saving or data carried "
        "by the picture itself, so it is not proof that nothing is hidden."
    ),
}


def dct_report(state: str, psnr: float | None = None) -> dict | None:
    """What cleaning did about data hidden in a JPEG's DCT coefficients; None when --scrub-dct was off."""
    if state not in _DCT_HEADLINES:
        return None
    if state == DCT_SCRUBBED:
        summary = "the parity of every quantised DCT coefficient was randomised and the smallest coefficients (-2 to 1) removed"
        if psnr is not None:
            summary += f" (estimated {psnr} dB against the input)"
    else:
        summary = "the compressed data was rebuilt from its DCT coefficients in a canonical form; the pixels are unchanged"
    return {"state": state, "headline": _DCT_HEADLINES[state], "summary": summary, "caveat": _DCT_CAVEATS[state], "psnr_db": psnr}
