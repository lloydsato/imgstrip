# imgstrip

`imgstrip` removes **all** metadata and hidden data from images and leaves only the picture. Use it from the command line, or open the local web app with `imgstrip-web`.

Most metadata removers delete the tags they recognise and copy everything else. imgstrip does the opposite: it decodes the image to raw pixels and writes a brand-new file from those pixels alone. By default nothing from the original file is copied except pixel values; the opt-in JPEG modes (`--jpeg-lossless`, `--jpeg-canonical`, `--scrub-dct`) keep the picture's compressed data or coefficients, and a few bits of structure (see below). Before anything is written, every output is checked twice:

1. A strict, format-aware parser walks the whole structure of the new file. Any segment, chunk, box or tag it cannot account for makes the run fail (it fails closed), and so does a place that must be empty but is not: padding, reserved fields, palette entries no pixel uses, colour stored under fully transparent pixels. (It cannot vouch for slack inside compressed data; see [What "accounted for" means](#what-accounted-for-means).)
2. The new file is decoded again and compared with the intended pixels: bit-exact for lossless formats; for lossy ones, within a quality-dependent tolerance on the mean error *and* on the worst 8 x 8 block, so a change to a small part of the picture is caught too (see [Lossy outputs: what the pixel check catches](#lossy-outputs-what-the-pixel-check-catches)).

Where Pillow both wrote the file and decodes it for the second check (PNG, APNG and BMP), the same check runs again with a reader that shares no code with Pillow or with imgstrip's encoders, so a defect common to the encoder and its decoder cannot hide (see [A second, independent reader](#a-second-independent-reader)). If any check fails, nothing is written. `imgstrip --self-test` adds a further opinion: readers that share no code with imgstrip or Pillow must see the same pixels in every output ([how](#how-it-is-tested)).

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

Requires Python 3.11+.

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

**Two panes.** The list of files is on the left and the file you pick fills the pane on the right: a plain-language summary of what it gives away, then identifying metadata, hidden data, colour profile, the pixels and the file's fingerprints. On a screen narrower than 900 pixels the page is one column and the detail opens as a full-screen sheet (Escape or *Back to list* closes it and puts the keyboard back on the row). Arrow keys walk the list.

**Options** holds the same switches as the command line: output format, quality, colour profile handling, keeping JPEG data bit-exact, LSB scrubbing, transparency scrubbing, **disrupting the camera's sensor fingerprint**, orientation, extra PNG compression, and the optional **Sign with Content Credentials** switch (see [below](#content-credentials-c2pa-optional)). The **Deep scan** switch that also searches the pixels for steganography sits in the bar at the bottom, beside the Clean button. **Every switch has an (i) button** that explains it in plain words.

**File fingerprints.** Every file shows its SHA-256 and MD5. Before cleaning they are those of the file as received; after cleaning each row shows a *Before* and an *After* line, and the detail shows both sets in full with copy buttons. They are computed by the server on the exact bytes (the cleaned file's are those of the download), and the API returns them as `fingerprints` (`/api/inspect`) and `before` and `after` (`/api/clean`, each with `sha256`, `md5` and `bytes`). SHA-256 is the proof; MD5 is only there to match older records and tools that still list it, and it can be forged. *Compare with a hash you already have* takes a pasted SHA-256 or MD5 and says whether it matches the file as added or the cleaned file. *Hash list* downloads `imgstrip-hashes.csv` (file, state, before and after SHA-256 and MD5, saved name), built in the page; names that start with `=`, `+`, `-` or `@` get a leading apostrophe so a spreadsheet does not run them.

**Options follow the images in the list.** Once an image is added, the switches that do not apply to it are greyed out and say why (a JPEG-only switch for a PNG, *Quality* for a lossless format, *Smaller PNGs* when nothing is saved as PNG, *Apply orientation* for a picture with no orientation tag, *Colour profile* for one with no profile). A switch that cannot be done for the image is greyed out with a yellow warning triangle and the reason: the pixel scrubs while any of the JPEG switches (*Keep JPEG data bit-exact*, *Scrub JPEG coefficients*, *Rebuild JPEG data canonically*) is on, because those work on the compressed data and not the pixels, *Disrupt camera fingerprint* for an image under 48 pixels on a side or one made of a palette, *Scrub least-significant bits* for 1-bit and 32-bit images, *Clean transparency channel* for an image with no transparency channel (greyed out as not applicable), *Sign with Content Credentials* for TIFF and BMP, *Deep scan* past 40 megapixels, and any format that cannot hold the image's size or colour depth (WebP is limited to 16,383 pixels on a side, for example). A greyed-out switch shows as off and is never sent, so it cannot make a file fail; your choice comes back when the image goes. With several images, a switch stays available if it works for at least one, and a note says which ones it skips (the skipped image's row says so too). Files over the 1 GB upload limit or the 100 megapixel limit are marked "Not supported" before anything is uploaded. The rules live in `static/support.js` and are tested against the engine itself.

| Option | Meaning |
|---|---|
| `--port N` | Port on `127.0.0.1` (default 8765; a free port is used if it is taken). |
| `--no-browser` | Print the link instead of opening the browser. Use it on a computer shared with other users (see below). |
| `--no-sandbox` | Decode uploads in the app's own process instead of a separate, restricted one (see [Decoding in a sandbox](#decoding-in-a-sandbox)). |

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

Originals are never modified unless you pass `--in-place`. Existing outputs are never overwritten unless you pass `--force`. Every file is first written to a temporary file in the destination's folder and flushed to disk, and only then takes its name, so a crash or a full disk never leaves a half-written file, and an existing file is never opened for writing. Without `--force` the name is claimed atomically (a hard link, or a rename that refuses to replace), so a file that appears while imgstrip is writing is not replaced either, and a symbolic link at the destination, even a dangling one, counts as taken. `--force` and `--in-place` replace the destination with a single rename: other hard links keep the old data, and a symbolic link at the destination is replaced, not followed (`-i` resolves a symbolic link to the file it names first). The web app's *Save to folder* and the C2PA receipt and test-identity files use the same code (`imgstrip.fileio`).

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
| `--scrub-lsb` | Randomise the least-significant bit of every sample to destroy LSB steganography. The report says "scrubbed" only if that happened: not with `--jpeg-lossless`, `--jpeg-canonical` or `--scrub-dct`, and "partly" if some frames could not be. |
| `--scrub-alpha` | Destroy data hidden in the transparency (alpha) channel, which `--scrub-lsb` leaves alone wherever alpha is exactly clear or exactly opaque. Alpha within `--alpha-tolerance` levels of fully opaque becomes exactly opaque, alpha near fully clear becomes exactly clear (and loses its colour), and the lowest bit of every partly transparent pixel is randomised. Colour is not touched. Off unless given. See [below](#hidden-data-in-the-transparency-channel---scrub-alpha). |
| `--alpha-tolerance N` | With `--scrub-alpha`: how many levels (of 255) from fully opaque or clear still count as such. 1 to 32, default 5; an even number is rounded up to the next odd one. Giving it without `--scrub-alpha` is a usage error. |
| `--deep` | Also decode the pixels and look for hidden data, with `--check` or while cleaning. A scan that finds nothing is not proof that nothing is hidden. See [below](#hidden-data-in-the-pixels-detected-scrubbed-not-detected). |
| `--scrub-prnu` | Disrupt the camera's sensor fingerprint (PRNU) with a smooth random warp that moves parts of the picture by a few pixels. Not combinable with `--jpeg-lossless`, `--jpeg-canonical` or `--scrub-dct`. See [Camera sensor fingerprint](#camera-sensor-fingerprint-prnu). |
| `--optimize` | Try extra PNG compression strategies (slower, smaller). |
| `--independent-jpeg-check` | Also decode JPEG output with imgstrip's own JPEG decoder, which shares no code with libjpeg, and require it to agree with the written pixels and with libjpeg. Pure Python, about 0.3 s per megapixel, so off unless given. |
| `--no-sandbox` | Decode inputs in imgstrip's own process instead of a separate, restricted one (see [Decoding in a sandbox](#decoding-in-a-sandbox)). Saves time: measured on a two-core test machine, the sandbox adds about 5 to 10 ms for a small picture and about 0.2 s for a 12-megapixel photo (the pixels cross a pipe), plus about 0.2 s once per run to start; a decoder bug that a hostile file triggers then runs with everything the process holds. |
| `--max-pixels N` | Refuse images larger than N pixels (default 150 million since 1.2.5, was 300 million; the web app uses 100 million). Also protects against decompression bombs. See [Memory](#memory) for what a picture of a given size needs. `--jpeg-canonical` and `--scrub-dct` also refuse a picture whose canonical rewrite would need more than about 3 GiB of memory, which only very noisy JPEGs of more than about 130 megapixels reach. |
| `-n`, `--dry-run` | Do all the work in memory, write nothing. |
| `--json` | Machine-readable report. |
| `-j N` | Process N files in parallel. By default a batch of 4 or more files uses one worker per CPU core, but no more than the available memory holds for the batch's largest picture (estimated from the headers of its largest files, see [Memory](#memory)); smaller batches run in-process. `-j 1` forces one at a time; an explicit `-j` is used as given. |
| `-v` / `--quiet` | Show every removed item / only errors. |

Exit status: `0` success (or `--check`: everything clean), `1` at least one file failed, `2` usage error, `3` `--check` found metadata or hidden data.

### Memory

Peak memory of cleaning one picture, in bytes per pixel above what the process held before (`scripts/memory_table.py`, Linux, Pillow 12.3; the decoder worker's peak is over by the time the main process encodes, so the two columns do not add up):

| Picture | 6 MP: main | 6 MP: worker | 24 MP: main | 24 MP: worker |
|---|---|---|---|---|
| JPEG (rotated by its EXIF tag) | 8.1 | 6.0 | 5.2 | 7.5 |
| PNG, 8-bit RGB | 8.9 | 5.9 | 5.3 | 7.5 |
| PNG, 16-bit RGBA | 40.1 | 18.1 | 31.7 | 19.5 |
| GIF, 4 frames (per pixel of all frames) | 13.0 | 7.8 | 15.3 | 8.0 |

So an ordinary 8-bit picture needs about 6 to 9 bytes per pixel at its peak, and a 16-bit one with alpha up to about 40: a 150-megapixel photo about 1.4 GB, a 150-megapixel 16-bit RGBA picture about 6 GB. That is why the default limit came down from 300 to 150 megapixels in 1.2.5 (a 300-megapixel 16-bit picture could need 12 GB); raise it with `--max-pixels` on a machine that has the memory. For the automatic worker count imgstrip assumes 12 bytes per pixel (44 for 16-bit PNG and for TIFF, whose depth the header check does not read) plus about 200 MB per worker, and uses at most three quarters of the memory the system reports as available (Linux `MemAvailable`, Windows `GlobalMemoryStatusEx`, elsewhere half the physical memory). `--jpeg-canonical` and `--scrub-dct` have their own budget (above).

## What gets removed

| Category | Examples |
|---|---|
| Descriptive metadata | EXIF (camera make/model, serial numbers, lens, owner, timestamps, maker notes), GPS location, XMP (including edit history, drone telemetry, Google/Apple camera data), IPTC, Photoshop resources, comments, PNG text chunks, TIFF tags, DPI/print resolution, C2PA/JUMBF content credentials |
| Embedded images | EXIF/JFIF/Photoshop thumbnails (which can show the *uncropped* original), MPO secondary images, depth maps, HDR gain maps, HEIC/AVIF auxiliary and thumbnail items, TIFF preview/thumbnail pages, APNG default images |
| Hidden payloads | Data appended after the end of the image (ZIP/RAR "polyglots", Samsung trailers, aCropalypse remnants of un-truncated earlier versions), unknown/private chunks and boxes, unreferenced bytes inside TIFF/BMP/HEIF, extra data inside the PNG pixel stream, JPEG quantisation and Huffman tables no scan uses, 0xFF fill bytes, padding and stray bytes |
| Hidden pixel data | Colour stored under fully transparent pixels, unused and duplicate palette entries (and palette order), pixels outside an AVIF clean-aperture crop, GIF frame areas outside the canvas |
| Colour metadata | ICC profiles (often naming the device/owner), PNG gAMA/cHRM/sRGB/iCCP. The colours are *converted*, so the picture looks the same. |

## What changes, and what doesn't

* **Orientation** is baked into the pixels. A photo that displayed upright still displays upright, without the tag. Only EXIF/TIFF orientation is honoured; viewers ignore XMP orientation, so imgstrip does too.
* **Colours**: images with a non-sRGB profile (Display P3, Adobe RGB, ProPhoto, CMYK, gray, or PNG gamma/chromaticity chunks) are converted to sRGB with LittleCMS. Profiles that are numerically equivalent to sRGB are dropped without touching pixels. `--icc sanitize` instead keeps the colour space by embedding a profile rebuilt from scratch (colorimetric data only, fixed header, no text, no dates). imgstrip verifies it is colorimetrically identical to the original.
* **Lossless formats** (PNG, GIF, TIFF, BMP, lossless WebP) keep their pixels bit-for-bit (unless you ask for `--scrub-lsb`, `--scrub-alpha` or `--scrub-prnu`, which change pixels on purpose). PNGs may get a smaller, equivalent colour type (for example RGB with ≤ 256 colours becomes a palette image).
* **16-bit images** (PNG, TIFF, PPM/PGM) keep full 16-bit precision, even though Pillow itself truncates 16-bit colour to 8 bits. imgstrip recovers the low bytes with a second decoding pass, and has its own 16-bit PNG/TIFF encoders and colour engine. Output was verified bit-identical against ImageMagick.
* **JPEG** is re-encoded at quality 95 with the original chroma subsampling (for CMYK JPEGs too, since 1.2.5; earlier versions re-encoded them at a quarter of their resolution in three of the four channels). Re-encoding replaces the quantisation-table "fingerprint" of the camera, but it only *degrades* JPEG-domain steganography, it does not remove it: a payload stored in the DCT coefficients stays partly readable (see the measurements below), so use `--scrub-dct` when that is the threat. To avoid any generation loss, use `--jpeg-lossless`: the compressed image data is copied bit-for-bit and everything around it is rebuilt from scratch. It drops quantisation and Huffman tables no scan uses and refuses a file with bytes inside the compressed data that no decoder reads (use `--jpeg-canonical` for such a file). That mode cannot rotate pixels, so a non-default orientation is kept as a minimal one-tag EXIF block, and DCT-domain content is preserved as-is (`--jpeg-canonical` and `--scrub-dct` are the opt-in ways to normalise it). How much of a coefficient payload a re-encode leaves depends on the embedder and the source quality: parity payloads come out close to noise for the tool's own extractor, a payload that changes zeros (J-UNIWARD and similar) mostly survives a re-encode at quality 95, and an independent evaluation that read the payload at its known positions measured bit error rates of 0.37 to 0.45 (0.5 is chance) for covers at quality 75 and 90.
* **Animations** (GIF, APNG, WebP, AVIF) keep all frames, frame timing and loop count. GIFs are written by a custom encoder that keeps every frame's colours exact.
* **Multi-page TIFFs** keep their pages (thumbnail/mask pages are dropped). Pages with more than 8 bits per sample are refused (split them into separate files first), because converting them for a multi-frame file would clip them.
* Output files are new files, so the original file's timestamps, extended attributes (for example macOS "where from" download URLs) and ownership don't carry over. In-place mode keeps only the permission bits.

## What the output keeps

The default mode's guarantee is non-interference up to an equivalence relation: two inputs that show the same pixels and agree on the values below give byte-identical outputs, whatever else differs between them (metadata, how the file was encoded, colour under transparent pixels, an orientation tag the pixels now carry). So the values below are the only way, besides the pixels themselves, for anything about the input to reach the output. Each result names the ones it carried over (`kept` in `--json` and in the web app's response; `-v` prints them). The minimal pairs in `tests/test_equivalence.py` show both halves: inputs that differ outside the relation give the same bytes, and inputs that differ in one kept value give outputs that differ in that value and nothing else.

| Kept value | When | At most | How to remove it |
|---|---|---|---|
| Output format | No `--format` | 3 bits per file | `--format` |
| Chroma subsampling (4:4:4, 4:2:2, 4:2:0) | JPEG to JPEG; AVIF or HEIF output from AVIF, HEIF, JPEG or WebP | 1.6 bits | Convert to a format that does not take it from the input |
| Lossless or lossy | WebP output (lossless input stays lossless) | 1 bit | `--format` to another format |
| CMYK | JPEG and TIFF keep a CMYK picture in CMYK | 1 bit | `--format png` (converted to RGB) |
| 16 bits per sample | PNG and TIFF keep 16-bit samples, even when 8 bits would hold them | 1 bit | `--format jpeg` or another 8-bit format |
| A rebuilt colour profile | `--icc sanitize` only: the colour space, from the input's profile | the profile's colorimetric data | The default `--icc convert` |
| Frames and their order | Animations (GIF, APNG, WebP, AVIF) and TIFF pages | the frame count | These are the picture; a single-frame format keeps the first only |
| Frame durations | Animations | about 16 bits per frame (GIF stores tens of milliseconds) | None: changing them would change the animation |
| Loop count | Animations | 16 bits | None |

Normalised since 1.2.5, so that they no longer carry anything:

* **The colour model follows the pixels, in every format.** An alpha channel is kept only if some pixel is not fully opaque, grey pixels are stored as grey wherever the format has a grey model (PNG, TIFF, BMP, JPEG), and a picture of 256 colours or fewer is stored with a canonical palette in PNG, TIFF and BMP (the pages of a multi-page TIFF share the model of the animation or file they come from). Up to 1.2.4 TIFF, BMP and JPEG kept the input's model, so a grey picture stored as RGB, an opaque alpha channel or a palette made a different output, a channel of 1 to 2 bits per file.
* **Durations** are whole, non-negative milliseconds, and **loop counts** a whole number from 0 to 65535, however the input stored them (an APNG's delay of 1001/10000 s is 100 ms).

The JPEG modes keep more by design, and say so (`kept` names it): `--jpeg-lossless` the compressed data bit for bit, `--jpeg-canonical` the quantised coefficients, `--scrub-dct` the scrubbed coefficients, and all three a non-default orientation as a one-tag EXIF block. Their outputs are not covered by the relation above; see [JPEG DCT steganography](#jpeg-dct-steganography---scrub-dct).

## Decoding in a sandbox

Reading an image means running large decoders written in C (libjpeg, libpng and Pillow's own decoders, libwebp, libtiff, libavif with its AV1 decoders, libheif with libde265, LittleCMS for colour profiles) on bytes that whoever made the file chose. A memory-safety bug in any of them that a hostile file can trigger runs with everything the process holds: the other files of a batch, the web app's session, a Content Credentials signing key. Since 1.2.5 imgstrip therefore reads every input in a separate process (`imgstrip/sandbox.py`), and only its own process encodes, verifies and writes the output, from what the decoding process hands back.

* **A fresh process for every file.** A *zygote* is started from a clean interpreter (`python -I`, with none of the environment's variables but the few a Python needs to start, so no passphrase or token in the environment goes with it), imports the decoders and waits. For each file it forks a worker, so nothing survives from one file to the next, and the main process's memory was never copied into it.
* **What the worker gives up**, before it reads a byte of the input: core dumps; writing data to any file (`RLIMIT_FSIZE` 0); more than 15 minutes of CPU time; on Linux, gaining privileges, and with [Landlock](https://docs.kernel.org/userspace-api/landlock.html) (Linux 5.13 and later), creating, deleting, renaming or truncating any file, running programs, using devices, and attaching to or reading the memory of any process outside it; with Landlock ABI 4 (Linux 6.7) connecting to or listening on TCP ports, and with ABI 6 (6.12) signalling processes or reaching abstract sockets outside it. It cannot read the next request, and it writes only to its own pipe. `imgstrip --self-test` prints which of these apply on your system, and `tests/test_sandbox.py` checks them from inside a worker.
* **What comes back** is pixels (or, for `--jpeg-canonical` and `--scrub-dct`, the quantised DCT coefficients) and a short list of typed values: the formats, the inspection findings, the actions and warnings to report, the frame durations and loop count, a JPEG's chroma subsampling and orientation, and a colour profile that imgstrip rebuilt itself. The stream carries no code: plain JSON plus raw blocks whose sizes follow from the checked JSON, never `pickle`. The main process checks every field against what the decode stage may legitimately produce (types, ranges, the pixel limit, that the output format is the one the options ask for, that a profile is one imgstrip's own builder writes, that a coefficient array fits its block grid) and refuses anything else, before any of it is used. `--jpeg-lossless` copies the input's compressed data by design, so its rebuilt file comes back as bytes, and the verifying decode of that output runs in the sandbox too.
* **Failures are refusals.** A worker that crashes, runs out of memory, hangs (15 minutes) or answers anything malformed fails that one file with a message ("the decoder crashed on this file ... nothing was written"); the rest of the batch goes on. If the sandbox cannot be started at all (for example in a frozen application with no Python interpreter to run), every file fails with a message that names `--no-sandbox`; imgstrip never falls back to decoding in its own process on its own.

The outputs are the same with and without the sandbox, byte for byte, for every fixture in every deterministic mode (`tests/test_sandbox.py`). It costs a fork and the pixels sent over a pipe: measured on a two-core test machine, about 5 to 10 ms for a small picture and about 0.2 s for a 12-megapixel photo, plus about 0.2 s once per run to start the sandbox; a 6-megapixel photo needs about two copies of its pixels in the worker while it is decoded and about four in the main process while it is encoded and verified, and the two peaks do not overlap.

What it does not do: it is not a system-call filter. On Linux without Landlock, and on macOS, a worker exploited to run code could still create empty files, delete or rename the user's files, read them, and use the network with the user's rights, and on Linux without Landlock attach to the main process if the system allows debugging between a user's processes (Yama `ptrace_scope` 0). On Windows, which cannot fork, every file gets a fresh process with none of these limits; only the separation of memory and the checked answer remain. The deep scan, the web app's previews and header facts, and the decoding of inputs to compare pixels for Content Credentials go through the sandbox too; the structural inspection (pure Python) and reading an input's own Content Credentials (the c2pa library, written in Rust) run in the main process.

## A second, independent reader

The second check decodes the output and compares it with the pixels that were written. When the same library wrote the file and decodes it, a defect common to both (a wrong channel order, a mishandled bit depth, a palette mix-up) would make the two agree on a picture that other viewers show differently: a common-mode failure. Since 1.2.5 the output is read a second time, by code that shares nothing with Pillow or with imgstrip's encoders (`imgstrip/refdecode.py`), wherever such a reader exists among imgstrip's own code:

| Output | Second reader | When |
|---|---|---|
| PNG and APNG | The strict chunk walk of the self-test's reference reader, zlib, and the PNG filters written again. Rather than undo each row's filter (a slow per-byte loop in Python), it applies the row's declared filter to the scanlines the intended pixels must have under the file's own header, palette and transparency, and compares the result with the stored bytes. Undoing a filter is a bijection once its type is known, so this holds exactly when the file decodes to the intended pixels. APNG frames are composed with their offsets and dispose and blend operations. | Always (about 1 s for a 12-megapixel PNG) |
| BMP | Headers, palette, row layout and bit fields read directly. | Always |
| JPEG from `--jpeg-lossless`, `--jpeg-canonical`, `--scrub-dct` | imgstrip's own entropy decoder must read the output's coefficients as the input's (bit-exact mode) or as the ones written (the other two), and the canonical form must be a fixed point: writing what was read back gives the very same bytes. | Always (when imgstrip's decoder can read the input) |
| JPEG from the default mode | imgstrip's entropy decoder plus an inverse DCT, chroma upsampling and colour conversion written here; the result must match the written pixels under the same bounds as Pillow's decode, and Pillow's decode within what two decoders may differ by (1.5 levels on average, 3 in the worst block). | With `--independent-jpeg-check` (pure Python, about 0.3 s per megapixel) |
| GIF, TIFF, 16-bit PNG | None needed: imgstrip's own encoders write these, and Pillow, which shares no code with them, reads them back. | |
| WebP, AVIF, HEIF | None exists among imgstrip's dependencies; the check rests on the library that wrote the file. `imgstrip --self-test` adds ImageMagick and FFmpeg where they are installed. | |

Each result lists what verified it (`verified_by` in `--json` and in the web app's response; `-v` prints it), for example `structure, Pillow, imgstrip PNG reader`. The tests in `tests/test_refdecode.py` show that the readers accept every output of every fixture, reject damaged files, agree with libjpeg and with the strict reference reader on every colour type and bit depth, and catch a simulated defect shared by an encoder and Pillow (red and blue swapped on both sides), which Pillow's own comparison misses.

## Lossy outputs: what the pixel check catches

A lossy encoder cannot reproduce the pixels exactly, so for JPEG, lossy WebP, AVIF, HEIF and GIFs that needed more than 256 colours the second check cannot be an equality. Up to 1.2.4 it was a bound on the mean error over the whole picture, `4 + 0.4 x (100 - quality)` levels, and the review of 1.2.4 showed what that lets through (finding F2): a black rectangle over 1 or 2 % of the picture, or 1 % of its pixels replaced, was accepted. Since 1.2.5 the check also bounds the worst 8 x 8 block. A re-encode spreads its error over every block; a local change moves a few blocks a lot. The difference is split the way the codecs store it: luma (BT.601 Y, which every codec keeps at full resolution), chroma (often subsampled, so its bound is looser) and alpha.

The bounds come from `scripts/calibrate_lossy.py`, which cleans a corpus meant to hold the worst cases (natural photographs, grey textures, photographs with Gaussian noise up to sigma 60, uniform white noise, saturated one-pixel colour graphics, gradients, CMYK and alpha gradients) through the real engine at qualities 1 to 100 with the block check off, and records the worst block of every output the mean check accepted. The bound is twice that worst block at the nearest calibrated quality at or below the one used, plus a floor (3 levels luma, 6 chroma, 3 alpha). At the default qualities that is about 10 to 11 levels RMS of luma for JPEG and WebP and 5 to 10 for AVIF and HEIF. The script fails if anything it measures exceeds the bound, so it can be re-run after a codec library changes.

The review's pixel faults at the default quality, applied to the pixels the encoder was given and encoded again with the same settings (three photographs each; `scripts/calibrate_lossy.py` prints this table):

| Fault | 1.2.4 (mean only) | 1.2.5 |
|---|---|---|
| 1 % of the area blacked out | 0 of 12 | 12 of 12 |
| 2 % of the area blacked out | 0 of 12 | 12 of 12 |
| 5 % of the area blacked out | 3 of 12 | 12 of 12 |
| 10 % of the area blacked out | 12 of 12 | 12 of 12 |
| 1 % of the pixels replaced at random | 0 of 12 | 12 of 12 |
| uniform noise of ±2, ±8 or ±16 levels over the whole picture | 0 of 12 each | 0 of 12 each |
| uniform noise of ±32 levels | 12 of 12 | 12 of 12 |

What is still not caught, by design: a small change spread evenly over the whole picture (the noise rows), a change in a block whose content the change hardly alters (a dark rectangle on a dark area), and anything within what a re-encode at that quality is allowed to move a block. The lower the quality, the looser the bound, so at very low qualities the check is weak. The tests in `tests/test_verify_faults.py` pin the faults above and the review's false-refusal check (photographs with Gaussian noise up to sigma 60 at quality 95 and 75 are accepted).

While calibrating, two pre-existing problems surfaced and were fixed: very noisy pictures could not be written at high JPEG qualities at all (Pillow's output buffer for optimised Huffman tables was too small, so the run failed with "broken data stream"; such pictures are now written with standard Huffman tables), and CMYK JPEGs lost resolution in three channels (above).

**A very noisy picture can be refused.** Static, dense film grain and similar pictures are what a lossy codec reproduces worst: at the default quality the re-encode of white noise is off by a mean of about 6 levels, and the bound is 6.0, so some such pictures fail with "re-encoded image deviates too much" (or the 8 x 8 block message). This is the check working as designed, not a fault in the file, and nothing is written. Raising `-q` does not help, because the bound shrinks as the quality rises (and lowering it gets some through only by loosening the check); what does is a lossless output, `--format png` (the file is larger, and the check is then exact). `--jpeg-lossless` also gets a JPEG through, but it keeps the picture's compressed data (see its entry in the options table), so the message does not suggest it.

## Hidden data in the pixels: detected, scrubbed, not detected

`--check` lists everything the container holds beside the pixels. `--deep` also decodes the pixels and looks for:

* colour data under fully transparent pixels,
* unused or duplicated palette entries that hold data,
* **LSB steganography**, estimated with two independent detectors, Sample Pair Analysis (Dumitrescu, Wu & Wang) and Weighted-Stego analysis (Fridrich & Goljan, with the Ker–Böhme predictor). A file is flagged only when both agree. The thresholds were calibrated on 75 real camera photos: the worst clean photo scored 0.117, and every image with a ≥ 20 % simulated payload scored ≥ 0.195. Smaller payloads, or ±1 embedding instead of LSB replacement, can go undetected, and as with any statistical test a false positive is possible.

`--scrub-lsb` replaces the least-significant bit of every colour sample with random noise (a change of at most 1/255, invisible). This destroys any LSB-embedded message in the colour samples regardless of whether it was detected. Fully opaque and fully transparent alpha values are preserved, so a message kept in the transparency channel is **not** covered: use `--scrub-alpha` for that.

A scan can find a payload but can never show there is none, and a scrub covers one family of methods. So every report (command line, `--json`, web app, Content Credentials) says one of four things about the pixels, in the same words, and never more:

| Verdict | Means | Does not mean |
|---|---|---|
| **detected** (likely or possible) | The pixel statistics indicate LSB steganography. | A decoded message. It is evidence and can be a false alarm. A file whose bits were randomised by `--scrub-lsb` reads the same way (random bits look like a full payload), which is why *scrubbed* is a separate state and is never inferred by re-scanning. |
| **scrubbed** | The least-significant bit of every colour sample was randomised while cleaning. Reported from what the engine did, not from the option: not for `--jpeg-lossless`, 1-bit or floating-point images, and *partly scrubbed* when only some frames or modes could be. | That nothing is hidden. Only LSB-replacement payloads are destroyed. |
| **not detected** | The LSB tests ran and found nothing. | Proof of absence. Small payloads, ±1 embedding and every other method can go undetected. |
| **not scanned** | Nothing was tested: `--deep` was not given, or the format has no sample values the tests can read (lossy formats, palette images, 16-bit, too few pixels). The reason is stated. | Anything about the pixels. |

An animation is tested frame by frame (up to 100 megapixels in all; the report says how many frames were covered). A payload hidden only in a later frame is found. With `--deep` while cleaning, a flagged payload that nothing scrubbed is reported as kept, with a warning; the web app marks the file "Payload kept" and withholds the seal.

## Hidden data in the transparency channel (`--scrub-alpha`)

The alpha channel of a PNG, WebP, GIF, TIFF or AVIF is a second picture that viewers never show as such, and `--scrub-lsb` deliberately keeps its two extreme values, 0 (invisible) and 255 (solid), exactly. A message written as 255-versus-254 over the solid parts of a picture, or 0-versus-1 over its clear parts, therefore passes through `--scrub-lsb` in whole or in large part. `--scrub-alpha` closes that, and is **off unless you ask**:

1. alpha within the tolerance of fully opaque (255 down to 255 minus the tolerance) is set to exactly 255;
2. alpha within the tolerance of fully clear (0 up to the tolerance) is set to exactly 0, and the colour under it is dropped, as under any clear pixel;
3. every alpha level in between has its lowest bit randomised. The levels left pair up as (even, odd), so a value never falls back into the part that was snapped.

The default tolerance is 5 levels out of 255 (about 2 %); `--alpha-tolerance N` takes 1 to 32, and an even value is rounded up to the next odd one (the report states the value used). Colour is never changed where the pixel still shows, so the picture looks the same: the largest change to any pixel is the tolerance itself (a pixel that was 98 % solid becomes fully solid at the default). A picture whose alpha is then all 255 is stored without an alpha channel. The report (`"alpha"` in `--json`, a "transparency" line on the command line, the result panel in the web app) says how many pixels changed, and for an image with no transparency channel says "nothing to clean". The option is also available as `Options(scrub_alpha=True, alpha_tolerance=5)`.

What it does **not** cover, as plainly as what it does:

* A message written with a **larger** difference than the tolerance (for example 255 versus 240) is still there; raising `--alpha-tolerance` closes wider channels at the price of changing fainter pixels more. Only flattening the picture onto a background, or saving it without transparency, removes the channel altogether.
* It does not touch colour. Use `--scrub-lsb` as well to randomise the low bits of the colour samples; the two options are independent and can be combined.
* `--check --deep` does not scan the alpha channel; there is no detector for it, so cleaning with `--scrub-alpha` is the only protection.
* JPEG has no transparency, so the option reports "nothing to clean" for it (also under `--jpeg-lossless`, `--jpeg-canonical` and `--scrub-dct`). When a transparent picture is saved as JPEG the transparency is flattened onto white, after the scrub, so a message in the alpha channel cannot reach the JPEG's pixels either.

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
| J-UNIWARD-like (syndrome, changes zeros) | 0.00 | **0.05 at q95**, 0.25 at q75 | 0.00 | 0.50 to 0.53 |

Two things follow. `--jpeg-canonical` alone does not touch a payload in the coefficients, by design. And the default re-encode, which was assumed to protect against DCT steganography, leaves most of a zero-changing payload readable at high quality (an error-correcting code would recover it).

### What is and is not covered

* **Covered, exactly:** LSB replacement of any coefficient subset in any order. A supervised attacker who is given the tool, the key and the position of every bit, and who learns `P(bit | value)` on other images, scores 0.50 on the scrubbed file (and 1.00 on the unscrubbed one, so the test is not blind).
* **Reduced, not closed:** ±1 changes (LSB matching, F5's decrement, J-UNIWARD-style syndrome coding). A tool that picks its carriers among the non-zero coefficients (steghide- and F5-style) reads noise after the scrub, because the scrub changes that set. A receiver who knows the carrier positions anyway (a tool that indexes every coefficient by key, as J-UNIWARD-style syndrome coders do) still learns a little, for a reason the review of 1.2.4 measured (finding F5) and 1.2.5 explains: the scrub sends -2..1 to 0 and keeps every other value non-zero, and a ±1 change at the edge of that zero zone (1 ↔ 2, -2 ↔ -3) decides whether the carrier ends up zero. A carrier at +1 that must hold 1 stays at 1 and is zeroed; one that must hold 0 moves to 2 and survives. Carriers the scrub keeps read as noise; carriers it zeroes read a deterministic 0 that is correlated with the payload. Read at the true positions, the review's ±1 payload has a bit error rate of 0.563 (0.534 to 0.599 over 28 files), and what such a receiver can learn at best from the scrubbed value is about 0.08 bit per carrier on photographs (0.13 on the test suite's noisier covers): enough for a slow channel with error correction, not for a payload. Widening or randomising the zero zone closes it only at a further 1.3 to 2.4 dB of picture loss, so the scrub was left as it is (`scripts/dct_matching_leak.py` reproduces every number; `tests/test_dct_scrub.py` pins the mechanism and the size).
* **Not covered:** watermarks built to survive re-saving (SynthID and similar), payloads carried by the picture itself, ternary or mod-3 embedders, and an attacker designing a payload around this specific scrub with strong error correction (the zero-zone edge above is one such channel; any cleaning that keeps the picture recognisable leaves some channel open).
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
| **altered** | You asked for `--scrub-prnu`, `--scrub-lsb`, `--scrub-alpha` or `--scrub-dct`, which change pixels on purpose. The credential says so rather than claim otherwise (recorded as the operations `lsb-scrubbed`, `alpha-scrubbed` and `dct-scrubbed`; `alpha-scrubbed` appears only when the scrub changed at least one alpha value). |

To be able to say "identical" for a JPEG, use `--jpeg-lossless` (web: *Keep JPEG data bit-exact*). The default re-encodes JPEGs, which replaces the camera's quantisation-table fingerprint and degrades (but does not remove) DCT-domain steganography, so imgstrip does not switch `--jpeg-lossless` on for you.

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
* **What "accounted for" means.** <a id="what-accounted-for-means"></a>The check on every output walks the whole structure of the file, requires every region that should be empty to be empty (row padding, reserved fields, palette entries no pixel uses, colour under transparent pixels, bytes after the end, JPEG tables that no component or scan uses or that are redefined before use, and 0xFF fill bytes before markers or inside the compressed data), and decodes it again. It cannot vouch individually for the bytes inside compressed data (deflate, LZW, JPEG entropy data, WebP and AV1/HEVC bitstreams), for JPEG quantisation and Huffman table entries no coefficient uses, for AVIF/HEIF header fields (flags, brands, handler names, codec configuration), or for how a TIFF value is represented. imgstrip's encoders write those from pixels, so nothing from the original reaches them, except in `--jpeg-lossless` mode, which keeps the original's compressed data and tables as they are (`--jpeg-canonical` and `--scrub-dct` rewrite them from the coefficients). `--check` is the same structural walk: it does not look inside compressed picture data or at the contents of structural fields such as PNG `tRNS`/`PLTE`, a BMP colour table or AVIF `ipco` properties, so it is a leak detector, not proof that a file is clean. The test suite measures the rest: see [How it is tested](#how-it-is-tested).
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
* The pytest suite (about 2,000 tests) covers every codec against independent decoders (ImageMagick for 16-bit data), the colour engine against LittleCMS, orientation 1–8, all CLI safety behaviours (no-clobber, atomic in-place writes, symlinks, hard links, FIFOs, collisions, permissions, stdin/stdout) the web app's security gates, round trips and signal handling, the sensor-fingerprint scrub (an *oracle* detector that knows the camera's exact fingerprint must fail to match after the scrub and succeed before it, on every colour mode, 16-bit data, animations and the failure cases), and **fuzzing**: 840 randomly corrupted files must each produce either a verified-clean output or a clean error, never a crash, a memory blow-up or an unverified write.
* **Coverage-guided fuzzing** (`fuzz/`, since 1.2.5). Atheris drives eight targets: the structural walk (must never raise), the bit-exact JPEG rebuild, imgstrip's coefficient reader and canonical writer (whatever it reads it must write, and the result must read back to the same coefficients and bytes), the ICC sanitiser (its output must be one the sandbox accepts, and a fixed point), the JUMBF and CBOR parsers, the main process's reading of a sandbox worker's answer (anything but a refusal is a bug, since a hijacked worker writes those bytes), Pillow's decoders, and the whole engine (a verified clean file or a refusal). CI runs each for a minute on every push and for half an hour nightly, and fuzzes the decoders through a Pillow built with AddressSanitizer and UndefinedBehaviorSanitizer (only Pillow's own C code is instrumented; the system's libjpeg, libpng and the like are not, so memory errors inside them are caught only where the sanitiser intercepts the call). The first runs found two bugs: a truncated JPEG segment length raised the wrong kind of error, and a colour profile with an out-of-range rendering intent made a whole file fail instead of falling back to the default intent; a third report was the engine target's own check being too strict (a damaged BMP header that turns trailing bytes into pixels the input itself shows). All three inputs are kept in `fuzz/regressions/`. Writing the worker-answer target also showed that deeply nested or type-confused answers could make the main process raise something other than a refusal; they are refused now. `tests/test_fuzz_targets.py` runs every target on its seeds, on mutations of them and on those regression inputs, without Atheris.
* The Content Credentials code has 285 tests of its own (skipped when the optional support is not installed). Highlights: every key type (ES256/384/512, RSA-PSS, Ed25519) signs and is verified with a **second, independent implementation** of the signature and the hard-binding hash; a **hostile-SDK harness** crafts credentials with one deliberate flaw each (an extra assertion, a hidden field, a thumbnail, the source's name as the ingredient title, a signature by another chain, different pixels, an EXIF segment slipped in, a double signature) and every one is refused, with a control proving the harness itself is sound; the canary fixtures prove no byte of the original reaches a credential; a local web server proves a hostile file's remote-manifest link is *not* followed (and that the library's defaults would follow it); thousands of randomly corrupted credentials must each give a verdict or a clean error, never a crash; the web app's key, passphrase and signing switch are shown never to reach a response, the disk or the browser's storage, and a real browser drives the toggle, the identity form and a signing run end to end. Eleven deliberate breakages of the key safeguards were each caught by these tests.
* Trial run on the public [exif-samples](https://github.com/ianare/exif-samples) corpus: 104 real files from dozens of cameras and phones, including iPhone HEICs and deliberately broken files. 104 cleaned, 0 failures. Afterwards `exiftool` reported zero non-structural tags across all outputs, and all 16 orientation test images came out identical.

```console
pip install -e '.[test]'
pytest
```
