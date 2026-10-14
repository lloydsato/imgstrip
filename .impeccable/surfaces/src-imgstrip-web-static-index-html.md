---
version: 1
slug: "src-imgstrip-web-static-index-html"
primary_target: "src/imgstrip/web/static/index.html"
related_targets: []
---

# imgstrip web app (main window)

Scope: the single screen of the local web app (`imgstrip-web`), served from `src/imgstrip/web/static/`. Visitor mode: **Operate**. Build path: code-led (Operate surface).

## Audience and job

Journalists and privacy-focused individuals about to publish, send or hand over images. Job: drop images, see precisely what each file reveals (scanned on arrival), clean on request, take the verified clean files back as a download, a ZIP, or written into a folder on disk.

## Constraints

- User-pinned character: mini, premium-feeling, simple. This brief replaced the dealt direction hand (the user rejected all four cards and wrote this steer); the roll does not bind here.
- Scan first; nothing is cleaned until the user presses Clean.
- Options exposed: output format, quality, lossless JPEG, colour profile, auto-orient, LSB scrub, extra PNG compression, deep scan. Not exposed: self-test, random names, in-place.
- Stack: Python stdlib server + plain HTML/CSS/JS, no build step, offline, fonts self-hosted.

## Direction contract

THESIS: The whole tool is one small, exact window, an instrument that fits in a glance. It refuses the category default: a page-wide dashed drop zone, a cloud-upload icon and a marketing headline above a list of check marks.

OWN-WORLD: A cool mineral ground (pale grey-green by day, deep ink-green by night, following the system), one compact panel on it, hairline rules, Schibsted Grotesk (a newsroom-born grotesk) for every word, tabular figures for counts (sizes stay proportional: the face's tabular period is too wide), system mono only for real values (coordinates, hashes, bytes). Ink green-black text; vermilion only for what a file reveals; mint/green only for verified clean. One reversed ink plate per state: the single primary action.

STORY: The reporter drops a source's photos. Within a second each row says what it gives away (camera, place, time, a hidden thumbnail). They open one to see every item, press Clean, and watch each row earn its seal. They take the ZIP or save to a folder, certain nothing but pixels leaves.

FIRST VIEWPORT: At 1440x900 a ~640px window sits centred on the tinted ground, upper third. Header: wordmark with seal glyph left; a "Local only" status and an Options button right. Body: a compact drop well (about 200px) with "Drop images to scan", one honest line, and Choose files; a formats line below. Footer: the primary plate, absent until files exist. With files: dense 56px rows (thumbnail, name, format/size, what it reveals), expanding inline; footer carries "Clean N files", then "Download all" and "Save to folder".

FORM: User-pinned brief (mini, premium, simple) in place of the dealt hand; not a position on the seven-candidate list. Kept from the hand: the engraved seal (from the security-print candidate) as the one signature move, one reversed plate per screen, packed numbered density. seed: none (unavailable). The roll ran on impeccable 0.1.14, which prints no seed reference, and the user replaced its hand with the pinned brief.

Signature interaction: each verified file earns a small guilloché seal engraved from the clean file's SHA-256 (petal count, ring radii and phase come from the hash), drawn once in fine strokes as verification passes; focus or hover shows the two checks and the hash. Motion grammar: 180ms exponential ease-out for expand and state; a 1px sweep under a row while it scans; the seal draws over 600ms with rings staggered; reduced motion shows final states instantly.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## Unresolved

- None blocking. Theme follows the operating system (no manual toggle, by simplicity).
