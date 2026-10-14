# imgstrip

`imgstrip` is a command-line tool that removes **all** metadata and hidden data from images and leaves only the picture.

Most metadata removers delete the tags they recognise and copy everything else. imgstrip does the opposite: it decodes the image to raw pixels and writes a brand-new file from those pixels alone. Nothing from the original file is copied except pixel values. Before anything is written, every output is checked twice:

1. A strict, format-aware parser walks every byte of the new file. Any segment, chunk, box, tag or byte it cannot account for makes the run fail (it fails closed).
2. The new file is decoded again and compared with the intended pixels: bit-exact for lossless formats, within a quality-dependent tolerance for lossy ones.

If either check fails, nothing is written.

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
imgstrip --self-test     # proves the installation works end to end
```

## Usage

```console
imgstrip photo.jpg                     # writes photo.clean.jpg next to it
imgstrip -o clean.png photo.png        # choose the output file
imgstrip -r ~/Pictures -o ~/clean      # whole folder tree, mirrored into ~/clean
imgstrip -i *.png                      # replace originals in place (atomic rename)
imgstrip --random-name -o out/ *.jpg   # also hide the original file names
imgstrip IMG_1234.heic --format jpeg   # convert while cleaning
cat in.webp | imgstrip - > out.webp    # stdin -> stdout

imgstrip --check photo.jpg             # report what a file contains, change nothing
imgstrip --check --deep image.png      # also look for data hidden in the pixels
```

Originals are never modified unless you pass `--in-place`. Existing outputs are never overwritten unless you pass `--force`.

| Option | Meaning |
|---|---|
| `-o PATH` | Output file (single input) or directory (several inputs, `-r`). `-` = stdout. |
| `-i`, `--in-place` | Replace the originals atomically, keeping their permission bits. |
| `-r` | Recurse into directories. Hidden directories are skipped. |
| `--format FMT` | Output format: `jpeg png webp gif tiff bmp avif heif`. Default: same as input. |
| `-q N` | Quality for lossy output (JPEG/WebP default 95, AVIF/HEIF 90). |
| `--jpeg-lossless` | Strip JPEGs without re-encoding (see below). |
| `--icc convert\|sanitize\|drop` | Colour profiles: convert pixels to sRGB (default), embed a rebuilt anonymous profile, or discard. |
| `--no-auto-orient` | Don't rotate pixels according to EXIF orientation. |
| `--scrub-lsb` | Randomise the least-significant bit of every sample to destroy LSB steganography. |
| `--optimize` | Try extra PNG compression strategies (slower, smaller). |
| `--max-pixels N` | Refuse images larger than N pixels (default 300 million). Also protects against decompression bombs. |
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
* **Lossless formats** (PNG, GIF, TIFF, BMP, lossless WebP) keep their pixels bit-for-bit. PNGs may get a smaller, equivalent colour type (for example RGB with ≤ 256 colours becomes a palette image).
* **16-bit images** (PNG, TIFF, PPM/PGM) keep full 16-bit precision, even though Pillow itself truncates 16-bit colour to 8 bits. imgstrip recovers the low bytes with a second decoding pass, and has its own 16-bit PNG/TIFF encoders and colour engine. Output was verified bit-identical against ImageMagick.
* **JPEG** is re-encoded at quality 95 with the original chroma subsampling. Re-encoding also destroys JPEG-domain steganography and the quantisation-table "fingerprint" of the camera. To avoid any generation loss, use `--jpeg-lossless`: the compressed image data is copied bit-for-bit and everything around it is rebuilt from scratch. That mode cannot rotate pixels, so a non-default orientation is kept as a minimal one-tag EXIF block, and DCT-domain content is preserved as-is.
* **Animations** (GIF, APNG, WebP, AVIF) keep all frames, frame timing and loop count. GIFs are written by a custom encoder that keeps every frame's colours exact.
* **Multi-page TIFFs** keep their pages (thumbnail/mask pages are dropped).
* Output files are new files, so the original file's timestamps, extended attributes (for example macOS "where from" download URLs) and ownership don't carry over. In-place mode keeps only the permission bits.

## Looking for hidden data: `--check --deep`

`--check` lists everything the container holds beside the pixels. `--deep` also decodes the pixels and looks for:

* colour data under fully transparent pixels,
* unused or duplicated palette entries that hold data,
* **LSB steganography**, estimated with two independent detectors, Sample Pair Analysis (Dumitrescu, Wu & Wang) and Weighted-Stego analysis (Fridrich & Goljan, with the Ker–Böhme predictor). A file is flagged only when both agree. The thresholds were calibrated on 75 real camera photos: the worst clean photo scored 0.117, and every image with a ≥ 20 % simulated payload scored ≥ 0.195. Smaller payloads, or ±1 embedding instead of LSB replacement, can go undetected, and as with any statistical test a false positive is possible.

`--scrub-lsb` replaces the least-significant bit of every colour sample with random noise (a change of at most 1/255, invisible). This destroys any LSB-embedded message regardless of whether it was detected. Fully opaque and fully transparent alpha values are preserved.

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

* **Visible content stays.** Faces, reflections, street signs and landmarks reveal more than any tag. imgstrip does not alter what the picture shows.
* **Robust watermarks** designed to survive re-encoding (Digimarc, SynthID, C2PA "soft binding" watermarks and similar) live in the image content itself and can survive. imgstrip removes C2PA *manifests*, not content-level watermarks.
* **File names** often contain timestamps (`IMG_20240517_133700.jpg`). Use `--random-name`.
* **In-place mode does not securely erase** the old data. The original bytes may remain on disk, in SSD wear-levelling areas, snapshots, backups or cloud sync history. Other hard links to the file keep pointing at the original data (imgstrip warns about this).
* Lossy formats (JPEG, lossy WebP, AVIF, HEIF) are re-encoded, which costs a little quality. Use `--jpeg-lossless` for JPEGs if that matters more than the camera-fingerprint and DCT-steganography protection.

## How it is tested

* `imgstrip --self-test` builds 17 worst-case fixtures, one per format. Each is stuffed with metadata, thumbnails, trailers, private chunks, palette tricks, hidden alpha data and steganography, and every item carries a unique canary string. Each fixture is stripped in every mode and to every output format (189 runs). No canary may survive anywhere in the output bytes, and both the built-in inspector and, if installed, `exiftool` must find nothing.
* The pytest suite (253 tests) covers every codec against independent decoders (ImageMagick for 16-bit data), the colour engine against LittleCMS, orientation 1–8, all CLI safety behaviours (no-clobber, atomic in-place writes, symlinks, hard links, FIFOs, collisions, permissions, stdin/stdout) and **fuzzing**: 680 randomly corrupted files must each produce either a verified-clean output or a clean error, never a crash, a memory blow-up or an unverified write.
* Trial run on the public [exif-samples](https://github.com/ianare/exif-samples) corpus: 104 real files from dozens of cameras and phones, including iPhone HEICs and deliberately broken files. 104 cleaned, 0 failures. Afterwards `exiftool` reported zero non-structural tags across all outputs, and all 16 orientation test images came out identical.

```console
pip install -e '.[test]'
pytest
```
