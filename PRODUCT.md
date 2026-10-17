# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Local web app with no new dependencies: a Python standard-library HTTP server (inside the existing `imgstrip` package) serving plain HTML, CSS and JavaScript. No build step, no Node, works offline. Launched with one command (`imgstrip-web`). The browser talks only to the server on the user's own machine; images never leave it.

The rest of the codebase is the existing Python engine (`src/imgstrip/`: Pillow, numpy, optional pillow-heif), which the web app calls directly.

## Users

Journalists and privacy-focused individuals: reporters, editors and researchers handling photos from sources, activists, and people who care where their pictures end up. They are about to publish, send or hand over an image and need to know, precisely, what the file gives away and that the copy they share gives away nothing. Many know what EXIF is; all of them want certainty over convenience.

The default view stays simple: drop, see what each file reveals, clean, take the clean files. The full per-item report, the deep pixel scan and every processing option are one step away, never in the way.

## Product Purpose

imgstrip removes all metadata and hidden data from images and leaves only the picture. The web app gives the existing command-line engine a local, visual front end: show what a file hides, clean it, prove the result is clean, and hand the cleaned file back. Success is a user who leaves with a file that holds nothing but pixels, and who understands what was removed.

## Positioning

Most metadata removers delete the tags they recognise and copy everything else. imgstrip decodes the image to raw pixels and writes a brand-new file from those pixels alone; by default nothing from the original is copied except pixel values (the opt-in JPEG modes keep the picture's compressed data, as the README explains). Every output is checked twice before it is released: a strict, format-aware parser must account for every byte, and the new file is decoded again and compared with the intended pixels (bit-exact for lossless formats). If either check fails, nothing is written. It fails closed.

## Operating Context

- Runs on the user's own computer; opened in their browser at a loopback address. No account, no network service, no upload to anyone.
- Files arrive by drag and drop or a file picker, singly or in batches. They are scanned on arrival; nothing is cleaned until the user asks.
- Cleaned files come back two ways: a browser download (one file, or all of them as a ZIP), or written straight into a chosen folder on disk.
- Originals are never modified.

## Capabilities and Constraints

- **Inspect** a file without changing it: the container's metadata and hidden data, grouped as metadata, embedded images and hidden payloads. Optional **deep scan** decodes the pixels and looks for colour under transparent pixels, palette tricks and LSB steganography (two detectors must agree).
- **Clean** in batches, with options: output format (same as input, JPEG, PNG, WebP, GIF, TIFF, BMP, AVIF, HEIF), lossy quality, lossless JPEG mode, canonical JPEG rebuild (`--jpeg-canonical`), JPEG coefficient scrub (`--scrub-dct`), random file names, colour profile handling (convert to sRGB / rebuilt anonymous profile / drop), auto-orient, LSB scrub, camera-fingerprint (PRNU) disruption, extra PNG compression. The advanced switches live in the Options tray; the PRNU switch is off by default because it deliberately moves pixels (a smooth random warp of a few pixels), and it cannot be combined with bit-exact JPEG mode.
- **Sign with Content Credentials (C2PA)**, optional and off by default (one switch in the Options tray, `--c2pa` on the command line; needs `pip install 'imgstrip[c2pa]'`). A cleaned image can carry one signed statement: metadata and hidden data were removed to protect the source, on this date, and the pixels are identical to / equivalent to / deliberately altered from the file received. The credential vouches for processing only, never for the image's origin or authenticity. The pixel claim is measured by comparing the original and the output, not assumed. Rules that must hold: the signing key lives in memory only (never on disk, in a report, in the page's storage or on a command line); nothing from the original enters the credential (its own credentials are removed and reported; a declaration of AI generation is carried over); the app never goes online (no timestamp server, no remote manifest); the signed file is re-verified and fails closed; the signing switch is never remembered between launches. Optional: a keyed commitment to the original and a private receipt, so the signer can prove the link later without creating one now.
- Tells the user what a public viewer will see: test or self-hosted certificates show as "Unverified" (an amber warning), real ones cannot be checked offline unless the user supplies trust anchors, credentials have no trusted timestamp and read as invalid once the certificate expires.
- Report per file: formats, dimensions, frames, size before and after, every removed item, actions taken, warnings, and whether pixels are exact.
- Reads JPEG/MPO, PNG/APNG, WebP, GIF, TIFF, BMP, HEIC/HEIF (needs pillow-heif), AVIF, plus ICO, PSD, TGA, QOI, JPEG 2000, PCX, SGI, PNM, ICNS, DDS (written as PNG).
- Not supported, with a clear error: camera RAW files, 16-bit Lab TIFF, 16-bit PSD, vector formats.
- Out of scope for the web app (CLI only): self-test, random output names, in-place replacement, verifying a credential against an original with `--c2pa-verify` (the web app shows a scanned file's credential, and downloads the private receipts).
- Terminology from the engine: "clean", "check", "removed", "findings" in the categories metadata, hidden, structure.

## Brand Commitments

- Name: **imgstrip** (lowercase).
- Character (user-pinned for the web app): mini, premium-feeling and simple.
- Voice: plain, precise, honest about limits. Says exactly what was found and removed; never overclaims. The README's "Limits" section (visible content stays, robust watermarks can survive, file names can leak, the camera's sensor fingerprint stays unless disrupted and its removal can never be proven, in-place mode does not securely erase) is product truth and must not be softened.

## Evidence on Hand

- README.md: worked example (a Nikon COOLPIX P6000 JPEG with EXIF, GPS, an embedded thumbnail and XMP), the full list of what gets removed, format support, and limits.
- Test evidence stated in README.md: built-in self-test (21 worst-case fixtures, 255 runs, cross-checked by readers independent of imgstrip), 873 pytest tests including fuzzing, differential tests and a byte-flip audit of every format, and a trial on the public exif-samples corpus (104 files, 0 failures).
- No testimonials, users, press or benchmarks exist. Do not invent any.

## Product Principles

1. **Fail closed.** If the engine cannot prove a file is clean, the user gets an error, never an unverified file.
2. **Originals are sacred.** Nothing the app does modifies an input file.
3. **Simple first, detail on demand.** The default view shows only what matters to act; the full report and every option are one step away, never in the way.
4. **Show, then remove.** Files are scanned first; users see what each one reveals before anything is cleaned, so the value is visible, not asserted.
5. **Honest about limits.** State what cleaning cannot do as clearly as what it does.

## Accessibility & Inclusion

No product-specific requirement established beyond a sound web baseline (keyboard operable, WCAG AA contrast, reduced-motion respected).
