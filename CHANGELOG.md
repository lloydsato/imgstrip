# Changelog

All notable changes to imgstrip. Versions follow [semantic versioning](https://semver.org/); each released version is
tagged `vX.Y.Z` in the repository and archived on Zenodo with its own DOI (see `CITATION.cff`).

## [Unreleased]

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

[1.2.5]: https://github.com/lloydsato/imgstrip/compare/v1.2.4...v1.2.5
[1.2.4]: https://github.com/lloydsato/imgstrip/releases/tag/v1.2.4
