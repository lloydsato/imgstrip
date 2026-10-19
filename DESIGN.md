---
name: imgstrip
description: One small, exact window that shows what an image gives away, cleans it, and seals the proof.
colors:
  ground: "#e9efec"
  ground-glow: "#d3ebe3"
  panel: "#fdfefd"
  sunken: "#f2f6f4"
  hover: "#ecf1ef"
  line: "#e0e7e3"
  line-strong: "#c6d0cb"
  field-line: "#7a8681"
  ink: "#10201a"
  ink-2: "#425149"
  ink-3: "#5a6761"
  leak: "#c0310f"
  leak-bg: "#fff0eb"
  leak-line: "#f6c6b8"
  clean: "#11774a"
  clean-bg: "#e3f8ec"
  clean-line: "#b0e0c4"
  warn: "#8f5608"
  warn-bg: "#fff6e0"
  warn-line: "#efd699"
  plate: "#10201a"
  plate-hover: "#23352d"
  plate-ink: "#f8fbf9"
  focus: "#00858d"
  selection: "#9be0db"
  ground-dark: "#0a1310"
  ground-glow-dark: "#12332b"
  panel-dark: "#15211c"
  sunken-dark: "#111b17"
  hover-dark: "#1d2a25"
  line-dark: "#25322d"
  line-strong-dark: "#364540"
  field-line-dark: "#66746e"
  ink-dark: "#e8eeeb"
  ink-2-dark: "#b6c2bc"
  ink-3-dark: "#96a39d"
  leak-dark: "#fd8868"
  leak-bg-dark: "#3e231b"
  leak-line-dark: "#6e3a2b"
  clean-dark: "#76e1a7"
  clean-bg-dark: "#163323"
  clean-line-dark: "#27593e"
  warn-dark: "#f7c15f"
  warn-bg-dark: "#3a2c12"
  warn-line-dark: "#66491b"
  plate-dark: "#e6ede9"
  plate-hover-dark: "#f8fdfa"
  plate-ink-dark: "#101b17"
  focus-dark: "#57d4d4"
  selection-dark: "#085955"
typography:
  display:
    fontFamily: "Schibsted Grotesk, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "1.625rem"
    fontWeight: 680
    lineHeight: 1.15
    letterSpacing: "-0.022em"
  wordmark:
    fontFamily: "Schibsted Grotesk, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "1.25rem"
    fontWeight: 680
    lineHeight: 1
    letterSpacing: "-0.02em"
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
  window: "20px"
  card: "14px"
  well: "14px"
  control: "10px"
  thumb: "9px"
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
  indent: "70px"
components:
  window:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.window}"
    width: "min(680px, 100% - 32px)"
  window-bar:
    height: "64px"
    padding: "0 14px 0 20px"
  button-plate:
    backgroundColor: "{colors.plate}"
    textColor: "{colors.plate-ink}"
    typography: "{typography.control}"
    rounded: "{rounded.control}"
    height: "40px"
    padding: "0 20px"
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
    height: "40px"
    padding: "0 16px"
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
    height: "40px"
    padding: "0 34px 0 12px"
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
    padding: "34px 24px 26px"
  file-card:
    backgroundColor: "{colors.panel}"
    rounded: "{rounded.card}"
    height: "68px"
    padding: "10px 6px 10px 10px"
  review-tray:
    backgroundColor: "{colors.warn-bg}"
    textColor: "{colors.ink}"
    typography: "{typography.control}"
    rounded: "{rounded.control}"
    height: "36px"
    padding: "6px 10px 6px 12px"
  status-pill:
    typography: "{typography.label}"
    rounded: "13px"
    height: "26px"
    padding: "0 10px"
  file-card-hover:
    backgroundColor: "{colors.hover}"
  thumb:
    backgroundColor: "{colors.sunken}"
    rounded: "{rounded.thumb}"
    size: "48px"
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
    size: "20px"
  seal-verified:
    textColor: "{colors.clean}"
    size: "64px"
  brand-mark:
    textColor: "{colors.ink}"
    size: "28px"
  well-mark:
    textColor: "{colors.ink-3}"
    size: "56px"
  seal-tip:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "10px 12px"
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

Type is a newsroom grotesk at small sizes, green-black ink on near-white, with parts separated by hairlines and tonal fills rather than heavy boxes and shadows. Colour is held back for meaning. Vermilion is what a file gives away, green is what has been verified clean, amber is what did not go to plan, and teal is the instrument itself (focus, the scan sweep, the drag ring). Every state has exactly one reversed ink plate, and it is the next action. Warnings are one more kind of instrument reading, and they never sprawl: however many images are loaded, they roll up into a single amber line (the review tray) that opens into a short, capped list grouped by cause. Density is packed but legible: 64px bar, file cards of at least 68px, 40px controls.

The signature is the engraved seal: a guilloché rosette computed from the clean file's SHA-256, so the same file always earns the same seal. It is drawn stroke by stroke when verification passes. Drawn from a fixed hash and without its check mark, it is also the brand mark. Theme follows the operating system with no toggle; the dark world is a deep ink-green, not a grey.

**Key Characteristics:**
- One centred window, at most 680px wide, on a tinted ground; no page chrome.
- One family (Schibsted Grotesk) for every word, 12px to 18px, plus one 26px display step for the empty-state headline; system mono only for real values.
- Meaning-only colour: vermilion reveals, green verifies, amber warns, teal instruments.
- One reversed ink plate per state; no plate before files exist.
- Flat inside, one lift outside: hairlines and tonal fills inside the window, a soft layered shadow under it. Files are cards on the window, not rows in a table.
- Warnings are rolled up, never repeated: one review tray, grouped by cause, capped in height; a sticky count bar and a sticky action bar keep the list's two ends on screen.
- A hash-engraved guilloché seal as the single signature move.
- Light and dark as exact token pairs following `prefers-color-scheme`.

## Colors

A cool, low-chroma green-grey world in which only the three meaning colours carry saturation.

### Primary
- **Green-Black Ink** (#10201a light / #e8eeeb dark): all primary text, the wordmark and brand mark, the checked switch track, and `accent-color` for native controls. 16.70:1 on the panel in light, 14.11:1 in dark.
- **Ink Plate** (#10201a, hover #23352d; dark #e6ede9, hover #f8fdfa) carrying **Plate Paper** text (#f8fbf9 / #101b17): the one reversed action per state, and the drop pill. In dark the plate turns pale with ink text, so it stays the strongest object in the window.

### Secondary
- **Reveal Vermilion** (#c0310f / #fd8868): the text of what a file gives away: the "N to remove" count, the field names in the reveals panel, "N revealing items" in the footer summary.
- **Vermilion Wash** (#fff0eb / #3e231b) and **Vermilion Rule** (#f6c6b8 / #6e3a2b): the fill and 1px inset edge of the count pill and the reveals panel.

### Tertiary
- **Verified Green** (#11774a / #76e1a7): seal strokes, check marks, the "Clean" status pill, "N clean" in the summary, and the icon on ok notes (nothing found and rebuilt, ZIP downloading).
- **Mint Wash** (#e3f8ec / #163323) and **Mint Rule** (#b0e0c4 / #27593e): the fill and 1px inset edge of the verified panel.

### Neutral
- **Mineral Ground** (#e9efec / #0a1310): the page background only. Pale grey-green by day, deep ink-green by night, lifted at the top by one soft radial glow (#d3ebe3 / #12332b) that never reaches the window.
- **Window Paper** (#fdfefd / #15211c): the window, field and secondary-button fills, item rows inside the detail tables, the seal tooltip.
- **Well Tint** (#f2f6f4 / #111b17): the drop well and access gate, the options tray, thumbnails, an open quiet button, the partial-verification panel.
- **Hover Wash** (#ecf1ef / #1d2a25): hover on rows and on quiet, secondary and icon buttons; the well while a drag is over the page.
- **Hairline** (#e0e7e3 / #25322d): every 1px rule (bar, list, rows, options, footer), the well border, the gaps in the item tables, and the disabled plate fill.
- **Strong Hairline** (#c6d0cb / #364540): the tooltip border, link underlines, the scrollbar thumb.
- **Field Edge** (#7a8681 / #66746e): the 1px border of selects, the folder input and secondary buttons (3.74:1 and 3.39:1 against the panel).
- **Second Ink** (#425149 / #b6c2bc): supporting text: well sub line, field labels, list count, footer summary, quiet buttons, the "Local only" status.
- **Quiet Ink** (#5a6761 / #96a39d): the quietest text and icons: file sub lines, meta lines, hints, the formats line, chevrons, mono detail values, the colophon. Still 5.86:1 (light) and 6.34:1 (dark) on the panel.

### Signal
- **Caution Amber** (#8f5608 / #f7c15f): outcomes that did not go to plan or need the user before cleaning can succeed: "Not scanned", "Not cleaned", "Damaged", "Payload in pixels", "Payload kept", "N not cleanable", an invalid folder border, the armed Clear button, warn and error notes.
- **Instrument Teal** (#00858d / #57d4d4): the focus ring, the text caret, a focused field's border, the 1px scan sweep and the 2px drag ring.
- **Warning Wash** (#fff6e0 / #3a2c12) and **Warning Rule** (#efd699 / #66491b): the fill and 1px inset edge of every amber surface: the review tray, warning pills, callouts inside a file's detail, skipped-option notes and the footer's error line. Amber text on the wash is 5.57:1 (light) and 8.23:1 (dark).
- **Teal Selection** (#9be0db / #085955): text selection, with ink text on top.

### Named Rules
**The Two Signals Rule.** Vermilion marks only what a file reveals; green marks only verified-clean output. Neither colour decorates anything else, and neither appears in chrome, buttons or the brand mark.

**The One Plate Rule.** Each state carries exactly one reversed ink plate: the next action (Clean N images, Download, or Save while the folder row is open, when the footer action steps down to secondary). The empty state's one plate is "Choose images", its only action; the access gate has none.

**The Paired Theme Rule.** Every colour is a light and dark pair under one custom property name; dark mode redefines `:root` under `prefers-color-scheme: dark` and nothing else. A new colour is not done until both halves exist.

## Typography

**Display Font:** Schibsted Grotesk (with ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif), self-hosted variable woff2, weights 400 to 900, latin and latin-ext subsets, OFL
**Body Font:** Schibsted Grotesk (same stack)
**Label/Mono Font:** the system monospace stack (ui-monospace, SF Mono, Cascadia Mono, Menlo, Consolas, monospace), for real values only

**Character:** A newsroom-born grotesk used small and exact, with hierarchy built from weight steps (400, 450, 500, 560, 600, 650) and three ink levels rather than from size. The system mono is a plain instrument readout beside it, never a style.

### Hierarchy
- **Display** (680, 26px, 1.15, -0.022em, balanced wrapping): the well and gate headline only. The largest type in the system, and used once per screen.
- **Wordmark** (680, 20px, line-height 1, -0.02em): "imgstrip", lowercase, beside the brand mark in the bar.
- **Title** (650, 18px, 1.45, -0.01em): reserved for dialog-scale headings; unused in the main window today.
- **Body** (400, 15px, 1.45): the base size on `body`.
- **Body Small** (400, 13.5px, 1.45): the workhorse for running text: the well sub line (max 42ch), notes and limits (max 64ch), reveals and item rows, the checks, the footer summary, select text.
- **Control** (560, 13.5px, 1.3): button labels (line-height 1 inside their fixed height), file names, switch labels, the list count.
- **Heading** (650, 13.5px): group titles in a file's detail ("Removed: Identifying metadata"), with the group count at 500 in quiet ink beside it; reveal field names at 600 in vermilion.
- **Label** (620, 12px): field labels in the options tray and the save row, and the row status pill ("4 to remove", "Clean"). Option-group headings (Privacy, JPEG files, Scan and output) are a 11px, 680-weight, letter-spaced capital label in quiet ink.
- **Caption** (400, 12px, 1.35): file sub lines, meta lines, hints, the formats line, the colophon. The seal tooltip uses this size at 450 with line-height 1.4.
- **Mono** (400, 12px, 1.55): item detail values (EXIF fields, coordinates, byte counts). The folder input sets its path in mono at 13px; inline `code` is mono at 0.92em.
- **Mono Hash** (400, 11px, 1.5): SHA-256 lines in the verified panel and the seal tooltip.

### Named Rules
**The One Face Rule.** Schibsted Grotesk sets every word, from the wordmark to the smallest hint. Weight and the three ink steps carry hierarchy; nothing goes above 18px except the one 26px display headline, and no second family appears.

**The Real Values Rule.** System mono appears only for actual values a file carries or the engine produces: metadata fields, coordinates, byte counts, hashes, folder paths, file suffixes. Labels and prose stay in the grotesk.

**The Instrument Figures Rule.** Counts use tabular figures: the row's count pill, the list count, group counts and the footer summary. File sizes and dimensions stay proportional, because this face's tabular period is too wide.

## Layout

A single centred column. The stage is `min(680px, 100% - 32px)` wide (a 16px gutter each side), with top padding `clamp(24px, 9vh, 96px)` and 56px below, so the window sits in the upper third of a desktop viewport. The window holds, top to bottom: a 64px bar (20px leading inset, 14px trailing), the options tray (an inline disclosure), the body (14px padding) and an action bar (12px by 14px) that exists only once files do. Below the window, centred: the colophon line and the limits disclosure (max 64ch).

Inside the body, the empty well is a hero: the 56px mark, the 26px headline, one sentence, the choose button, and a three-step strip (Scan, Clean, Verify) under a hairline. With files, the list top (count left, Add and Clear right, then the review tray when anything needs a look) sits above a stack of file cards 8px apart. Each card is a grid: 48px thumbnail, the name and sub line (1fr), the status pill (auto) and an 18px chevron, with 12px gaps and a 68px minimum height; the card's download and remove icon buttons sit outside the expanding head. An open card's detail stacks its parts 14px apart and indents 70px so it starts on the file-name column. Detail tables use a label column of up to 13em; the reveals panel sizes its label column to content; the verified panel is a 64px seal beside the checks.

The options tray is a three-column grid (14px row gap, 16px column gap, 18px 20px 20px padding) for the selects, with the switches below a hairline in two columns, gathered under three small capital headings: Privacy, JPEG files, Scan and output. The tray is a scroll container: it never grows past `min(76vh, 720px)`, so the list and the action bar stay reachable however many notes it carries.

**The two ends of the list stay on screen.** The list top is `position: sticky` at the top of the viewport and the action bar is `position: sticky` at the bottom, both on a frosted panel (`backdrop-filter` blur over 86% panel). However long the list, the count, the review tray and the primary action are never more than a glance away. The action bar scrolls internally above 55vh (50vh on narrow windows) so an open save row plus a long error line can never cover the list.

Spacing follows a small rhythm: 2px between toolbar icons, 4px and 6px inside stacks, 8px between cards and grouped controls, 12px between card parts, 14px for the window's own insets, 20px for the bar and tray edges. Inline disclosures (options, a card's detail, save to folder, limits) open in place by animating `grid-template-rows` from 0fr to 1fr over 180ms on the expo ease-out; content is set to `visibility: hidden` once closed. The review tray opens by showing its panel directly, at the same 180ms for its chevron.

**Responsive.** The window is a size container (`container: win / inline-size`), and its narrow layout answers to the window's own width, so it holds wherever it is shown, not only at a given viewport. At a window of 600px and below the bar insets to 16px and 8px, the options tray and the switches go to one column, the body padding becomes 10px, and each card turns into two lines: the name takes the full width (wrapping to at most two lines), the status pill drops beneath it, the card actions stack vertically, and the detail loses its indent. The action bar's buttons go full width with the primary plate first, on its own line. At 400px and below the "Local only" text is visually hidden and its lock stays; at 319px and below the "Options" label goes too. Below a 640px viewport the stage's top padding drops to 14px. Hover styles apply only under `(hover: hover)`, and under `(pointer: coarse)` every control reaches 44px.

The direction contract asked for a window of about 640px and 56px rows; the build settled on 680px, with 68px cards. The build wins, and these are the recorded values.

### Named Rules
**The Glance Width Rule.** The window never grows past 680px, whatever the viewport. More files make it taller, never wider.

**The Name Column Rule.** Everything inside an open card's detail starts on the file-name column (70px: a 10px card inset, the 48px thumbnail and a 12px gap), so the thumbnail column stays clear.

**The Two Ends Rule.** The top of the list (count, tools, review tray) and the action bar stay on screen while the list scrolls. Nothing that matters is ever below sixty files.

## Elevation & Depth

A hybrid with one lift. The window is the only surface raised off the ground, under a soft four-layer shadow tinted green-black (rgb 24 52 42) (four layers in dark too, the first a faint white hairline that defines the edge). Inside the window everything is flat: hairlines, tonal fills (well tint, hover wash, vermilion, mint and warning washes) and 1px inset edges separate parts. The only things that carry their own small shadow are transient: the seal tooltip, the drop pill, and the one file card that is open, which lifts a hair so the eye finds it among sixty. Dragging files over the page adds a 2px teal ring around the window, outside its shadow.

### Shadow Vocabulary
- **Window lift, light** (`box-shadow: 0 0 0 1px rgb(24 52 42 / 0.05), 0 1px 2px rgb(24 52 42 / 0.06), 0 12px 28px -8px rgb(24 52 42 / 0.14), 0 40px 80px -32px rgb(24 52 42 / 0.26)`): the window at rest.
- **Window lift, dark** (`box-shadow: 0 0 0 1px rgb(255 255 255 / 0.03), 0 1px 2px rgb(0 0 0 / 0.3), 0 16px 40px -8px rgb(0 0 0 / 0.5), 0 40px 80px -32px rgb(0 0 0 / 0.6)`): the window at rest in dark.
- **Drag ring** (`box-shadow: 0 0 0 2px var(--focus), var(--window-shadow)`): the window while files are dragged over the page.
- **Tooltip float** (`box-shadow: 0 1px 2px rgb(var(--shadow-rgb) / 0.08), 0 12px 28px -10px rgb(var(--shadow-rgb) / 0.35)`): the seal tooltip.
- **Open card lift** (`box-shadow: 0 1px 2px rgb(var(--shadow-rgb) / 0.05), 0 8px 20px -14px rgb(var(--shadow-rgb) / 0.22)`): the single expanded file card.
- **Drop pill float** (`box-shadow: 0 8px 24px -8px rgb(var(--shadow-rgb) / 0.4)`): the "Drop to scan" pill.
- **Inset edge** (`box-shadow: inset 0 0 0 1px var(--line)`, or `--leak-line` / `--clean-line`): the edge of thumbnails, item tables, the count pill and the reveals and verified panels; the switch's off state uses a 1.5px inset in quiet ink.

### Named Rules
**The One Lift Rule.** Only the window lifts off the ground. Inside it, only transient overlays (tooltip, drop pill) and the open file card cast a shadow; if a part needs separating, use a hairline or a tonal fill.

## Shapes

Gently rounded, with the radius stepping down as elements nest: the window (20px), file cards, the drop well and gate (14px), controls, the review tray, panels, item tables and the tooltip (10px), thumbnails (9px). Pills are exactly half their height: status pills (13px on 26px), the switch track (10px on 20px) and the drop pill (20px on 40px). Apart from icon details, the only circles are the switch knob and the seal.

Edges are 1px. Hairline rules divide the bar, options, and action bar; file cards carry a 1px hairline border; tinted panels draw their edge as a 1px inset shadow rather than a border, so it never shifts layout. Fields and secondary buttons take a solid 1px field-edge border. Nothing is dashed.

Icons come from one inline SVG sprite on a 24 grid: 1.6px strokes, round caps and joins, no fills, drawn at 15px to 18px in `currentColor` (lock, sliders, chevron, download, folder, close, plus, check, alert, warn, copy, info, key, image). Check marks inside the verified panel use a 2px stroke.

## Components

### Buttons
Compact and confident; the plate is the only filled button.
- **Shape:** gently rounded (10px), 40px tall, 7px gap between icon and label, control type.
- **Plate (primary):** ink plate with plate-paper text, 20px horizontal padding, at least 140px wide. It carries the action bar's next action ("Clean N images", "Download", "Download all (.zip)"), the empty state's "Choose images", and the folder row's Save (34px small variant).
- **Secondary:** window-paper fill, 1px field-edge border, ink text, 16px padding: "Save to folder", "Download N", "Scan the pixels too". When the folder row opens, the action bar's plate becomes secondary so Save is the only plate.
- **Quiet:** no fill, second-ink text, 34px tall, 11px padding: Options, Add, Clear. Open (`aria-expanded="true"`) takes the well tint, a hairline edge and ink.
- **Icon:** 34px square, quiet-ink icon: a card's download and remove, copy hash (28px). Under a coarse pointer every button, field and chip reaches 44px.
- **Hover / Focus:** hover (hover-capable devices only) moves to the hover wash, the plate to its hover shade, the secondary border to quiet ink; 180ms on the expo ease-out. Focus is a 2px instrument-teal outline with a 2px offset. Press nudges the button down 0.5px over 120ms.
- **Disabled and busy:** the plate is never `disabled`; it takes `aria-disabled="true"` so it keeps focus, and shows a hairline fill with second-ink text while waiting. While cleaning it stays an ink plate at 0.86 opacity reading "Cleaning…". Clear arms on first press: amber text on the warning wash, and a second press clears.
- **Link:** second-ink text underlined 1px in the strong hairline with a 3px offset; on hover the underline takes the text colour. Used only for the colophon's "What cleaning can't do".

### Chips and Pills
- **Status pill:** one component, 26px tall (13px radius), 10px padding, label type with a 1px inset edge. Its tone says what kind of thing the status is, set by the page from the file's state: **leak** (vermilion wash: "4 to remove"), **ok** (mint wash, the 20px seal at its left: "Clean"), **warn** (warning wash, an alert icon: "Payload kept", "Not cleaned", "Damaged"), **muted** (well tint: "Not supported", "Clean, old options", "No metadata · pixels not scanned") and **busy** (well tint, with the card's scan sweep: "Scanning…", "Cleaning…"). Text is held to one line and ends in an ellipsis at 18em; on narrow windows the pill drops under the name and uses the full width. The only pills in the system.
- **Chip:** a 26px (34px on touch) pill on window paper with a warning-rule inset edge, holding one file name, in the review tray only. Pressing it opens that file's card and brings it into view. A "+N more" chip in second ink reveals the rest of the names in place.
- **"Local only" status:** a lock icon and 12px, 560-weight second-ink text inside a 28px well-tint pill with a hairline edge, in the bar. Under 400px the text is visually hidden and the lock remains.

### Review Tray
The one place warnings live. It sits in the sticky list top, directly under the count and tools, and is absent when there is nothing to say.
- **Collapsed:** a single 36px line on the warning wash: the warn triangle, "3 things need a look", a quiet "· 44 images", and a chevron. It never wraps and never grows, whatever the list holds. It is a disclosure button (`aria-expanded`, `aria-controls`).
- **Open:** a panel under a warning-rule hairline holding one group per cause, never per file: a 650-weight title with a count badge, one or two plain sentences (the first two distinct wordings; a third reason is left to each file's own card), and a row of chips for the affected files, three at most, then "+N more". Errors (a file cannot be cleaned: unreadable, camera RAW, over a limit, a failed scan or clean, a format it cannot be saved as) come before warnings (it can, with a caveat: payload found or kept, scrub not applied, an option skipped for some images, damaged structure). The panel scrolls inside itself at 40vh at most (360px cap).
- **Where warnings are not:** the options tray keeps its own short notes beside each affected option, held to two lines with the full sentence in a tooltip; the full wording, and every file it names, is here. A file's own card still carries its detail in a warning callout.
- **Accessibility:** a polite live region announces the count once the list has settled and only when the set of warnings changes, so a large drop does not chatter. Escape closes the tray and returns focus to its toggle.

### Cards / Containers
- **Window:** window paper, 20px radius, the window lift; the one container on the ground. It is a size container for its own narrow layout.
- **Drop well:** a soft radial from window paper to well tint, 1px hairline border, 14px radius, padding 34px 24px 26px; centred 56px well mark, display headline, sub line and the plate "Choose images", then a three-step strip (Scan, Clean, Verify: a 20px ink numeral and one line each) under a hairline; the formats line sits just below the well. The empty state's one plate is allowed because choosing images is its only action. The access gate reuses the well without the button and the steps. While a drag is over the page its border turns teal and its fill the hover wash.
- **Reveals panel:** vermilion wash with a vermilion-rule inset edge, 10px radius, 12px by 14px padding; a two-column definition list with vermilion 600-weight names and ink values.
- **Verified panel:** mint wash with a mint-rule inset edge, 10px radius, 14px padding; the 64px seal beside two green-checked statements and the full SHA-256 with a copy button. When the structure check passed but a pixel payload was kept, it becomes the partial panel: well tint, hairline edge, quiet-ink checks and no seal.
- **Item tables:** groups of rows on window paper separated by 1px hairline gaps inside a 10px-radius hairline edge; label at 500, detail value in mono quiet ink.
- **Warning callout:** inside a file's detail, a warn or error note is a block on the warning wash with a warning-rule inset edge, 10px radius, 9px by 12px padding and ink text, so it cannot be read as explanation.
- **Shadow strategy:** see Elevation & Depth; the window lifts, and the open card a hair.

### Inputs / Fields
- **Style:** 40px tall, window-paper fill, 1px field-edge border, 10px radius, 13.5px text. Selects drop the native arrow for the sprite chevron (18px, quiet ink, 10px from the right; 34px right padding). The folder input sets its path in mono at 13px with quiet-ink placeholder.
- **Hover:** border to second ink.
- **Focus:** border to instrument teal plus the 2px teal outline at zero offset.
- **Error:** `aria-invalid="true"` turns the border amber, with the reason in an amber note below.
- **Switch:** a 32 by 20px track. Off is an outline (1.5px inset in quiet ink) around a 10px quiet-ink knob; on is a solid ink track with a 16px window-paper knob moved 12px. Both states clear 3:1. Knob and track move over 180ms. Each switch carries a 13.5px label at 580 and a 12px quiet-ink hint. Switches sit under small capital group headings; an option that cannot apply to the images loaded is greyed out, with a note held to two lines (warning wash and triangle when it cannot be done, plain quiet ink when it merely has nothing to act on).

### Navigation
- **Bar:** the window's 64px header. Left: the 28px brand mark and the wordmark, 11px apart. Right: the "Local only" status and the Options quiet button (sliders icon). A hairline sits beneath it, and another beneath the options tray when open. No other navigation exists; the colophon below the window holds the version and the limits link.

### File Card
The dense unit of the list: a 1px hairline card, 14px radius, 8px from its neighbours. A full-width head button (48px thumbnail, name, sub line, status pill, chevron) with the icon actions beside it, at least 68px tall, hover wash on hover. The sub line reads in quiet ink for format, dimensions and size, and in second ink when it lists what the file reveals; once cleaned it reads "format · dimensions · before → after size", plus "· signed" when credentialed. Status is the pill above. A 2px teal sweep travels along the foot of a scanning or cleaning card (1.1s, repeating). Cards enter with a 4px rise and fade. Opening a card rotates the chevron 180 degrees, expands its detail inline, squares the head's lower corners and lifts the card a hair. Going to a card from the review tray flashes a 2px teal ring on it for a second.

### Action Bar
The window's footer, sticky to the bottom of the viewport on a frosted panel (86% window paper, blurred), a hairline above it, 12px by 14px padding plus the device's bottom safe area. A summary on the left (the count of revealing items in vermilion, "N clean" in green, "N not cleanable" in amber, tabular figures, wrapping rather than clipping) and the actions on the right: Save to folder, Download N, and the plate. Opening Save to folder adds the folder row beneath; a result or error then appears as a note under it: green wash for success, warning wash for errors, wrapping, its text scrolling inside 6em. The bar scrolls inside itself above 55vh (50vh narrow) rather than covering the list.

### Content Credentials (optional)
Off by default and quiet when off. One more switch in the Options tray, "Sign with Content Credentials", built exactly like the others. Ticking it opens a panel under the switches (the same 180ms disclosure, a hairline above it, sunken paper) holding, top to bottom: a 13.5px title with a quiet "C2PA" tag; one paragraph in second ink saying what the credential claims and does not (12px); the signing identity; the date and detail selects; the "Make it auditable later" switch; short quiet notes about what the current options will make the credential say; and, only once a commitment has been made, a receipts line with a secondary button.
- **Identity:** with none loaded, one line "No signing identity loaded." and two buttons, "Load certificate" (secondary, key icon) and "Try a test identity" (quiet). Loading opens a two-column form of the standard fields (file inputs with a 32px secondary-styled button, a password field in mono) and a plate "Load identity" button. Loaded, it is a window-paper card with an 8px radius and a hairline edge: the signer's name at 650, a quiet "Remove" button, a two-column fact list (signer, issued by, validity, key, SHA-256 in mono at 11px) and the assessment as notes.
- **Warnings:** amber, never red. A test or untrusted certificate, an approaching expiry and the missing timestamp are `.note.is-warn` or plain info notes, with the alert or info glyph; blocking problems (self-signed, expired) are the amber error note on the form and refuse the identity. No new colour is introduced.
- **In a file's detail:** a signed card's sub line ends "· signed". Its detail gains a sunken "Content Credential" block under the verified panel: who signed and when, what the pixels are (identical, equivalent, altered) in one plain sentence, what a public viewer will see ("Unverified (signed, signer not trusted)"), the certificate warning if any, and a closing note that it vouches for processing only. A scanned file that already carries a credential shows a "Content Credentials found" block on window paper saying that cleaning removes it. The seal is unchanged: it still means the file passed both cleaning checks, and is never drawn for the credential.
- **Storage:** nothing in this panel is stored by the page. Its inputs live outside the options form and carry no `name`; the signing switch and the commitment switch are never saved between launches; the passphrase field is emptied the moment the request is sent.

### Seal Tooltip
On hover over a clean card's status, or keyboard focus of its head, a tooltip shows the proof: "Verified clean", "Structure verified" with "pixels exact" or "pixels within tolerance", "Hidden data in pixels:" followed by the verdict (detected, scrubbed, not detected or not scanned), and the first 16 hex digits of the SHA-256 in mono. Window paper, strong-hairline border, 10px radius, 10px by 12px padding, at most 260px wide, set 10px above the status and aligned to its right edge, fading and rising 3px on entry. Kept-payload rows get the same tooltip explaining what was kept. Under forced colours it takes a CanvasText border. On narrow screens it aligns to the status's left edge instead.

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
- **Do** keep the whole tool in one centred window no wider than 680px (`min(680px, 100% - 32px)`), on the mineral ground, and let its narrow layout follow the window's own width (a container query), not only the viewport's.
- **Do** separate parts inside the window with 1px hairlines, tonal fills and 1px inset edges.
- **Do** give every interactive element a visible `:focus-visible` ring: 2px instrument teal at a 2px offset (zero on fields, inset 2px on row heads).
- **Do** keep the plate focusable while it waits or works: `aria-disabled="true"`, never `disabled`.
- **Do** gate hover styles behind `(hover: hover)`, and zero all motion under `prefers-reduced-motion: reduce`.
- **Do** give each component that relies on fills or inset edges a forced-colours fallback (a 1px CanvasText outline; switches in Canvas, CanvasText and Highlight).
- **Do** add every new colour as a light and dark pair under the same custom property.
- **Do** roll warnings up by cause in the review tray, show at most three names per cause, and cap every open warning surface (tray 40vh, options 76vh, action bar 55vh) so a hundred images cannot push the list or the action out of reach.
- **Do** keep the list's top and the action bar sticky, and test long lists, long unbroken file names and 320px windows.

### Don't:
- **Don't** use vermilion or green for anything except what a file reveals and what has been verified clean.
- **Don't** put a second reversed plate in a state, and don't show a plate other than "Choose images" before any file exists.
- **Don't** write a per-file warning into the options tray, the footer or a toast when a cause covers several files: it belongs in the review tray once, with the names as chips.
- **Don't** set type above 18px (the 26px empty-state headline is the one exception) or in a second family, and don't use mono for labels or prose.
- **Don't** draw a checked seal for a file that did not pass both checks, and don't tint the brand mark green.
- **Don't** cast shadows inside the window; only the window, the open card, the seal tooltip and the drop pill float.
- **Don't** turn the drop well into a page-wide dashed zone with an upload icon; drag feedback is the teal ring, the well's teal border and the drop pill.
