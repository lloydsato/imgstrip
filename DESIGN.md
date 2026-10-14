---
name: imgstrip
description: One small, exact window that shows what an image gives away, cleans it, and seals the proof.
colors:
  ground: "#e5f0eb"
  panel: "#fbfdfc"
  sunken: "#eff6f2"
  hover: "#ebf2ef"
  line: "#d7dfdb"
  line-strong: "#c0cac5"
  field-line: "#7c8883"
  ink: "#13221d"
  ink-2: "#45544e"
  ink-3: "#5f6c67"
  leak: "#c33315"
  leak-bg: "#ffebe5"
  leak-line: "#fbc1b2"
  clean: "#157c4f"
  clean-bg: "#def8e7"
  clean-line: "#a8dcbd"
  warn: "#9a600b"
  plate: "#13221d"
  plate-hover: "#23352f"
  plate-ink: "#f8fbf9"
  focus: "#00858d"
  selection: "#9be0db"
  ground-dark: "#0e1915"
  panel-dark: "#17231f"
  sunken-dark: "#131e1a"
  hover-dark: "#1f2b27"
  line-dark: "#283530"
  line-strong-dark: "#36433e"
  field-line-dark: "#66746e"
  ink-dark: "#e8eeeb"
  ink-2-dark: "#b5c1bb"
  ink-3-dark: "#95a29c"
  leak-dark: "#fd8868"
  leak-bg-dark: "#43241c"
  leak-line-dark: "#723c2d"
  clean-dark: "#76e1a7"
  clean-bg-dark: "#183525"
  clean-line-dark: "#27593e"
  warn-dark: "#f7c15f"
  plate-dark: "#e6ede9"
  plate-hover-dark: "#f8fdfa"
  plate-ink-dark: "#121d1a"
  focus-dark: "#57d4d4"
  selection-dark: "#085955"
typography:
  wordmark:
    fontFamily: "Schibsted Grotesk, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "1.125rem"
    fontWeight: 650
    lineHeight: 1
    letterSpacing: "-0.015em"
  title:
    fontFamily: "Schibsted Grotesk, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "1.125rem"
    fontWeight: 650
    lineHeight: 1.45
    letterSpacing: "-0.01em"
  body:
    fontFamily: "Schibsted Grotesk, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 400
    lineHeight: 1.45
  body-sm:
    fontFamily: "Schibsted Grotesk, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "0.84375rem"
    fontWeight: 400
    lineHeight: 1.45
  control:
    fontFamily: "Schibsted Grotesk, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "0.84375rem"
    fontWeight: 560
    lineHeight: 1.3
  heading:
    fontFamily: "Schibsted Grotesk, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "0.84375rem"
    fontWeight: 650
    lineHeight: 1.45
  label:
    fontFamily: "Schibsted Grotesk, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 600
    lineHeight: 1.45
  caption:
    fontFamily: "Schibsted Grotesk, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 400
    lineHeight: 1.35
  mono:
    fontFamily: "ui-monospace, SF Mono, Cascadia Mono, Menlo, Consolas, monospace"
    fontSize: "0.75rem"
    fontWeight: 400
    lineHeight: 1.55
  mono-hash:
    fontFamily: "ui-monospace, SF Mono, Cascadia Mono, Menlo, Consolas, monospace"
    fontSize: "0.6875rem"
    fontWeight: 400
    lineHeight: 1.5
rounded:
  window: "14px"
  well: "10px"
  control: "8px"
  thumb: "6px"
  pill-count: "11px"
  pill-switch: "9px"
  pill-drop: "18px"
spacing:
  hair: "2px"
  xs: "4px"
  tight: "6px"
  sm: "8px"
  md: "12px"
  inset: "14px"
  gutter: "16px"
  lg: "18px"
  indent: "56px"
components:
  window:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.window}"
    width: "min(680px, 100% - 32px)"
  window-bar:
    height: "56px"
    padding: "0 12px 0 18px"
  button-plate:
    backgroundColor: "{colors.plate}"
    textColor: "{colors.plate-ink}"
    typography: "{typography.control}"
    rounded: "{rounded.control}"
    height: "36px"
    padding: "0 18px"
  button-plate-hover:
    backgroundColor: "{colors.plate-hover}"
  button-plate-disabled:
    backgroundColor: "{colors.line}"
    textColor: "{colors.ink-2}"
  button-secondary:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    typography: "{typography.control}"
    rounded: "{rounded.control}"
    height: "36px"
    padding: "0 14px"
  button-secondary-hover:
    backgroundColor: "{colors.hover}"
  button-quiet:
    backgroundColor: "transparent"
    textColor: "{colors.ink-2}"
    typography: "{typography.control}"
    rounded: "{rounded.control}"
    height: "32px"
    padding: "0 10px"
  button-quiet-hover:
    backgroundColor: "{colors.hover}"
    textColor: "{colors.ink}"
  button-quiet-open:
    backgroundColor: "{colors.sunken}"
    textColor: "{colors.ink}"
  button-icon:
    textColor: "{colors.ink-3}"
    rounded: "{rounded.control}"
    size: "32px"
  button-icon-hover:
    backgroundColor: "{colors.hover}"
    textColor: "{colors.ink}"
  field:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.control}"
    height: "36px"
    padding: "0 32px 0 11px"
  switch-track:
    backgroundColor: "transparent"
    rounded: "{rounded.pill-switch}"
    width: "30px"
    height: "18px"
  switch-track-on:
    backgroundColor: "{colors.ink}"
  drop-well:
    backgroundColor: "{colors.sunken}"
    textColor: "{colors.ink}"
    rounded: "{rounded.well}"
    padding: "22px 24px 20px"
  file-row:
    rounded: "{rounded.control}"
    height: "60px"
    padding: "8px 6px 8px 4px"
  file-row-hover:
    backgroundColor: "{colors.hover}"
  thumb:
    backgroundColor: "{colors.sunken}"
    rounded: "{rounded.thumb}"
    size: "40px"
  reveal-count:
    backgroundColor: "{colors.leak-bg}"
    textColor: "{colors.leak}"
    typography: "{typography.label}"
    rounded: "{rounded.pill-count}"
    height: "22px"
    padding: "0 8px"
  reveals-panel:
    backgroundColor: "{colors.leak-bg}"
    textColor: "{colors.ink}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.control}"
    padding: "12px 14px"
  verified-panel:
    backgroundColor: "{colors.clean-bg}"
    textColor: "{colors.ink}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.control}"
    padding: "14px"
  verified-panel-partial:
    backgroundColor: "{colors.sunken}"
  seal-row:
    textColor: "{colors.clean}"
    size: "24px"
  seal-verified:
    textColor: "{colors.clean}"
    size: "64px"
  brand-mark:
    textColor: "{colors.ink}"
    size: "24px"
  well-mark:
    textColor: "{colors.ink-3}"
    size: "32px"
  seal-tip:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "9px 11px"
  drop-pill:
    backgroundColor: "{colors.plate}"
    textColor: "{colors.plate-ink}"
    rounded: "{rounded.pill-drop}"
    padding: "8px 14px"
---

# Design System: imgstrip

## Overview

**Creative North Star: "The Exact Window"**

imgstrip is one small, exact window set on a cool mineral ground: an instrument that fits in a glance. A single panel carries the whole tool, from the drop well to the verified files, and nothing sits outside it except a version line and the honest list of what cleaning can't do. The world turns down the category's default of a page-wide dashed drop zone under a cloud-upload icon. Dropping a file shows as a quiet teal ring on the window and a small ink pill near the bottom of the viewport.

Type is a newsroom grotesk at small sizes, green-black ink on near-white, with parts separated by hairlines and tonal fills rather than boxes and shadows. Colour is held back for meaning. Vermilion is what a file gives away, green is what has been verified clean, amber is what did not go to plan, and teal is the instrument itself (focus, the scan sweep, the drag ring). Every state has exactly one reversed ink plate, and it is the next action. Density is packed but legible: 56px bar, rows of at least 60px, 36px controls.

The signature is the engraved seal: a guilloché rosette computed from the clean file's SHA-256, so the same file always earns the same seal. It is drawn stroke by stroke when verification passes. Drawn from a fixed hash and without its check mark, it is also the brand mark. Theme follows the operating system with no toggle; the dark world is a deep ink-green, not a grey.

**Key Characteristics:**
- One centred window, at most 680px wide, on a tinted ground; no page chrome.
- One family (Schibsted Grotesk) for every word, 12px to 18px; system mono only for real values.
- Meaning-only colour: vermilion reveals, green verifies, amber warns, teal instruments.
- One reversed ink plate per state; no plate before files exist.
- Flat inside, one lift outside: hairlines and tonal fills inside the window, a soft layered shadow under it.
- A hash-engraved guilloché seal as the single signature move.
- Light and dark as exact token pairs following `prefers-color-scheme`.

## Colors

A cool, low-chroma green-grey world in which only the three meaning colours carry saturation.

### Primary
- **Green-Black Ink** (#13221d light / #e8eeeb dark): all primary text, the wordmark and brand mark, the checked switch track, and `accent-color` for native controls. 16.13:1 on the panel in light, 13.77:1 in dark.
- **Ink Plate** (#13221d, hover #23352f; dark #e6ede9, hover #f8fdfa) carrying **Plate Paper** text (#f8fbf9 / #121d1a): the one reversed action per state, and the drop pill. In dark the plate turns pale with ink text, so it stays the strongest object in the window.

### Secondary
- **Reveal Vermilion** (#c33315 / #fd8868): the text of what a file gives away: the "N to remove" count, the field names in the reveals panel, "N revealing items" in the footer summary.
- **Vermilion Wash** (#ffebe5 / #43241c) and **Vermilion Rule** (#fbc1b2 / #723c2d): the fill and 1px inset edge of the count pill and the reveals panel.

### Tertiary
- **Verified Green** (#157c4f / #76e1a7): seal strokes, check marks, the "Clean" row status, "N clean" in the summary, and the icon on ok notes (nothing found and rebuilt, ZIP downloading).
- **Mint Wash** (#def8e7 / #183525) and **Mint Rule** (#a8dcbd / #27593e): the fill and 1px inset edge of the verified panel.

### Neutral
- **Mineral Ground** (#e5f0eb / #0e1915): the page background only. Pale grey-green by day, deep ink-green by night.
- **Window Paper** (#fbfdfc / #17231f): the window, field and secondary-button fills, item rows inside the detail tables, the seal tooltip.
- **Well Tint** (#eff6f2 / #131e1a): the drop well and access gate, the options tray, thumbnails, an open quiet button, the partial-verification panel.
- **Hover Wash** (#ebf2ef / #1f2b27): hover on rows and on quiet, secondary and icon buttons; the well while a drag is over the page.
- **Hairline** (#d7dfdb / #283530): every 1px rule (bar, list, rows, options, footer), the well border, the gaps in the item tables, and the disabled plate fill.
- **Strong Hairline** (#c0cac5 / #36433e): the tooltip border, link underlines, the scrollbar thumb.
- **Field Edge** (#7c8883 / #66746e): the 1px border of selects, the folder input and secondary buttons (3.60:1 and 3.31:1 against the panel).
- **Second Ink** (#45544e / #b5c1bb): supporting text: well sub line, field labels, list count, footer summary, quiet buttons, the "Local only" status.
- **Quiet Ink** (#5f6c67 / #95a29c): the quietest text and icons: file sub lines, meta lines, hints, the formats line, chevrons, mono detail values, the colophon. Still 5.37:1 (light) and 6.11:1 (dark) on the panel.

### Signal
- **Caution Amber** (#9a600b / #f7c15f): outcomes that did not go to plan or need the user before cleaning can succeed: "Not scanned", "Not cleaned", "Damaged", "Payload in pixels", "Payload kept", "N not cleanable", an invalid folder border, the armed Clear button, warn and error notes.
- **Instrument Teal** (#00858d / #57d4d4): the focus ring, the text caret, a focused field's border, the 1px scan sweep and the 2px drag ring.
- **Teal Selection** (#9be0db / #085955): text selection, with ink text on top.

### Named Rules
**The Two Signals Rule.** Vermilion marks only what a file reveals; green marks only verified-clean output. Neither colour decorates anything else, and neither appears in chrome, buttons or the brand mark.

**The One Plate Rule.** Each state carries exactly one reversed ink plate: the next action (Clean N images, Download, or Save while the folder row is open, when the footer action steps down to secondary). The empty state has no plate at all.

**The Paired Theme Rule.** Every colour is a light and dark pair under one custom property name; dark mode redefines `:root` under `prefers-color-scheme: dark` and nothing else. A new colour is not done until both halves exist.

## Typography

**Display Font:** Schibsted Grotesk (with ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif), self-hosted variable woff2, weights 400 to 900, latin and latin-ext subsets, OFL
**Body Font:** Schibsted Grotesk (same stack)
**Label/Mono Font:** the system monospace stack (ui-monospace, SF Mono, Cascadia Mono, Menlo, Consolas, monospace), for real values only

**Character:** A newsroom-born grotesk used small and exact, with hierarchy built from weight steps (400, 450, 500, 560, 600, 650) and three ink levels rather than from size. The system mono is a plain instrument readout beside it, never a style.

### Hierarchy
- **Wordmark** (650, 18px, line-height 1, -0.015em): "imgstrip", lowercase, beside the brand mark in the bar.
- **Title** (650, 18px, 1.45, -0.01em, balanced wrapping): the well and gate headline. The largest type in the system.
- **Body** (400, 15px, 1.45): the base size on `body`.
- **Body Small** (400, 13.5px, 1.45): the workhorse for running text: the well sub line (max 42ch), notes and limits (max 64ch), reveals and item rows, the checks, the footer summary, select text.
- **Control** (560, 13.5px, 1.3): button labels (line-height 1 inside their fixed height), file names, switch labels, the list count.
- **Heading** (650, 13.5px): group titles in a file's detail ("Removed: Identifying metadata"), with the group count at 500 in quiet ink beside it; reveal field names at 600 in vermilion.
- **Label** (600, 12px): field labels in the options tray and the save row, and the row status ("4 to remove", "Clean").
- **Caption** (400, 12px, 1.35): file sub lines, meta lines, hints, the formats line, the colophon. The seal tooltip uses this size at 450 with line-height 1.4.
- **Mono** (400, 12px, 1.55): item detail values (EXIF fields, coordinates, byte counts). The folder input sets its path in mono at 13px; inline `code` is mono at 0.92em.
- **Mono Hash** (400, 11px, 1.5): SHA-256 lines in the verified panel and the seal tooltip.

### Named Rules
**The One Face Rule.** Schibsted Grotesk sets every word, from the wordmark to the smallest hint. Weight and the three ink steps carry hierarchy; nothing goes above 18px and no second family appears.

**The Real Values Rule.** System mono appears only for actual values a file carries or the engine produces: metadata fields, coordinates, byte counts, hashes, folder paths, file suffixes. Labels and prose stay in the grotesk.

**The Instrument Figures Rule.** Counts use tabular figures: the row's count pill, the list count, group counts and the footer summary. File sizes and dimensions stay proportional, because this face's tabular period is too wide.

## Layout

A single centred column. The stage is `min(680px, 100% - 32px)` wide (a 16px gutter each side), with top padding `clamp(24px, 11vh, 112px)` and 48px below, so the window sits in the upper third of a desktop viewport. The window holds, top to bottom: a 56px bar (18px leading inset, 12px trailing), the options tray (an inline disclosure), the body (14px padding) and a footer (12px by 14px) that exists only once files do. Below the window, centred: the colophon line and the limits disclosure (max 64ch).

Inside the body, the empty well is centred text in a tinted box. With files, a list head (count left, Add and Clear right) sits above rows divided by hairlines. Each row is a grid: 40px thumbnail, the name and sub line (1fr), the status (auto) and an 18px chevron, with 12px gaps and a 60px minimum height; the row's download and remove icon buttons sit outside the expanding head. An open row's detail stacks its parts 14px apart and indents 56px so it starts on the file-name column. Detail tables use a label column of up to 13em; the reveals panel sizes its label column to content; the verified panel is a 64px seal beside the checks.

The options tray is a three-column grid (14px row gap, 16px column gap, 16px 18px 18px padding) with the switches below a hairline in two columns.

Spacing follows a small rhythm: 2px between toolbar icons, 4px and 6px inside stacks, 8px between grouped controls, 12px between row parts, 14px for the window's own insets, 18px for the bar and tray edges. Inline disclosures (options, a row's detail, save to folder, limits) open in place by animating `grid-template-rows` from 0fr to 1fr over 180ms on the expo ease-out; content is set to `visibility: hidden` once closed.

**Responsive.** At 640px and below the stage's top padding drops to 16px, the bar insets to 14px and 6px, the options tray and the switches go to one column, the body padding becomes 10px, and each row turns into two lines: the name takes the full width (wrapping to at most two lines), the status drops beneath it, the row actions stack vertically, and the detail loses its indent. The footer actions go full width with the primary plate first, on its own line. At 359px and below, the "Local only" and "Options" labels are visually hidden and their icons remain. Hover styles apply only under `(hover: hover)`.

The direction contract asked for a window of about 640px and 56px rows; the build settled on 680px and 60px. The build wins, and these are the recorded values.

### Named Rules
**The Glance Width Rule.** The window never grows past 680px, whatever the viewport. More files make it taller, never wider.

**The Name Column Rule.** Everything inside an open row's detail starts on the file-name column (56px: a 4px row inset, the 40px thumbnail and a 12px gap), so the thumbnail column stays clear.

## Elevation & Depth

A hybrid with one lift. The window is the only surface raised off the ground, under a soft three-layer shadow tinted green-black (rgb 30 51 44) (four layers in dark, the first a faint white hairline that defines the edge). Inside the window everything is flat: hairlines, tonal fills (well tint, hover wash, vermilion and mint washes) and 1px inset edges separate parts. Only transient overlays that float above the window carry their own small shadow: the seal tooltip and the drop pill. Dragging files over the page adds a 2px teal ring around the window, outside its shadow.

### Shadow Vocabulary
- **Window lift, light** (`box-shadow: 0 1px 2px rgb(30 51 44 / 0.06), 0 10px 24px -6px rgb(30 51 44 / 0.14), 0 32px 64px -24px rgb(30 51 44 / 0.22)`): the window at rest.
- **Window lift, dark** (`box-shadow: 0 0 0 1px rgb(255 255 255 / 0.03), 0 1px 2px rgb(0 0 0 / 0.3), 0 16px 40px -8px rgb(0 0 0 / 0.5), 0 40px 80px -32px rgb(0 0 0 / 0.6)`): the window at rest in dark.
- **Drag ring** (`box-shadow: 0 0 0 2px var(--focus), var(--window-shadow)`): the window while files are dragged over the page.
- **Tooltip float** (`box-shadow: 0 1px 2px rgb(var(--shadow-rgb) / 0.08), 0 12px 28px -10px rgb(var(--shadow-rgb) / 0.35)`): the seal tooltip.
- **Drop pill float** (`box-shadow: 0 8px 24px -8px rgb(var(--shadow-rgb) / 0.4)`): the "Drop to scan" pill.
- **Inset edge** (`box-shadow: inset 0 0 0 1px var(--line)`, or `--leak-line` / `--clean-line`): the edge of thumbnails, item tables, the count pill and the reveals and verified panels; the switch's off state uses a 1.5px inset in quiet ink.

### Named Rules
**The One Lift Rule.** Only the window lifts off the ground. Nothing inside the window casts a shadow; if a part needs separating, use a hairline or a tonal fill.

## Shapes

Gently rounded, with the radius stepping down as elements nest: the window (14px), the drop well and gate (10px), controls, rows, panels, item tables and the tooltip (8px), thumbnails (6px). Pills are exactly half their height: the count pill (11px on 22px), the switch track (9px on 18px) and the drop pill (18px). Apart from icon details, the only circles are the switch knob and the seal.

Edges are 1px. Hairline rules divide the bar, list, rows and footer; tinted panels draw their edge as a 1px inset shadow rather than a border, so it never shifts layout. Fields and secondary buttons take a solid 1px field-edge border. Nothing is dashed.

Icons come from one inline SVG sprite on a 24 grid: 1.6px strokes, round caps and joins, no fills, drawn at 15px to 18px in `currentColor` (lock, sliders, chevron, download, folder, close, plus, check, alert, copy, info, image). Check marks inside the verified panel use a 2px stroke.

## Components

### Buttons
Compact and confident; the plate is the only filled button.
- **Shape:** gently rounded (8px), 36px tall, 7px gap between icon and label, control type.
- **Plate (primary):** ink plate with plate-paper text, 18px horizontal padding, at least 132px wide. It carries the footer's next action ("Clean N images", "Download", "Download all (.zip)") and the folder row's Save (32px small variant).
- **Secondary:** window-paper fill, 1px field-edge border, ink text, 14px padding: "Choose images", "Save to folder", "Download N", "Scan the pixels too". When the folder row opens, the footer plate becomes secondary so Save is the only plate.
- **Quiet:** no fill, second-ink text, 32px tall, 10px padding: Options, Add, Clear. Open (`aria-expanded="true"`) takes the well tint and ink.
- **Icon:** 32px square, quiet-ink icon: a row's download and remove, copy hash (26px).
- **Hover / Focus:** hover (hover-capable devices only) moves to the hover wash, the plate to its hover shade, the secondary border to quiet ink; 180ms on the expo ease-out. Focus is a 2px instrument-teal outline with a 2px offset. Press nudges the button down 0.5px over 120ms.
- **Disabled and busy:** the plate is never `disabled`; it takes `aria-disabled="true"` so it keeps focus, and shows a hairline fill with second-ink text while waiting. While cleaning it stays an ink plate at 0.86 opacity reading "Cleaning…". Clear arms on first press: amber text on the well tint, and a second press clears.
- **Link:** second-ink text underlined 1px in the strong hairline with a 3px offset; on hover the underline takes the text colour. Used only for the colophon's "What cleaning can't do".

### Chips
- **Reveal count:** a 22px pill (11px radius) in the vermilion wash with a vermilion-rule inset edge and vermilion label text with tabular figures, 8px padding: "4 to remove". The only chip in the system, and only for findings.
- **"Local only" status:** not a chip. A lock icon and 12px, 500-weight second-ink text with no fill or edge, 28px tall, in the bar.

### Cards / Containers
- **Window:** window paper, 14px radius, the window lift; the one container on the ground.
- **Drop well:** well tint, 1px hairline border, 10px radius, padding 22px 24px 20px; centred well mark, title, sub line and secondary button; the formats line sits just below the well. The access gate reuses it without the button. While a drag is over the page its border turns teal and its fill the hover wash.
- **Reveals panel:** vermilion wash with a vermilion-rule inset edge, 8px radius, 12px by 14px padding; a two-column definition list with vermilion 600-weight names and ink values.
- **Verified panel:** mint wash with a mint-rule inset edge, 8px radius, 14px padding; the 64px seal beside two green-checked statements and the full SHA-256 with a copy button. When the structure check passed but a pixel payload was kept, it becomes the partial panel: well tint, hairline edge, quiet-ink checks and no seal.
- **Item tables:** groups of rows on window paper separated by 1px hairline gaps inside an 8px-radius hairline edge; label at 500, detail value in mono quiet ink.
- **Shadow strategy:** see Elevation & Depth; only the window lifts.

### Inputs / Fields
- **Style:** 36px tall, window-paper fill, 1px field-edge border, 8px radius, 13.5px text. Selects drop the native arrow for the sprite chevron (18px, quiet ink, 9px from the right; 32px right padding). The folder input sets its path in mono at 13px with quiet-ink placeholder.
- **Hover:** border to second ink.
- **Focus:** border to instrument teal plus the 2px teal outline at zero offset.
- **Error:** `aria-invalid="true"` turns the border amber, with the reason in an amber note below.
- **Switch:** a 30 by 18px track. Off is an outline (1.5px inset in quiet ink) around a 10px quiet-ink knob; on is a solid ink track with a 14px window-paper knob moved 12px. Both states clear 3:1. Knob and track move over 180ms. Each switch carries a 13.5px label at 560 and a 12px quiet-ink hint.

### Navigation
- **Bar:** the window's 56px header. Left: the 24px brand mark and the wordmark, 10px apart. Right: the "Local only" status and the Options quiet button (sliders icon). A hairline sits beneath it, and another beneath the options tray when open. No other navigation exists; the colophon below the window holds the version and the limits link.

### File Row
The dense unit of the list. A full-width head button (thumbnail, name, sub line, status, chevron) with the icon actions beside it, at least 60px tall, 8px radius, hover wash on hover. The sub line reads in quiet ink for format, dimensions and size, and in second ink when it lists what the file reveals. Status by state: "Scanning…" or "Cleaning…" in quiet ink over a 1px teal sweep travelling under the row (1.1s, repeating); the vermilion count pill once scanned; the 24px seal and green "Clean" once verified ("Clean, old options" in quiet ink when the options changed since); an amber alert icon and amber text for failures. Opening a row rotates the chevron 180 degrees and expands its detail inline.

### Seal Tooltip
On hover over a clean row's status, or keyboard focus of its head, a tooltip shows the proof: "Verified clean", "Every byte accounted for" with "pixels exact" or "pixels within tolerance", and the first 16 hex digits of the SHA-256 in mono. Window paper, strong-hairline border, 8px radius, 9px by 11px padding, at most 260px wide, set 10px above the status and aligned to its right edge, fading and rising 3px on entry. Kept-payload rows get the same tooltip explaining what was kept. Under forced colours it takes a CanvasText border. On narrow screens it aligns to the status's left edge instead.

### Engraved Seal (signature)
A guilloché rosette on a 100 by 100 viewBox, engraved from the clean file's SHA-256: the same file always draws the same seal. Bytes 0 to 8 of the hash set its parts:
- an outer ring (radius 47, line stroke);
- an outer braided band of 5 to 7 sinusoidal paths (2 in the compact seal) around radius 39.5, with 14 to 22 lobes, amplitude 2.6 to 3.95, a phase from the hash and a small 2 to 4 cycle wobble; this band stands for the structure check;
- a middle ring (radius 32, hairline);
- an inner rosette of 4 to 6 paths (2 compact) around radius 20, with 5 to 9 petals and amplitude 6 to 9; this rosette stands for the pixel check;
- a centre check mark (bold stroke) on verified files, or a 3-unit dot on the brand marks.

Strokes are non-scaling and round-capped: hairline 0.7px, line 0.85px, bold 1.4px (the row seal's hairline is 0.75px; the brand mark's line is 1px). Colour is `currentColor`: verified green in rows (24px) and the verified panel (64px); ink for the bar's brand mark (24px, compact); quiet ink for the well and gate marks (32px, full detail). The favicon is a static compact seal in ink, switching to the dark ink under `prefers-color-scheme: dark`.

When a file has just been cleaned, each stroke engraves from nothing (a `pathLength` of 1 with the dash offset running from 1 to 0) over 640ms on the expo ease-out, staggered 70ms per stroke from the outer ring inward, so a row seal (7 strokes) completes in about 1.1s and the panel seal (12 to 16 strokes) in about 1.4 to 1.7s. It draws once per file; re-renders show it finished. The direction contract asked for 600ms in total; the build's timing is the recorded one. Under reduced motion the seal appears finished.

**The Earned Seal Rule.** A seal with a check mark is drawn only for a file that passed both checks. Partial, payload-kept and refused results show no seal. The check-less seal from the fixed hash is the brand mark (bar, empty well, access gate, favicon) and is never drawn in green.

### Drop Feedback
While files are dragged over the page: the window gains the 2px teal ring, the well its teal border and hover wash, and a fixed "Drop to scan" pill fades in 8vh above the bottom of the viewport (ink plate, plate-paper text at 13.5px and 600, 18px radius, 8px by 14px padding, drop-pill float shadow), over 180ms.

### Motion
State changes (hover, disclosure, switch, chevron, drag feedback) run 180ms on `cubic-bezier(0.16, 1, 0.3, 1)`. Button press is 120ms. Only the scan sweep (1.1s, repeating) and the seal engraving take longer. Under `prefers-reduced-motion: reduce` every transition and animation is zeroed; the sweep becomes a static teal line at 0.5 opacity.

**The 180 Rule.** Every state change takes 180ms on the expo ease-out. Longer motion is reserved for work in progress (the sweep) and proof (the seal).

## Do's and Don'ts

### Do:
- **Do** keep the whole tool in one centred window no wider than 680px (`min(680px, 100% - 32px)`), on the mineral ground.
- **Do** separate parts inside the window with 1px hairlines, tonal fills and 1px inset edges.
- **Do** give every interactive element a visible `:focus-visible` ring: 2px instrument teal at a 2px offset (zero on fields, inset 2px on row heads).
- **Do** keep the plate focusable while it waits or works: `aria-disabled="true"`, never `disabled`.
- **Do** gate hover styles behind `(hover: hover)`, and zero all motion under `prefers-reduced-motion: reduce`.
- **Do** give each component that relies on fills or inset edges a forced-colours fallback (a 1px CanvasText outline; switches in Canvas, CanvasText and Highlight).
- **Do** add every new colour as a light and dark pair under the same custom property.

### Don't:
- **Don't** use vermilion or green for anything except what a file reveals and what has been verified clean.
- **Don't** put a second reversed plate in a state, and don't show a plate before any file exists.
- **Don't** set type above 18px or in a second family, and don't use mono for labels or prose.
- **Don't** draw a checked seal for a file that did not pass both checks, and don't tint the brand mark green.
- **Don't** cast shadows inside the window; only the window, the seal tooltip and the drop pill float.
- **Don't** turn the drop well into a page-wide dashed zone with an upload icon; drag feedback is the teal ring, the well's teal border and the drop pill.
