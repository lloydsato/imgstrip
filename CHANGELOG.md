# Changelog

All notable changes to imgstrip. Versions follow [semantic versioning](https://semver.org/); each released version is
tagged `vX.Y.Z` in the repository and archived on Zenodo with its own DOI (see `CITATION.cff`).

## [2.0.2]

Fixes and hardening found in a line-by-line review of 2.0.1. No change to what the engine writes, to the verifier or to
the sandbox; every fix has a test.

### Security

- **Text from a file could reach the terminal unfiltered.** File names, and metadata shown by `--check`, `-v` and
  `--check --deep`, were printed as they were, so a name or a tag holding terminal escape sequences (cursor moves, a
  changed window title) could rewrite what the person sees. The command line now writes every control character
  (including the C1 range and the characters that reorder text, such as U+202E) as an escape (`\x1b`, `\u202e`).
  `--json` output already escaped them and is unchanged. The web app's file-name cleaner had the same gap for the C1
  range and the reordering characters; it now strips them.
- **The web app read an upload before it took a place in the queue.** Requests that were waiting for a free slot
  already held their whole upload in memory, so a few slow or large requests could use far more memory than the slot
  count suggests. The body is now read only once the request has a slot, and an upload that arrives slower than about
  1 MiB/s (after a minute's grace) is cut off with a 408.

### Fixed

- **Option errors read as crashes or did odd things.** `--suffix` with a path separator (`-s "../x"`, `-s /`) ended in
  "internal error: ValueError"; it is now a usage error. `--max-pixels -5` reported "-0 MP"; it must now be a positive
  whole number. `-o all.jpg a.png b.png` made a directory called `all.jpg`; with several files, an `-o` that does not
  exist yet and looks like a file name is refused (create the directory first, or give a name without an image
  extension). A symbolic link loop, or a link to nothing, now says so instead of "no such file".
- **Making a file's permissions raced with other threads.** The default mode of a new file was found by setting the
  process umask to zero and putting it back, which another thread could observe. On Linux the umask is now read from
  `/proc/self/status`; elsewhere it is read once when the module loads.
- **The refusal of a very noisy picture did not say why or what to do.** A picture of static or dense grain (a lossy
  codec reproduces it worst) can fail the pixel check of a lossy output ("re-encoded image deviates too much", or the
  8x8 block message). Both messages now say so and point to `--format png`. We tested the way out before writing it:
  PNG always gets such a picture through, and a higher `-q` does not (the allowed error shrinks as the quality rises),
  so the message does not suggest that. Behaviour is unchanged: the picture is still refused and nothing is written.
- **A test that could not fail.** `test_new_files_respect_umask` referred to a name that no longer existed and accepted
  two different modes; it now checks the exact mode.

### Documentation

- The README said that on macOS the decoder "cannot write to files". Only Linux with Landlock stops it creating,
  changing and deleting files; on macOS and older kernels the limit is that it cannot write data (`RLIMIT_FSIZE`), as
  `docs/REFERENCE.md` already said. The README now says the same.
- The sandbox cost is given as it was measured on a two-core test machine, not as "about 15 ms per file": about 5 to
  10 ms for a small picture, about 0.2 s for a 12-megapixel photo, and about 0.2 s once per run to start it.
- `SECURITY.md` says how to report a vulnerability privately, and what counts as one.

### Project

- **`.github/workflows/fuzz.yml` is in the repository.** 1.2.5 and later changelogs and `fuzz/run.py` name it, but it
  was not in the 2.0.1 tree. Every Atheris target runs for a minute on each push and pull request and for half an hour
  each night; a second job (night and on request) fuzzes the decoders through a Pillow built with AddressSanitizer and
  UndefinedBehaviorSanitizer. A crashing input is kept as an artifact.
- Package metadata: author, keywords, classifiers, project URLs (documentation, changelog, source, security policy).
  The numpy floor is now `numpy>=1.26` (1.24 has no build for Python 3.12 and later, so it could not be met there).
  Python 3.14 is in the test matrix, and a new CI job installs the lowest versions pyproject allows
  (numpy 1.26.4, Pillow 13.0.0, pillow-heif 0.18.0) on Python 3.11.
- GitHub Actions are pinned to commits (the version is in a comment), workflows get read-only tokens, and Dependabot
  keeps the pins current. A `.gitignore` is added.
- About sixty `# noqa` comments for rules this project does not enable are plain comments now.

## [2.0.1]

A dependency floor, no code change in the engine, the command line or the web app. imgstrip now requires **Pillow 13.0.0
or later** (it asked for 11.2 or later before). Pillow 13 needs Python 3.11, so imgstrip now requires **Python 3.11 or
newer**; Python 3.10 reached end of life on 1 October 2026 and gets no further security fixes.

### Security

- **Pillow up to 12.3.0 can hand back uninitialised memory as pixels when it decodes a damaged TIFF** (JPEG-compressed or
  Group 4 fax, with strips or tiles that end early). Pillow published this as GHSA-q45j-58j2-45rg and fixed it in 13.0.0.
  imgstrip's verifier cannot see it: stage 2 compares the output with what the decoder returned, and the output holds
  exactly that, so both checks pass. With the decoder sandbox on (the default) we found no pixels of an earlier image in
  the output; with `--no-sandbox` or `sandbox=False`, pixels of the image processed just before did reach the output.
  Requiring Pillow 13.0.0 closes it. If you must stay on an older Pillow, keep the sandbox on, and do not use
  `--no-sandbox`.

### Tests

- The credentials UI test failed intermittently, on Pillow 13 and, much less often, on Pillow 12.3. It clicked the
  Content Credentials switch, and later the Remove button, while the page was still starting or moving, so the click
  sometimes did not land. The test now waits until the page has finished starting and flips the switch and presses
  Remove through events. This is a fix to the test; the app is unchanged.
- `test_the_strip_size_never_changes_the_lsb_statistics` passed `None` to `pytest.approx` for the four image shapes that are too
  small for the weighted estimate, which pytest 9.2.0 refuses with a `TypeError`. The test now checks for `None` first.
- `tests/test_pillow_floor.py` checks that the installed Pillow meets the floor the package declares, and that
  `requires-python` is no lower than the Python that Pillow's floor needs, so a test run can never quietly use a Pillow
  below it and the package can never declare a Python it cannot install on.

## [2.0.0]

A new look for the web app, an explanation beside every switch, and file fingerprints. This is a major version because
the web app's whole interface is new (layout, where Deep scan and Remove live, colours). The engine, the command line,
the library and the web API's existing fields are unchanged: with the same options, 2.0.0 writes the same bytes as
1.2.8.

### Added

- **An (i) button beside every switch** in the web app (all twelve of them, the one in the bottom bar and the one in
  the signing panel included). It opens a short explanation in plain words and closes with Escape, a click elsewhere or
  by moving on. It is a button, so it works from the keyboard, it is read out when it opens, it never flips the switch
  beside it, and the bubble is kept on the screen at any width.
- **SHA-256 and MD5 of every file, before and after cleaning.** Each row shows the shortened hashes of the file as
  received, and once it is cleaned a *Before* and an *After* line, each with a copy button. The detail pane shows them in
  full, in groups of eight characters, and copying gives the plain hex. SHA-256 is the proof; MD5 is there only to match
  older records, and the page says so. The server hashes the exact bytes: `/api/inspect` returns `fingerprints`, and
  `/api/clean` returns `before` and `after` (each with `sha256`, `md5` and `bytes`) beside the existing `sha256` and
  `md5` of the cleaned file.
- **Compare with a hash you already have.** Paste a SHA-256 or MD5 (spaces, capitals and a `SHA-256:` label are fine) into
  a file's detail and it says whether it is the hash of the file as added or of the cleaned one, or that there is no
  match. An MD5 match carries a reminder that MD5 can be forged.
- **Hash list.** One button downloads `imgstrip-hashes.csv` with every file's name, state and before and after SHA-256
  and MD5. It is built in the page and sent nowhere; file names that a spreadsheet would run as a formula are neutralised.

### Changed

- **The web app is rebuilt as two panes**: the list of files on the left (thumbnail, name, format, size, what it gives
  away, hashes, status), and what the chosen file gives away on the right (plain-language summary, identifying metadata,
  hidden data, colour profile, the pixels, file fingerprints). Below 900 pixels the page is one column and the detail
  opens as a full-screen sheet that holds the keyboard, hides the page behind it, and closes with Escape or *Back to
  list*. It was tested from 320 to 1920 pixels wide, in light and dark, with touch targets of at least 44 pixels.
- *Deep scan* moved from the Options tray to the bottom bar, beside the Clean button. Verified results are teal rather
  than green. A file is removed from the list from its detail (*Remove from list*). *Add* appears only on narrow screens,
  where the drop zone is far from the list. Scanning shows an indeterminate bar, not a percentage, because the work has
  no way of knowing how far along it is.

### Tests

`tests/test_redesign_ui.py` (new) drives a real browser through the (i) buttons, the hashes (checked against `hashlib`
and against the bytes the download link serves), the compare box, the hash list, the selection and the phone sheet, and
twelve screen sizes in light and dark for sideways scrolling, the bubble staying on screen, touch targets and text
contrast. The other browser tests were moved to the new layout, and `tests/test_web.py` checks the hashes the server
sends.

## [1.2.8]

### Added

- **`--scrub-alpha` (and `--alpha-tolerance N`): clean data hidden in the transparency channel.** `--scrub-lsb`
  deliberately keeps alpha 0 and 255 exactly, so a message written as 255-versus-254 over the solid parts of a
  picture, or 0-versus-1 over its clear parts, passed through it (in whole or in large part), and `--check --deep` does
  not look at alpha at all. The new option is **off by default**, and with it off nothing changes. When on:
  - alpha within the tolerance of fully opaque is set to exactly opaque, and alpha within it of fully clear to exactly
    clear, with the colour under it dropped as under any clear pixel;
  - the lowest bit of every alpha level in between is randomised (the remaining levels pair up as even and odd, so a
    value never falls back into the snapped part);
  - colour is never touched where the pixel still shows; the two scrubs are independent and can be combined;
  - the tolerance is 5 levels of 255 by default, 1 to 32 with `--alpha-tolerance` (an even value is rounded up to the
    next odd one, and the report says which was used); it is a usage error without `--scrub-alpha`;
  - 8-bit and 16-bit alpha, grey-with-alpha, RGBA, palette images with per-entry transparency (widened to RGBA so the
    scrub is per pixel), GIF, and every frame of an animation are covered; a picture whose alpha becomes all 255 is
    stored without an alpha channel; images with no transparency channel, and every JPEG mode, report
    "nothing to clean";
  - it runs after the camera-fingerprint warp (which resamples alpha into new in-between values) and before the LSB
    scrub; a transparent picture saved as JPEG is flattened after it, so a message in alpha cannot reach the JPEG's
    pixels either.

  Reported as `"alpha"` in `--json` and the web API (`state`, `headline`, `summary`, `caveat`, `tolerance`, `pixels`), as
  a "transparency" line on the command line, and in the web app's result panel; `Result.alpha_scrub`,
  `Result.alpha_pixels` and `Result.alpha_tolerance` for library users, `Options(scrub_alpha=True,
  alpha_tolerance=5)` to ask. The web app has a "Clean transparency channel" switch, greyed out for an image that has
  no transparency channel (the image facts the server sends now include `alpha`). The self-test matrix runs the new
  option on every fixture. Tests: `tests/test_scrub_alpha.py`.

  **What it does not cover** (documented in `docs/REFERENCE.md`): a message written with a difference larger than the
  tolerance survives (raise `--alpha-tolerance`, or flatten the picture); colour hidden under partly transparent
  pixels is still visible colour and is the business of `--scrub-lsb`; and there is still no detector for alpha in
  `--check --deep`.

### Changed

- **Content Credentials: a new operation, `alpha-scrubbed`,** is recorded (at detail `full`) when the alpha scrub
  changed at least one alpha value, and makes the pixel-integrity claim "altered", like the other deliberate scrubs.
  The set of operation names is closed, so a credential that lists it is not recognised as well-formed by a verifier
  from before 1.2.8; leave `--scrub-alpha` out when signing images that older tools must verify.

## [1.2.7]

### Fixed

- **Six legal, invisible structures were accepted by the output verifier and reported as clean by `--check`.**
  Each could carry data that no viewer shows:
  - a suggested palette (`PLTE`) in a truecolour or RGBA PNG (up to 768 bytes);
  - a PNG `tRNS` colour key with bits above the sample depth or matching no pixel, and a palette `tRNS` in which every
    entry is opaque;
  - empty `IDAT` chunks and `IDAT` chunk boundaries chosen by the writer;
  - a JPEG restart interval (`DRI`) that no restart marker uses;
  - JPEG component identifiers other than 1..n;
  - GIF still images with a frame delay, a loop count or the GIF87a signature.

  Rules that no legitimate input breaks are in the inspector, so `--check` reports them. Choices that other encoders
  make differently and that imgstrip's writers fix (the `IDAT` chunk size, the JPEG component identifiers, the GIF89a
  signature) are an output-only canonical-form check in the verifier, `canonical_form_problems`. A colour key that no
  pixel is transparent through is caught by the independent PNG readers. `--check` still reports an unused colour key
  and non-standard JPEG component identifiers as clean (the first needs the pixels, the second is normal in inputs);
  the verifier refuses both in an output. `--jpeg-lossless` keeps the input's component identifiers, as it keeps the
  rest of the compressed data. Tests: `tests/test_invisible_structures.py`.
- **A valid 1-bit BMP whose palette maps index 0 to white was refused** ("pixels changed during lossless
  re-encoding"). Pillow reads an 8-bit BMP with a two-entry black, white palette as a 1-bit image, so the verifier
  saw different pixels. Such a picture is now written as a 1-bit BMP.
- **A PNG whose text keyword was corrupted into a very long string was refused with the decoder sandbox on but cleaned
  with `--no-sandbox`.** The label of a finding could exceed the 400 characters the sandbox's answer validator
  accepts. Every finding label is now cut at 300 characters where it is made, so both paths agree.

## [1.2.6]

### Fixed

- **Converting to HEIC/HEIF failed with `KeyError: 'HEIF'` when the input was not itself a HEIC/HEIF file** (command
  line and web app, with the default decoder sandbox). The HEIF writer is registered by the plugin set-up that opening
  an image does, and with the decode stage in the sandbox the process that encodes had never opened one. The encode
  stage now registers the plugins itself. Test: `tests/test_sandbox.py` (it runs in a fresh process, because every
  other test has already opened an image).
- **The fuzzing workflow named in 1.2.5 was missing from the repository** (`.github/workflows/fuzz.yml` had never
  been committed). It is there now: every Atheris target in `fuzz/targets.py` runs for a minute on each push and pull
  request and for half an hour each night, and a second job fuzzes the decoders through a Pillow built with
  AddressSanitizer and UndefinedBehaviorSanitizer; a crashing input is kept as an artifact.
  `tests/test_fuzz_targets.py` checks that the workflow lists every target (and skips where the file is not in the
  checkout, as in a source distribution).
- **`--json` now lists a path that cannot be used** (missing, a directory without `-r`, not a regular file) as a
  `failed` record in `files`, with its message, instead of counting it in the summary only.
- `--c2pa-date` and the other credential options without `--c2pa` now say "only works" for one option and "only
  work" for several.
- The web app's save-to-folder no longer reads each cleaned file whole into memory (a batch ZIP can be gigabytes):
  `fileio.write_new` and `replace_atomic` accept the path of a file to copy as well as bytes. An unexpected server
  error on a POST or DELETE now answers with the exception type only, like a GET, because its message can quote a
  path.
- The FFmpeg reader used by `--self-test` and the differential tests asks for every frame with `-fps_mode passthrough`
  where FFmpeg has it (5.1 and later) instead of the deprecated `-vsync 0`. The exiftool cross-check reports a file
  exiftool cannot read, or one it flags with an error, instead of crashing or passing it (`tests/test_selftest.py`).

### Changed

- The layout test that follows a review chip waits for the smooth scroll to stop instead of sleeping 700 ms, which a
  long list on a slow machine overran; a truncation test no longer ends in `... or True`.
- Removed code nothing used: `cli.atomic_write`, the `originals` parameter of `cli.verify_one`,
  `dct.same_coefficients` and `credentials.seal`. `jumbf.extract_store` no longer reports a `KeyError` raised inside
  an extractor as "cannot carry Content Credentials". Added a `.gitignore`.

## [1.2.5]

This release answers the preprint *imgstrip: Sanitising Images by Rebuilding Them from Their Pixels, with Fail-Closed
Verification of the Result* (October 2026), which evaluated 1.2.4 and reported nine findings, F1 to F9, and the review
of it that followed (items 1 to 14 below). Each entry names the finding or item it answers and the tests that pin it.

### Security and verification

- **F1 / item 1: unreferenced JPEG tables and fill bytes are accounted for.** The structural walk now tracks which
  quantisation, Huffman and arithmetic-conditioning tables a scan actually latches (by scan type: sequential,
  progressive DC first and refinement, AC, lossless, arithmetic) and reports any table that no scan uses, or that is
  redefined before use, as hidden data; so are 0xFF fill bytes before a marker or inside entropy-coded data. Both
  are refused in every output (fault injection now catches the three faults the review planted) and reported as
  removed from inputs. imgstrip's coefficient reader and `--jpeg-lossless`'s scan copier now treat fill bytes the
  way libjpeg does (they were counted as data before). Tests: `tests/test_verify_faults.py`,
  `tests/test_coverage_audit.py`.
- **F2 / item 2: lossy outputs are checked block by block.** The mean-error bound for JPEG, lossy WebP, AVIF, HEIF
  and quantised GIF frames is now joined by a bound on the worst 8 x 8 block (luma, chroma and alpha separately),
  calibrated per format, chroma layout and quality by `scripts/calibrate_lossy.py` with a margin of 2 and fixed
  floors. The review's faults (a black rectangle over 1 % or 2 % of the picture, 1 % of the pixels replaced) went
  from 0 of 12 detected to 12 of 12; noisy photographs up to sigma 60 are still accepted. Tests:
  `tests/test_verify_faults.py`.
- **Item 3: a second, independent reader in stage 2.** PNG and APNG output is verified a second time by re-filtering
  the intended scanlines and comparing with the stored bytes, BMP by a direct reader, and the JPEG coefficient
  modes by imgstrip's own coefficient reader (and the canonical form must be a fixed point). Default-mode JPEG output
  can also be decoded by imgstrip's own JPEG decoder with `--independent-jpeg-check` (pure Python, about 0.3 s per
  megapixel, so opt-in). Each result lists what verified it (`verified_by`). Tests: `tests/test_refdecode.py`.
- **Item 4: decoding in a sandbox.** Every input is decoded in a fresh worker process, forked per file from a zygote
  started from a clean interpreter, that cannot write to files (`RLIMIT_FSIZE` 0), dump core, run for more than 15
  minutes of CPU, or gain privileges, and on Linux with Landlock cannot create, delete or execute files, use TCP, or
  attach to other processes. It hands back only pixels or DCT coefficients and a short list of typed values over a
  pipe (JSON and raw blocks, never pickle), which the main process checks field by field before use. On by default;
  `--no-sandbox` (CLI and web app) and `Options(sandbox=False)` decode in-process. Outputs are byte-identical either
  way. Tests: `tests/test_sandbox.py`; CI runs them on macOS and Windows too.
- **Item 5: one safe write path.** Every file imgstrip writes (outputs, the web app's *Save to folder*, the C2PA
  receipt and test identity) goes through `imgstrip.fileio`: a temporary file in the destination's directory, fsync,
  then an atomic publish that never replaces an existing file (a hard link, or `renameat2(RENAME_NOREPLACE)` /
  `renamex_np(RENAME_EXCL)`), so the check-then-write race of 1.2.4 is gone; `--force` and `--in-place` replace with a
  single rename and never open an existing file for writing. A dangling symbolic link at the destination counts as
  taken. Tests: `tests/test_fileio.py`.

### Residual channels

- **Item 6: what the output keeps is normalised or documented.** The colour model now follows the pixels in every
  format (an opaque alpha channel is dropped, grey is stored as grey, 256 colours or fewer get a canonical palette;
  TIFF, BMP and JPEG kept the input's model before, a channel of one to two bits per file); frame durations are
  whole non-negative milliseconds and loop counts 0 to 65535. What remains (format, chroma subsampling, WebP's
  lossless flag, CMYK, 16-bit depth, frames, timing and loop count) is the documented equivalence relation, with its
  capacity, in docs/REFERENCE.md ("What the output keeps"), and every result names what it carried over (`kept` in
  `--json` and the web app; `-v` prints it). Tests: `tests/test_equivalence.py` (minimal pairs both ways).
- **F5 / item 7: the ±1 DCT payload after `--scrub-dct`, explained and documented.** The bit error rate of 0.562
  comes from the scrub's zero zone: a ±1 change at its edge (1 and 2, −2 and −3) decides whether a carrier ends up
  zero, so a reader who knows the carriers recovers about 0.08 bit per carrier (mutual information; the review's
  symmetric-channel estimate was 0.011). Randomising the zone's edge, the measured fix, costs 1.3 to 2.4 dB of extra
  picture quality, above the 1 dB set as the limit for adopting it, so the scrub is unchanged and the docs now say "reduced, not
  closed" and no longer claim that the tool's own extractor reads pure noise. Experiment:
  `scripts/dct_matching_leak.py`; test: `tests/test_dct_scrub.py`.

### Robustness

- **Item 8: coverage-guided fuzzing.** Atheris targets for the structural walk, the JPEG rebuild, the coefficient
  reader and canonical writer (a fixed-point check), the ICC sanitiser, the JUMBF/CBOR parser, the main process's
  reading of a sandbox answer, Pillow's decoders and the whole engine (`fuzz/`), run for a minute each on every push
  and for longer nightly, plus a job that fuzzes the decoders through a Pillow built with AddressSanitizer and
  UndefinedBehaviorSanitizer (`.github/workflows/fuzz.yml`). `tests/test_fuzz_targets.py` runs every target on its
  seeds and on mutations without Atheris.
- **F7 / item 9: the SIGHUP test no longer depends on its environment.** It failed when the test runner itself
  ignored SIGHUP (for example under `nohup`); the server under test now starts with default signal dispositions, and
  the test runs both with an inherited default and an inherited ignored SIGHUP. Test: `tests/test_web.py`.
- **Item 10: a regression test for every finding fixed**, listed with each entry above.

### Usability and wording

- **F4 / item 11: "re-encoding destroys DCT steganography" is gone.** Re-encoding degrades coefficient payloads
  (the review measured bit error rates of 0.37 to 0.45 at known positions); only `--scrub-dct` targets them. The
  README, reference, web app hints and messages say so, and `tests/test_docs_claims.py` refuses the old claim
  anywhere in the docs, sources or web page.
- **Item 12: memory.** Peak memory per pixel is measured for JPEG, PNG, 16-bit RGBA PNG and animated GIF
  (`scripts/memory_table.py`, table in docs/REFERENCE.md). The default `--max-pixels` is now 150 million (was 300
  million; the web app keeps 100 million), and the automatic worker count of a batch is also capped by the memory
  available for the largest file's pixels (an explicit `-j` is unchanged).

### Release hygiene

- **Items 13 and 14:** this changelog and `CITATION.cff`; version 1.2.5. The 1.2.4 tag and release are unchanged.

### Fixed along the way

- CMYK JPEGs were re-encoded with three of their four channels at a quarter of their resolution (Pillow reports no
  subsampling for four components and fell back to 4:2:0); they now keep their own subsampling, and the block check
  compares CMYK channel by channel.
- Very noisy pictures could not be written as JPEG at high qualities ("broken data stream": Pillow's output buffer
  for optimised Huffman tables was too small); they are now written with standard Huffman tables.
- imgstrip's coefficient reader and `--jpeg-lossless` read fill bytes inside entropy-coded data differently from
  libjpeg (see F1).
- Found by the fuzzer: a truncated JPEG segment length raised a bare `struct.error` instead of a JPEG error, and a
  colour profile whose header names an out-of-range rendering intent made the whole file fail instead of falling
  back to the default intent. Found while writing the sandbox-answer target: a deeply nested or type-confused worker
  answer could make the main process raise something other than a refusal. All are fixed and kept as regression
  inputs or tests.

### Not changed (findings that need no code change)

- **F3:** a file produced by `--scrub-lsb` is flagged by the `--deep` detector (312 of 312), because random low bits
  look like a fully loaded payload. A consequence of the design; the tool already warns when it happens and reports
  such a file as "scrubbed".
- **F6:** `--jpeg-lossless` preserves the input's encoding choices (progressive scans, restart intervals, Huffman
  tables): by design and documented; `--jpeg-canonical` is the mode that normalises them.
- **F8:** the LSB detector flagged some clean 192 x 192 crops and tiny whole images, none of the whole images of
  0.02 MP or more: a property of very small test material, in line with the documented calibration ("a false
  positive is possible").
- **F9:** in the review's PRNU simulation a minority of scrubbed images stayed above the PCE threshold of 60, where
  the documented calibration on 1 to 12 MP pictures found none. The simulation differs in several ways at once (grey
  scenes, no shot noise, no JPEG compression, another denoiser); the documentation already says the scrub lowers the
  match without making identification impossible. No change to the scrub; the question is listed as open.

## [1.2.4]

Released before this changelog was started; see the `v1.2.4` tag and its GitHub release. This is the version the
preprint describes.

[2.0.1]: https://github.com/lloydsato/imgstrip/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/lloydsato/imgstrip/compare/v1.2.8...v2.0.0
[1.2.8]: https://github.com/lloydsato/imgstrip/compare/v1.2.7...v1.2.8
[1.2.7]: https://github.com/lloydsato/imgstrip/compare/v1.2.6...v1.2.7
[1.2.6]: https://github.com/lloydsato/imgstrip/compare/v1.2.5...v1.2.6
[1.2.5]: https://github.com/lloydsato/imgstrip/compare/v1.2.4...v1.2.5
[1.2.4]: https://github.com/lloydsato/imgstrip/releases/tag/v1.2.4
