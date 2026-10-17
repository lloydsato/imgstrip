# imgstrip

`imgstrip` removes **all** metadata and hidden data from images and leaves only the picture. Use it from the command line, or open the local web app with `imgstrip-web`.

Most metadata removers delete the tags they recognise and copy everything else. imgstrip does the opposite: it decodes the image to raw pixels and writes a brand-new file from those pixels alone. By default nothing from the original file is copied except pixel values; the opt-in JPEG modes (`--jpeg-lossless`, `--jpeg-canonical`, `--scrub-dct`) keep the picture's compressed data or coefficients, and a few bits of structure (see below). Before anything is written, every output is checked twice:

1. A strict, format-aware parser walks the whole structure of the new file. Any segment, chunk, box or tag it cannot account for makes the run fail (it fails closed), and so does a place that must be empty but is not: padding, reserved fields, palette entries no pixel uses, colour stored under fully transparent pixels. (It cannot vouch for slack inside compressed data; see [What "accounted for" means](#what-accounted-for-means).)
2. The new file is decoded again and compared with the intended pixels: bit-exact for lossless formats, within a quality-dependent tolerance for lossy ones.

If either check fails, nothing is written. `imgstrip --self-test` adds a third opinion: readers that share no code with imgstrip or Pillow must see the same pixels in every output ([how](#how-it-is-tested)).

```console
$ imgstrip --check DSCN0010.jpg
FOUND    DSCN0010.jpg (JPEG 640x480): 4 item(s)
           [metadata] EXIF: 11250 bytes; make=NIKON, model=COOLPIX P6000, software=Nikon Transfer 1.1 W, date=2008:11:01 21:15:07, taken=2008:10:22 16:28:39, maker notes
           [metadata] GPS location: 43.467448, 11.885127
           [hidden] Embedded EXIF thumbnail: 6702 bytes
           [metadata] XMP: 4000 bytes

$ imgstrip DSCN0010.jpg
cleaned  DSCN0010.jpg -> DSCN0010.clean.jpg  (JPEG 640x480, 157.9 KB -> 184.7 KB)
           removed: EXIF, GPS location, Embedded EXIF thumbnail, XMP

$ imgstrip --check DSCN0010.clean.jpg
clean    DSCN0010.clean.jpg (JPEG 640x480)
```

## Install

Requires Python 3.10+.

```console
pip install .            # installs Pillow, numpy and pillow-heif (for iPhone HEIC photos)
imgstrip --self-test     # proves the installation works end to end, with independent readers cross-checking the output
pip install '.[c2pa]'    # optional: Content Credentials (C2PA) signing, see below
```

## Web app

```console
imgstrip-web             # starts the app on this computer and opens it in your browser
```

The web app is the same engine behind a small window in your browser. It runs only on your own machine (on `127.0.0.1`), works offline, and never uploads anything.

1. **Drop images** onto the window, choose them, or paste them. Each file is scanned straight away and its row says what it gives away: location, camera, time taken, serial numbers, hidden preview images, appended data. Open a row for every item, grouped as identifying metadata, hidden data and colour profile. Nothing is changed yet. The scan reads the file's structure; to also search the pixels for steganography, turn on **Deep scan** or use *Scan the pixels too* on one file. What it says about the pixels is always one of four verdicts (*detected*, *scrubbed*, *not detected*, *not scanned*), shown with what it does and does not mean ([below](#hidden-data-in-the-pixels-detected-scrubbed-not-detected)); a scan that finds nothing is never presented as proof that nothing is hidden.
2. **Clean.** Every file is rebuilt from its pixels and passes both checks before it is offered back. Each verified file gets a small seal engraved from its SHA-256 (the same file always gets the same seal); open the row for the checks, the full hash, and the list of what was removed.
3. **Take the clean files**: download one, download all as a ZIP (its entries carry no dates), or **save to a folder** on disk. Saving creates the folder if needed and never replaces an existing file; a name that is taken gets a number instead (`photo.clean (2).jpg`).

**Options** holds the same switches as the command line: output format, quality, colour profile handling, keeping JPEG data bit-exact, LSB scrubbing, **disrupting the camera's sensor fingerprint**, orientation, extra PNG compression, the **deep scan** that also searches the pixels for steganography, and the optional **Sign with Content Credentials** switch (see [below](#content-credentials-c2pa-optional)).

| Option | Meaning |
|---|---|
| `--port N` | Port on `127.0.0.1` (default 8765; a free port is used if it is taken). |
| `--no-browser` | Print the link instead of opening the browser. Use it on a computer shared with other users (see below). |

How it stays private:

* Originals are never written to disk. They are read from the request, cleaned in memory, and only the verified clean output is kept, in a private temporary folder that is deleted when you stop the app (Ctrl+C).
* The terminal prints a link with a secret, single-use key. The page trades that key for an access token, which it keeps only in that browser tab, and the terminal then prints a fresh link. Each link replaces the previous tab's token, so only the newest tab can use the app. No cookies are used (browsers share cookies between all local ports), and requests addressed to any other host name are refused. Other websites open in your browser cannot drive the app or read your images.
* On a computer shared with other users, start it with `--no-browser` and open the printed link yourself: opening a browser passes the link on its command line, which other local users can briefly see. If someone else uses a link first, your tab says "already used"; restart with `--no-browser`.
* If the scan finds signs of steganography in the pixels and *Scrub least-significant bits* is off, the cleaned file is marked "Payload kept" instead of getting the seal.
* The server keeps no log of file names, and the page loads nothing from the internet (its typeface, Schibsted Grotesk, ships with the app under the SIL Open Font License).

## Usage

```console
imgstrip photo.jpg                     # writes photo.clean.jpg next to it
imgstrip -o clean.png photo.png        # choose the output file
imgstrip -r ~/Pictures -o ~/clean      # whole folder tree, mirrored into ~/clean
imgstrip -i *.png                      # replace originals in place (atomic rename)
imgstrip --random-name -o out/ *.jpg   # also hide the original file names
imgstrip IMG_1234.heic --format jpeg   # convert while cleaning
imgstrip --scrub-prnu photo.jpg        # also scramble the camera's sensor fingerprint (see below)
cat in.webp | imgstrip - > out.webp    # stdin -> stdout

imgstrip --check photo.jpg             # report what a file contains, change nothing
imgstrip --check --deep image.png      # also look for data hidden in the pixels
imgstrip --deep photo.png              # clean, and say whether a pixel payload was found, kept or scrubbed
```

Originals are never modified unless you pass `--in-place`. Existing outputs are never overwritten unless you pass `--force`.

| Option | Meaning |
|---|---|
| `-o PATH` | Output file (single input) or directory (several inputs, `-r`). `-` = stdout. An output file name's extension picks the format (`-o clean.png` writes a PNG); a `--format` that contradicts it is refused. |
| `-i`, `--in-place` | Replace the originals atomically, keeping their permission bits. |
| `-r` | Recurse into directories. Hidden directories are skipped. |
| `--format FMT` | Output format: `jpeg png webp gif tiff bmp avif heif`. Default: same as input. |
| `-q N` | Quality for lossy output (JPEG/WebP default 95, AVIF/HEIF 90). |
| `--jpeg-lossless` | Strip JPEGs without re-encoding (see below). |
| `--jpeg-canonical` | JPEG only. Rebuild the compressed data from its DCT coefficients in one canonical form: no pixel changes, and data hidden in table layouts, padding, markers and the like is gone. The coefficients are kept, so DCT steganography survives. See [JPEG DCT steganography](#jpeg-dct-steganography---scrub-dct). |
| `--scrub-dct` | JPEG only. Everything `--jpeg-canonical` does, plus destroy steganography hidden in the DCT coefficients (JSteg, F5, steghide, J-UNIWARD, ...). It changes pixels slightly and says by how much. Off unless given. |
| `--dct-min-psnr DB` | With `--scrub-dct`: refuse a picture the scrub would change by more than this (default 25; `0` never refuses). |
| `--icc convert\|sanitize\|drop` | Colour profiles: convert pixels to sRGB (default), embed a rebuilt anonymous profile, or discard. |
| `--no-auto-orient` | Don't rotate pixels according to EXIF orientation. |
| `--scrub-lsb` | Randomise the least-significant bit of every sample to destroy LSB steganography. The report says "scrubbed" only if that happened: not with `--jpeg-lossless`, and "partly" if some frames could not be. |
| `--deep` | Also decode the pixels and look for hidden data, with `--check` or while cleaning. A scan that finds nothing is not proof that nothing is hidden. See [below](#hidden-data-in-the-pixels-detected-scrubbed-not-detected). |
| `--scrub-prnu` | Disrupt the camera's sensor fingerprint (PRNU) with a smooth random warp that moves parts of the picture by a few pixels. Not combinable with `--jpeg-lossless`. See [Camera sensor fingerprint](#camera-sensor-fingerprint-prnu). |
| `--optimize` | Try extra PNG compression strategies (slower, smaller). |
| `--max-pixels N` | Refuse images larger than N pixels (default 300 million; the web app uses 100 million). Also protects against decompression bombs. `--jpeg-canonical` and `--scrub-dct` also refuse a picture whose canonical rewrite would need more than about 3 GiB of memory (noisy JPEGs reach it at roughly 10 megapixels, smooth ones at about 50). |
| `-n`, `--dry-run` | Do all the work in memory, write nothing. |
| `--json` | Machine-readable report. |
| `-j N` | Process N files in parallel. |
| `-v` / `--quiet` | Show every removed item / only errors. |

Exit status: `0` success (or `--check`: everything clean), `1` at least one file failed, `2` usage error, `3` `--check` found metadata or hidden data.

## What gets removed

| Category | Examples |
|---|---|
| Descriptive metadata | EXIF (camera make/model, serial numbers, lens, owner, timestamps, maker notes), GPS location, XMP (including edit history, drone telemetry, Google/Apple camera data), IPTC, Photoshop resources, comments, PNG text chunks, TIFF tags, DPI/print resolution, C2PA/JUMBF content credentials |
| Embedded images | EXIF/JFIF/Photoshop thumbnails (which can show the *uncropped* original), MPO secondary images, depth maps, HDR gain maps, HEIC/AVIF auxiliary and thumbnail items, TIFF preview/thumbnail pages, APNG default images |
| Hidden payloads | Data appended after the end of the image (ZIP/RAR "polyglots", Samsung trailers, aCropalypse remnants of un-truncated earlier versions), unknown/private chunks and boxes, unreferenced bytes inside TIFF/BMP/HEIF, extra data inside the PNG pixel stream, padding and stray bytes |
| Hidden pixel data | Colour stored under fully transparent pixels, unused and duplicate palette entries (and palette order), pixels outside an AVIF clean-aperture crop, GIF frame areas outside the canvas |
| Colour metadata | ICC profiles (often naming the device/owner), PNG gAMA/cHRM/sRGB/iCCP. The colours are *converted*, so the picture looks the same. |

## What changes, and what doesn't

* **Orientation** is baked into the pixels. A photo that displayed upright still displays upright, without the tag. Only EXIF/TIFF orientation is honoured; viewers ignore XMP orientation, so imgstrip does too.
* **Colours**: images with a non-sRGB profile (Display P3, Adobe RGB, ProPhoto, CMYK, gray, or PNG gamma/chromaticity chunks) are converted to sRGB with LittleCMS. Profiles that are numerically equivalent to sRGB are dropped without touching pixels. `--icc sanitize` instead keeps the colour space by embedding a profile rebuilt from scratch (colorimetric data only, fixed header, no text, no dates). imgstrip verifies it is colorimetrically identical to the original.
* **Lossless formats** (PNG, GIF, TIFF, BMP, lossless WebP) keep their pixels bit-for-bit (unless you ask for `--scrub-lsb` or `--scrub-prnu`, which change pixels on purpose). PNGs may get a smaller, equivalent colour type (for example RGB with ≤ 256 colours becomes a palette image).
* **16-bit images** (PNG, TIFF, PPM/PGM) keep full 16-bit precision, even though Pillow itself truncates 16-bit colour to 8 bits. imgstrip recovers the low bytes with a second decoding pass, and has its own 16-bit PNG/TIFF encoders and colour engine. Output was verified bit-identical against ImageMagick.
* **JPEG** is re-encoded at quality 95 with the original chroma subsampling. Re-encoding also destroys JPEG-domain steganography and the quantisation-table "fingerprint" of the camera. To avoid any generation loss, use `--jpeg-lossless`: the compressed image data is copied bit-for-bit and everything around it is rebuilt from scratch. It drops quantisation and Huffman tables no scan uses and refuses a file with bytes inside the compressed data that no decoder reads (use `--jpeg-canonical` for such a file). That mode cannot rotate pixels, so a non-default orientation is kept as a minimal one-tag EXIF block, and DCT-domain content is preserved as-is (`--jpeg-canonical` and `--scrub-dct` are the opt-in ways to normalise it). The claim that re-encoding destroys JPEG-domain steganography is only partly true: it destroys payloads read from coefficient parity, but a payload that changes zeros (J-UNIWARD and similar) mostly survives a re-encode at quality 95 (see the measurements below).
* **Animations** (GIF, APNG, WebP, AVIF) keep all frames, frame timing and loop count. GIFs are written by a custom encoder that keeps every frame's colours exact.
* **Multi-page TIFFs** keep their pages (thumbnail/mask pages are dropped). Pages with more than 8 bits per sample are refused (split them into separate files first), because converting them for a multi-frame file would clip them.
* Output files are new files, so the original file's timestamps, extended attributes (for example macOS "where from" download URLs) and ownership don't carry over. In-place mode keeps only the permission bits.

## Hidden data in the pixels: detected, scrubbed, not detected

`--check` lists everything the container holds beside the pixels. `--deep` also decodes the pixels and looks for:

* colour data under fully transparent pixels,
* unused or duplicated palette entries that hold data,
* **LSB steganography**, estimated with two independent detectors, Sample Pair Analysis (Dumitrescu, Wu & Wang) and Weighted-Stego analysis (Fridrich & Goljan, with the Ker–Böhme predictor). A file is flagged only when both agree. The thresholds were calibrated on 75 real camera photos: the worst clean photo scored 0.117, and every image with a ≥ 20 % simulated payload scored ≥ 0.195. Smaller payloads, or ±1 embedding instead of LSB replacement, can go undetected, and as with any statistical test a false positive is possible.

`--scrub-lsb` replaces the least-significant bit of every colour sample with random noise (a change of at most 1/255, invisible). This destroys any LSB-embedded message regardless of whether it was detected. Fully opaque and fully transparent alpha values are preserved.

A scan can find a payload but can never show there is none, and a scrub covers one family of methods. So every report (command line, `--json`, web app, Content Credentials) says one of four things about the pixels, in the same words, and never more:

| Verdict | Means | Does not mean |
|---|---|---|
| **detected** (likely or possible) | The pixel statistics indicate LSB steganography. | A decoded message. It is evidence and can be a false alarm. A file whose bits were randomised by `--scrub-lsb` reads the same way (random bits look like a full payload), which is why *scrubbed* is a separate state and is never inferred by re-scanning. |
| **scrubbed** | The least-significant bit of every colour sample was randomised while cleaning. Reported from what the engine did, not from the option: not for `--jpeg-lossless`, 1-bit or floating-point images, and *partly scrubbed* when only some frames or modes could be. | That nothing is hidden. Only LSB-replacement payloads are destroyed. |
| **not detected** | The LSB tests ran and found nothing. | Proof of absence. Small payloads, ±1 embedding and every other method can go undetected. |
| **not scanned** | Nothing was tested: `--deep` was not given, or the format has no sample values the tests can read (lossy formats, palette images, 16-bit, too few pixels). The reason is stated. | Anything about the pixels. |

An animation is tested frame by frame (up to 100 megapixels in all; the report says how many frames were covered). A payload hidden only in a later frame is found. With `--deep` while cleaning, a flagged payload that nothing scrubbed is reported as kept, with a warning; the web app marks the file "Payload kept" and withholds the seal.

## JPEG DCT steganography (`--scrub-dct`)

JPEG steganography tools (JSteg, JPHide, OutGuess, steghide, F3/F4/F5, nsF5, MME, J-UNIWARD, UERD, ...) do not touch pixels. They change the quantised DCT coefficients inside the file. `--scrub-lsb` cannot see that, `--jpeg-lossless` copies it untouched, and a re-encode only destroys part of it. Two opt-in levels deal with it; both are **off by default** and neither changes anything for people who do not ask. [`docs/DCT_WORKFLOW.md`](DCT_WORKFLOW.md) has the full workflow as diagrams.

Both are built on imgstrip's own JPEG coefficient codec (`imgstrip.dct`, pure Python and numpy, no new dependency): it reads baseline, extended and progressive Huffman JPEGs of any sampling, and writes one canonical baseline form. A JPEG it cannot represent exactly (arithmetic coding, 12-bit, lossless, hierarchical) is **refused**, never copied with its coefficients intact.

| Level | What it does | Pixels | Closes |
|---|---|---|---|
| `--jpeg-canonical` | Re-serialises the coefficients canonically: optimal Huffman tables, one baseline scan, no restart markers, padding of ones, blocks outside the picture given plain content, tables used by a component written once, canonical component ids. | **Identical** (verified against the original with the decoder, and the coefficients are compared exactly) | Nearly every place a payload can hide that is not a coefficient (a few bits remain: unused quantisation-table entries, the Adobe colour-transform byte and the sampling factors): Huffman table layout and symbol order, unused tables, redundant tokens (an alias of the end-of-block symbol, zero runs spelled out), padding bits, restart markers, progressive scan scripts, data in blocks nobody sees, 16-bit tables that fit in 8, component ids. The output is a pure function of the coefficients, the quantisation tables and the geometry. |
| `--scrub-dct` | The above, after changing the coefficients so that no payload read from their low bit survives. Every coefficient (DC and AC, every component) is grouped into the pairs `{2k, 2k+1}` that a two's-complement LSB flip moves between; the pairs `{-2,-1}` and `{0,1}` become 0 (this also closes the *support* channel of embedders that change zeros), and every other pair is re-drawn with a fresh random low bit, so the output's parity (read either way, two's complement or sign-magnitude) is uniformly random and independent of the input's. | Changed, and reported (below) | Everything above, plus payloads read from coefficient parity or support: sequential LSB replacement (JSteg), keyed random order (steghide-style), keyed stride (OutGuess-style), matrix/syndrome coding with decrement-only changes (F5-style), and Hamming/syndrome-coded ±1 changes over every coefficient, zeros included (J-UNIWARD-style). |

### What it costs

A payload in the coefficients *is* image content, and the embedder's own change is one quantisation step on the coefficients it uses, so destroying it without knowing where it sits means moving the coefficients by about one step too. That is the unavoidable price of the guarantee: it cannot be made free, only made as small as the guarantee allows. Measured against the input, on a photograph at 768 x 576:

| Source quality | `--scrub-dct` PSNR | `--scrub-dct` file size | `--jpeg-canonical` file size |
|---|---|---|---|
| 50 | 33 dB | about 30 % | 84 % |
| 75 | 35 dB | about 30 % | 90 % |
| 90 | 37 to 38 dB | about 46 % | 96 % |
| 95 | 36 to 39 dB | about 50 % | 97 % |

So the scrub is about the picture quality of re-saving the file at a much lower quality, and visibly softens fine texture on close inspection; the report prints the estimated PSNR, and `--dct-min-psnr` makes the run refuse (and write nothing) instead of wrecking a picture. `--jpeg-canonical` costs nothing in pixels and usually makes the file *smaller*.

### What was measured (reference embedders in `tests/dct_embedders.py`)

Bit error rate of each tool's own extractor on 1000 to 3000 embedded bits (0.0 = payload intact, 0.5 = noise), over qualities 50 to 95 and two subsamplings:

| Tool (re-implemented) | `--jpeg-lossless` | default re-encode (q95) | `--jpeg-canonical` | `--scrub-dct` |
|---|---|---|---|---|
| JSteg | 0.00 | about 0.49 | 0.00 | 0.50 |
| steghide-style | 0.00 | about 0.50 | 0.00 | 0.50 |
| OutGuess-style | 0.00 | about 0.49 | 0.00 | 0.50 |
| F5 (matrix embedding, shrinkage) | 0.00 | about 0.48 | 0.00 | 0.50 |
| J-UNIWARD-like (syndrome, changes zeros) | 0.00 | **0.05 at q95**, 0.25 at q75 | 0.00 | 0.50 |

Two things follow. `--jpeg-canonical` alone does not touch a payload in the coefficients, by design. And the default re-encode, which was assumed to protect against DCT steganography, leaves most of a zero-changing payload readable at high quality (an error-correcting code would recover it).

### What is and is not covered

* **Covered, exactly:** LSB replacement of any coefficient subset in any order. A supervised attacker who is given the tool, the key and the position of every bit, and who learns `P(bit | value)` on other images, scores 0.50 on the scrubbed file (and 1.00 on the unscrubbed one, so the test is not blind).
* **Covered in practice:** ±1 changes (LSB matching, F5's decrement, J-UNIWARD-style syndrome coding). The extractor that the tools actually ship reads noise. Such embedders leave a small statistical trace in the pair of the scrubbed value, which only helps an attacker who already knows which coefficients carried bits; the scrub changes the set of non-zero coefficients those tools index into, so that knowledge is gone. A test pins the worst case.
* **Not covered:** watermarks built to survive re-saving (SynthID and similar), payloads carried by the picture itself, ternary or mod-3 embedders, and an attacker designing a payload around this specific scrub with strong error correction (any cleaning that keeps the picture recognisable leaves that channel open).
* The verdict never says "nothing is hidden": the report says what was done and carries the caveat above.

### Verification

Before anything is written the output is re-read with the codec and the coefficients are compared with what was intended; for `--jpeg-canonical` the decoded pixels must equal the original's bit for bit; for `--scrub-dct` the written coefficients must satisfy the scrub's contract (values -2..1 are 0, every other value stays in its pair), the picture must decode, and the measured quality loss must not be wildly worse than predicted. Any failure aborts with nothing written. The codec is also checked against libjpeg: for optimised baseline files with no hidden blocks its output is byte-identical to libjpeg's, and its pixels agree with Pillow/libjpeg on every file tried; ImageMagick and FFmpeg read the rebuilt files in the self-test.

## Camera sensor fingerprint (PRNU)

Every image sensor has a tiny, permanent pattern of pixel-to-pixel sensitivity differences, the photo-response non-uniformity (PRNU). It is not metadata: it is in the pixels, it survives JPEG compression and colour conversion, and anyone who holds other photos from the same camera (or the camera itself) can estimate it and test whether a given picture came from that sensor ([Lukáš, Fridrich & Goljan, 2006](https://doi.org/10.1109/TIFS.2006.873602)). **Plain cleaning does not touch it**: in the simulation below, a cleaned photo is still matched to its camera with a peak-to-correlation energy (PCE) of about 10,000, where the usual threshold is 60.

`--scrub-prnu` (web app: *Disrupt camera fingerprint*) is opt-in, because it changes pixels.

**Why not just add noise?** The match is a correlation over millions of pixels, so noise a viewer cannot see leaves it intact: in the simulation, noise of 2 grey levels moved the PCE from 10,700 to 9,200, and even clearly visible noise (8 levels, PSNR 30 dB) left it at 4,500. The test suite keeps a regression test for this, so the scrub is never "simplified" to noise. A rigid shift or rotation does not work either, because the detector searches over shifts.

**What it does instead** is *desynchronise* the sensor pattern. The detector is a matched filter: it needs every pixel's noise at the position the fingerprint expects. imgstrip moves every part of the picture by a different, smooth, random amount, so no global shift or rotation can line the noise up again:

* the displacement field is smooth (correlation length 28 px at 1 MP) and has a *uniform* distribution of values, which leaves fewer pixels near their original position than a Gaussian field of the same size; what the detector can still see is, to a good approximation, the share of pixels that stayed put;
* the amplitude grows with the fourth root of the pixel count (about 3.8 px typical at 1 MP, 7 px at 12 MP, at most 8 and 16 px): a larger image gives the detector more pixels to work with, so the warp has to be larger in pixels, though not relative to the picture (0.1–0.4 % of the width);
* **edge-parallel**: where a single edge direction dominates (a horizon, a building edge, a doorframe) the motion perpendicular to the edge is removed, so the edge slides along itself instead of bending. On an isolated straight line this cut the mean displacement of the line from 2.5 px to 0.2 px (diagonals: 2.4 px to 0.5 px). Flat and textured regions, where any displacement is invisible and where the fingerprint is easiest to read, move freely;
* one warp is used for every colour channel and every frame of an animation, so there is no colour fringing and nothing flickers. It is drawn from operating-system entropy, so it cannot be reproduced from imgstrip's algorithm alone. It is not secret from an attacker who holds a photo of the same scene, though: see "What this does not show";
* bicubic resampling, with the border pinned so no sample ever leaves the image. 16-bit images keep full precision.

Palette images (GIF, 8-bit PNG), colour-keyed images and images under 48 px on a side carry no usable fingerprint and cannot be resampled without changing their colours; they are left alone and the report says so. `--jpeg-lossless` copies the compressed data bit-for-bit and so cannot be combined with the scrub: the run fails rather than quietly doing nothing. The output is still verified: it decodes to exactly the pixels that were written.

### How well does it work?

Measured against simulated attackers, through the real engine and the default settings. A simulated camera with a multiplicative fingerprint (σ = 1.2 %, shot and read noise, JPEG q92) took photographs of real scenes (the scikit-image sample photos). The attacker estimated the fingerprint from 20–30 photos with the standard wavelet-denoising estimator and tested with PCE; two further attackers searched for local alignment tile by tile (32 and 64 px tiles). A second, unrelated camera is the control.

| Image size | Cleaned only | With `--scrub-prnu` | Other camera (control) |
|---|---|---|---|
| 1 MP | PCE 10,600 | **23** (worst of 20 photos: 30) | 23 (worst 27) |
| 6 MP | PCE 72,600 | **26–28** (worst of 12 photos: 34) | 26 (worst 28) |
| 12 MP | PCE 117,000 | **27–30** (worst of 8 photos: 44) | 30 (worst 32) |

Median PCE (the range is two independent runs); 60 is the usual "same camera" threshold, and every scrubbed photo stayed below it. The two tile-search attackers saw 2–6 % of the excess over the control level that unscrubbed photos show. Along the way the sweeps showed that a Gaussian field needs about 1.7 times the amplitude for the same result, and that an earlier 2.5 px base amplitude left 17 % of 1 MP photos above the threshold.

**What this does not show.** It is a simulation: real sensors have additional structure (demosaicing, per-camera processing) that the model leaves out, and no real-camera corpus was available. An attacker who holds any aligned reference of the same scene (another photo, a thumbnail, the original document) does not need to know imgstrip's algorithm: generic optical-flow registration to that reference restored much of the match in an independent simulation (the PCE of scrubbed photos rose from about 25 to hundreds or thousands, while controls stayed below 30). Do not rely on this scrub when the scene can be photographed again. The independent review also measured about 20 dB PSNR and text shifts of up to roughly 7 px at 1 MP, and found that at about five times the stated signal strength, smooth or upsampled scenes can still match without any registration. At crossings of strong lines and in dense grids (brick, tiles, window frames) the motion cannot vanish without leaving that area aligned, so lines there can still wobble by a few pixels, and fine grain softens slightly where resampling falls between pixels. **Nothing here is a proof**: no tool can show that a fingerprint is gone from one file (there is nothing to compare it with), so `--check` does not report it and the report cannot give the file a seal for it. Treat it as a strong reduction of the standard attack, not as an erasure.

## Content Credentials (C2PA, optional)

For journalists who need to *show* that a published image was sanitised without being altered, imgstrip can sign each cleaned image with a [C2PA](https://c2pa.org) Content Credential. Anyone can check it with standard tools. It is **off unless you ask for it**: a tick box in the web app (*Options → Sign with Content Credentials*), or `--c2pa` on the command line.

```console
pip install 'imgstrip[c2pa]'                       # adds the official C2PA library (c2pa-python)
imgstrip --c2pa-test-identity ~/id-test            # a throw-away TEST certificate to try it with
imgstrip --c2pa --c2pa-cert ~/id-test/test-signer-chain.pem --c2pa-key ~/id-test/test-signer-key.pem \
         --jpeg-lossless photo.jpg                 # -> photo.clean.jpg, signed
imgstrip --c2pa-verify photo.clean.jpg             # what a viewer will make of it
```

```console
$ imgstrip --c2pa-verify photo.clean.jpg
ok       photo.clean.jpg (JPEG)
           credential: Unverified (signed, signer not trusted); signed by Test Newsroom (Test Signer)
           sanitised 2026-10-15; pixels: identical to the file received
           this credential vouches for the processing only, not for the image's origin or authenticity
           the file holds nothing but the picture and this credential
```

### What the credential says

> Metadata and hidden data were removed from this image to protect its source. *The pixel data is identical to the file received.* This credential vouches for that processing only, not for the origin or authenticity of the image.

The second sentence is **measured, not assumed**. imgstrip decodes the file you gave it and the file it produced and compares them:

| Says | When |
|---|---|
| **identical** | The decoded pixels are bit-for-bit those of the original: lossless formats (PNG, lossless WebP, GIF), and JPEGs cleaned with `--jpeg-lossless` or `--jpeg-canonical`. Hidden colour under transparent pixels and unused palette entries do not count as pixels. |
| **equivalent** | Same picture, but re-encoded (JPEG/WebP/AVIF/HEIF at a quality below lossless), rotated by the orientation tag, colour-converted or converted to another format. The measured PSNR is shown to you, and recorded if you choose *full* detail. |
| **altered** | You asked for `--scrub-prnu`, `--scrub-lsb` or `--scrub-dct`, which change pixels on purpose. The credential says so rather than claim otherwise (the last one is recorded as the operation `dct-scrubbed`). |

To be able to say "identical" for a JPEG, use `--jpeg-lossless` (web: *Keep JPEG data bit-exact*). The default re-encodes JPEGs, which also destroys DCT-domain steganography and the camera's quantisation-table fingerprint, so imgstrip does not switch it on for you.

The credential is a standard C2PA manifest (an `org.imgstrip.sanitization` assertion, plus `c2pa.actions` and an ingredient standing for "the image as received", bound to the file by a hash). It records the **date** (day only by default; `--c2pa-date minute` adds the minute, never seconds) and nothing else you did not choose.

### Valid is not the same as trusted

A signature can be cryptographically perfect and still show as **Unverified** to the public, because the viewer does not recognise the signer's certificate authority. imgstrip tells you what a viewer will see, before you publish:

* **Test certificates.** `--c2pa-test-identity` makes a throw-away two-step chain (the C2PA library refuses a single self-signed certificate). Credentials from it are valid but untrusted; the web app shows an amber warning: *"You are using a test/development certificate. The image will be signed, but public viewers will see an 'Unverified' warning. For publication, use a certificate issued by a certificate authority on the C2PA Trust List."*
* **Real certificates.** Use a signing certificate from a certificate authority on the [C2PA Trust List](https://c2pa.org). imgstrip works offline and does not bundle that list, so for such a certificate it says plainly that it cannot check the issuer. Give it a copy of the list (or your own root) with `--c2pa-trust-anchors FILE` and the same credential reads **Trusted**.
* **Your own trust.** An organisation can sign with its own CA and give the public root to its readers; a reader who adds that root sees **Trusted**. Test it with `imgstrip --c2pa-verify --c2pa-trust-anchors root.pem file`.
* **Refused outright:** self-signed, expired, not-yet-valid or CA certificates, keys the format cannot use (RSA under 2048 bits, other curves), and anything the C2PA library itself rejects. The identity is test-signed against a throw-away image first, so you learn this when you load it, not on your tenth file.
* **No trusted timestamp.** A timestamp needs a server on the internet; imgstrip never goes online. Viewers therefore treat every credential as **invalid once the signing certificate expires**, and imgstrip warns you 30 days ahead.

Supported keys: ECDSA P-256/384/521, RSA (PSS) and Ed25519. Certificates in PEM (chain and key together or in two files) or PKCS#12 (`.p12`/`.pfx`).

### How your identity and the source are protected

* **Your private key stays in memory.** It signs through a callback, appears in no report, JSON output, error message or temporary file, and cannot be pickled or copied. Passphrases are never accepted on the command line (they would sit in your shell history and process list): use the prompt, `IMGSTRIP_C2PA_PASSPHRASE`, or `--c2pa-passphrase-file`. In the web app the identity lives in the server's memory until you remove it or stop `imgstrip-web`, the passphrase field is wiped as soon as the request is sent, and the page never keeps the signing switch, the key, the passphrase or the certificate in its storage: after a reload, signing is off again.
* **No byte of the original goes into the credential.** The C2PA library is never given the source file, so nothing from it (names, XMP ids, a camera's own credential, thumbnails) can be copied. The manifest holds a fixed vocabulary; a hidden field would make imgstrip refuse the file. If the *original* carried Content Credentials (say from a camera), they are removed like any other metadata and you are told who signed them. If they declared the image **AI-generated**, the new credential records that: a sanitised copy must not pass as a camera original.
* **It never phones home.** Remote manifests, certificate-status lookups and timestamp servers are off, with a test that proves a hostile file's remote link is not followed (the library's defaults *do* follow it).
* **Fail closed.** The signed file is re-verified before it is written: the inspector must find exactly one new item (the credential); the decoded pixels must equal those of the clean file; every byte of the manifest is parsed by imgstrip's own reader (padding must be zero); the manifest must match field for field what was requested; the signature is verified with a second, independent implementation; and the library must report a valid, hard-bound manifest. Any failure writes nothing.
* **Auditable later, without linking now (optional).** `--c2pa-commit-source` (web: *Make it auditable later*) adds a *keyed* commitment to the original's pixels. Only the private **receipt** you keep (`--c2pa-receipt FILE`, mode 0600) can open it, so someone holding both the published image and the original cannot use the credential to prove they are linked, but you can, later, for an editor or a court:

```console
imgstrip --c2pa-verify --c2pa-original original.jpg --c2pa-receipt receipts.json published.jpg
ok       published.jpg (JPEG)
           ...
           audit: the original matches; pixels identical to the file received
```

The receipt holds the secret salt and the hash of the original: keep it as private as the original, and never publish it. (The web app holds receipts in memory; download them before you stop it.)

### Options

| Option | Meaning |
|---|---|
| `--c2pa` | Sign each cleaned image. Needs `--c2pa-cert`. |
| `--c2pa-cert FILE`, `--c2pa-key FILE` | Your certificate chain (PEM; may also hold the key) or a `.p12`/`.pfx` bundle, and the key if separate. |
| `--c2pa-passphrase-file FILE` | Read the key's passphrase from a file (or set `IMGSTRIP_C2PA_PASSPHRASE`, or type it when asked). |
| `--c2pa-date day\|minute` | How exactly the signing time is recorded (default day). |
| `--c2pa-detail minimal\|full` | `full` also lists what was done to the pixels and the measured fidelity. |
| `--c2pa-commit-source`, `--c2pa-receipt FILE` | Seal an auditable commitment; the private receipt is appended to `FILE`. |
| `--c2pa-trust-anchors FILE` | CA certificates (PEM) to treat as trusted when judging the credential. |
| `--c2pa-verify` | Verify credentials instead of cleaning (`--c2pa-original` + `--c2pa-receipt` also audits). Exit 0 only for a valid imgstrip credential on a file that holds nothing else. |
| `--c2pa-test-identity DIR` | Create a TEST certificate, key and root in `DIR` and exit. |

`--check` also shows a file's credential (a signed file still reports the credential as data beside the picture, and exits 3).

**Formats:** JPEG, PNG, WebP, GIF, AVIF and HEIF. BMP has no place for a manifest and TIFF is refused (the library leaves the old directory orphaned in the file); imgstrip says so and suggests PNG.

**What a credential cannot do.** It vouches for what *you* did to the file, not for who took the picture or whether it is real; it says so in its own text. It names your organisation in the certificate, so it is not for someone who must stay anonymous. It is only as trustworthy as whoever holds the key. The "pixels identical" statement rests on imgstrip's own measurement, which an auditor can repeat with the original and the receipt, and an `altered` credential says honestly that the pixels were changed on purpose.

## Supported formats

| Format | Read | Write (default output) |
|---|---|---|
| JPEG / MPO | ✓ | JPEG |
| PNG / APNG | ✓ (incl. 16-bit) | PNG |
| WebP (lossy, lossless, animated) | ✓ | WebP |
| GIF (animated) | ✓ | GIF |
| TIFF (multi-page, 1/8/16/32-bit, CMYK, palette, tiled, planar) | ✓ | TIFF |
| BMP (incl. RLE, 16-bit, alpha) | ✓ | BMP |
| HEIC / HEIF | ✓ (pillow-heif) | HEIF |
| AVIF | ✓ | AVIF |
| ICO, PSD (8-bit), TGA, QOI, JPEG 2000, PCX, SGI, PNM, ICNS, DDS | ✓ | PNG |

Not supported, with a clear error: camera RAW files (DNG, CR2/CR3, NEF, ARW, …; export them first), 16-bit Lab TIFF, 16-bit PSD, vector formats (EPS/PDF/SVG).

## Limits: what no metadata tool can do

* **A clean scan is not proof.** "Not detected" means the LSB tests found nothing, and "scrubbed" means LSB-replacement payloads were destroyed. Neither says that no hidden message exists. See [the four verdicts](#hidden-data-in-the-pixels-detected-scrubbed-not-detected).
* **What "accounted for" means.** <a id="what-accounted-for-means"></a>The check on every output walks the whole structure of the file, requires every region that should be empty to be empty (row padding, reserved fields, palette entries no pixel uses, colour under transparent pixels, bytes after the end), and decodes it again. It cannot vouch individually for the bytes inside compressed data (deflate, LZW, JPEG entropy data, WebP and AV1/HEVC bitstreams), for JPEG quantisation and Huffman table entries no coefficient uses, for AVIF/HEIF header fields (flags, brands, handler names, codec configuration), or for how a TIFF value is represented. imgstrip's encoders write those from pixels, so nothing from the original reaches them, except in `--jpeg-lossless` mode, which keeps the original's compressed data and tables as they are (`--jpeg-canonical` and `--scrub-dct` rewrite them from the coefficients). `--check` is the same structural walk: it does not look inside compressed picture data or at the contents of structural fields such as PNG `tRNS`/`PLTE`, a BMP colour table or AVIF `ipco` properties, so it is a leak detector, not proof that a file is clean. The test suite measures the rest: see [How it is tested](#how-it-is-tested).
* **Visible content stays.** Faces, reflections, street signs and landmarks reveal more than any tag. imgstrip does not alter what the picture shows.
* **Robust watermarks** designed to survive re-encoding (Digimarc, SynthID, C2PA "soft binding" watermarks and similar) live in the image content itself and can survive. imgstrip removes C2PA *manifests* (the original's Content Credentials), not content-level watermarks. If you opt in to signing, the only credential in the output is the new one it writes.
* **The camera's sensor fingerprint (PRNU) stays** unless you turn on `--scrub-prnu`, and even then its absence cannot be proven. See [Camera sensor fingerprint](#camera-sensor-fingerprint-prnu) for what the scrub does, what it costs and how it was measured.
* **File names** often contain timestamps (`IMG_20240517_133700.jpg`). Use `--random-name`, or the Random file names switch in the web app (off by default).
* **In-place mode does not securely erase** the old data. The original bytes may remain on disk, in SSD wear-levelling areas, snapshots, backups or cloud sync history. Other hard links to the file keep pointing at the original data (imgstrip warns about this).
* Lossy formats (JPEG, lossy WebP, AVIF, HEIF) are re-encoded, which costs a little quality. Use `--jpeg-lossless` for JPEGs if that matters more than the quantisation-table fingerprint and DCT-steganography protection, or `--jpeg-canonical` if you also want the file rebuilt in one canonical form. `--scrub-dct` is the opposite trade: it protects against DCT steganography at a measurable cost in quality.

## How it is tested

* `imgstrip --self-test` builds 21 worst-case fixtures covering every format. Each is stuffed with metadata, thumbnails, trailers, private chunks, palette tricks, hidden alpha data and steganography, and four of them hide data where decoders never look (BMP row padding and palette bytes, unused GIF colour-table entries, TIFF tag padding and colour-map low bytes, the scan parameters and version fields of a JPEG); every item carries a unique canary string. Each fixture is stripped in every mode and to every output format (261 runs). No canary may survive anywhere in the output bytes, and both the built-in inspector and, if installed, `exiftool` must find nothing. With the optional C2PA support installed it also signs every fixture that can carry a credential, checks that the signed file holds exactly one credential and still no canary, that it reads back as valid, and that a changed picture byte is detected (17 more runs).
* The same self-test then asks **readers that share no code with imgstrip or Pillow** what each output shows: a strict PNG reader written for the purpose (only `zlib` and `numpy`; it rejects bad checksums, chunk-order violations, data after `IEND` and unused deflate bytes, and decodes every colour type and bit depth exactly), plus ImageMagick and FFmpeg when installed. They must see the same pixels: identical for lossless data, and within 3 levels of mean difference for JPEG and other lossy formats, whose decoders legitimately differ. The summary says how many outputs each reader examined.
* **Differential tests** (`tests/test_differential.py`). The strict reader is itself tested against ImageMagick's encoder on every PNG colour type and bit depth (1 to 16 bit, palettes, colour keys, every filter) and must reject sixteen kinds of damage, so a reader that cannot fail is not trusted. Files written by *other* encoders, whose pixels are known, are cleaned and must show the same picture to every reader: ImageMagick and FFmpeg PNG (including Adam7 interlacing), TIFF (LZW, Zip, PackBits, tiled, strips, big-endian), BMP (including RLE), lossless WebP, GIFs optimised into disposal-method sub-frames, JPEGs (4:2:0, 4:4:4, progressive, grey) and 16-bit data. So must everything imgstrip accepts from a corrupted file.
* **Does the verifier cover every byte?** (`tests/test_coverage_audit.py`) Bits are flipped one at a time in clean outputs of every format. A flip that changes nothing the picture shows, and that nothing notices (no inspector finding, no decode failure, no decode-based check), marks a byte the verifier does not account for; PNG chunk checksums are repaired after each flip, because otherwise they would "account" for every byte. Such bytes must be on an explicit, reasoned allow-list (padding bits inside compressed streams, table entries no coefficient uses, ...). 38 planted hidden channels (data in BMP row padding, a palette's reserved byte, an unused GIF colour-table entry, the padding of a TIFF tag, a nested box that swallows its siblings, a JFIF version nobody wrote, ...) must each be refused, the engine must refuse to write if its own scrubbing regressed, and switching a check off must make the audit report the hole it covered. Its first run found real gaps, now closed: BMP row padding, palette and header fields; unused palette entries and colour under transparent pixels in lossless outputs; unused colour-table entries in every GIF frame; TIFF tag padding and the discarded low bytes of 16-bit colour maps; AVIF/HEIF boxes claiming to extend "to the end" inside another box; and a BMP V5 embedded profile that was read from the wrong header fields.
* **Steganography verdicts** (`tests/test_stego_verdicts.py`, `tests/test_stego_ui.py`). The four states and their caveats; that "scrubbed" follows what the engine did, never what was asked for (a scrub that cannot apply, such as `--jpeg-lossless`, is not reported as one); that a payload hidden only in a later animation frame is found; that the command line, JSON, web API and credentials agree; and that a real browser shows each state in the right words.
* **JPEG DCT steganography** (`tests/test_dct_*.py`, with `tests/dct_embedders.py` and `tests/jpeg_craft.py`). Five reference embedders (JSteg, steghide-style, OutGuess-style, F5 with matrix embedding and shrinkage, and a J-UNIWARD-like syndrome coder that changes zeros) must be readable by their own extractors before the cleaner is tried and read as noise (bit error rate within 0.08 of 0.5) after `--scrub-dct`, at four qualities and two subsamplings. A supervised attacker given the tool, the key and every carrier position scores 1.00 on the unscrubbed file and at most 0.53 on the scrubbed one for LSB replacement (a looser pinned bound for ±1-matching embedders). A reference JPEG encoder with a knob for every non-coefficient hiding place (another Huffman table, an end-of-block alias, ZRL tails, restart markers, both padding bits, unused tables, 16-bit tables, another frame type and component id, data in blocks outside the picture) must collapse to byte-identical output after `--jpeg-canonical`. The codec is checked against libjpeg (pixel-identical on every variant, byte-identical to libjpeg's own optimised files where no hidden blocks exist), against every sampling layout including 4:4:0, 4:1:1 and ten blocks per MCU, and at the edges: one-pixel and extreme-aspect pictures, extreme coefficient magnitudes, zero runs around the ZRL boundary, restart intervals from one block to longer than the picture, fill bytes, impossible headers, corrupt tables and 12-bit or arithmetic files (refused). Randomised corruption (bit flips, truncation, deleted and inserted bytes) must end in a decoded picture or a clean error, never another exception (tens of thousands of cases were also run outside the suite). A real browser drives the two switches end to end.
* The pytest suite (about 1,300 tests) covers every codec against independent decoders (ImageMagick for 16-bit data), the colour engine against LittleCMS, orientation 1–8, all CLI safety behaviours (no-clobber, atomic in-place writes, symlinks, hard links, FIFOs, collisions, permissions, stdin/stdout) the web app's security gates, round trips and signal handling, the sensor-fingerprint scrub (an *oracle* detector that knows the camera's exact fingerprint must fail to match after the scrub and succeed before it, on every colour mode, 16-bit data, animations and the failure cases), and **fuzzing**: 840 randomly corrupted files must each produce either a verified-clean output or a clean error, never a crash, a memory blow-up or an unverified write.
* The Content Credentials code has 285 tests of its own (skipped when the optional support is not installed). Highlights: every key type (ES256/384/512, RSA-PSS, Ed25519) signs and is verified with a **second, independent implementation** of the signature and the hard-binding hash; a **hostile-SDK harness** crafts credentials with one deliberate flaw each (an extra assertion, a hidden field, a thumbnail, the source's name as the ingredient title, a signature by another chain, different pixels, an EXIF segment slipped in, a double signature) and every one is refused, with a control proving the harness itself is sound; the canary fixtures prove no byte of the original reaches a credential; a local web server proves a hostile file's remote-manifest link is *not* followed (and that the library's defaults would follow it); thousands of randomly corrupted credentials must each give a verdict or a clean error, never a crash; the web app's key, passphrase and signing switch are shown never to reach a response, the disk or the browser's storage, and a real browser drives the toggle, the identity form and a signing run end to end. Eleven deliberate breakages of the key safeguards were each caught by these tests.
* Trial run on the public [exif-samples](https://github.com/ianare/exif-samples) corpus: 104 real files from dozens of cameras and phones, including iPhone HEICs and deliberately broken files. 104 cleaned, 0 failures. Afterwards `exiftool` reported zero non-structural tags across all outputs, and all 16 orientation test images came out identical.

```console
pip install -e '.[test]'
pytest
```
