---
name: imgstrip
description: Two panes on one plain ground: the list on the left, what the chosen file gives away on the right.
colors:
  bg: "#ffffff"
  pane: "#f3f5f4"
  selected: "#eaf0ee"
  hover: "#f1f5f3"
  line: "#d9e0dd"
  line-soft: "#e3e9e6"
  control: "#7e8d87"
  ink: "#101a17"
  ink-2: "#3e4c46"
  ink-3: "#56645e"
  leak: "#a8400a"
  leak-bg: "#fcefe7"
  leak-line: "#efc9b5"
  verified: "#0a5c70"
  verified-bg: "#e6f1f4"
  verified-line: "#b4d3dc"
  warn: "#8a5400"
  warn-bg: "#fff6e0"
  warn-line: "#efd699"
  plate: "#101a17"
  plate-hover: "#27352f"
  plate-ink: "#ffffff"
  focus: "#0a5c70"
  selection: "#bfe3ea"
  bg-dark: "#0d1412"
  pane-dark: "#111a17"
  selected-dark: "#19241f"
  hover-dark: "#15201c"
  line-dark: "#2a3833"
  line-soft-dark: "#1f2b27"
  control-dark: "#75847e"
  ink-dark: "#e9efec"
  ink-2-dark: "#b8c4be"
  ink-3-dark: "#98a59f"
  leak-dark: "#ff9568"
  leak-bg-dark: "#35211a"
  verified-dark: "#62c9de"
  verified-bg-dark: "#0f2a31"
  warn-dark: "#f7c15f"
  warn-bg-dark: "#33270f"
  plate-dark: "#e9efec"
  plate-ink-dark: "#0d1412"
  focus-dark: "#62c9de"
typography:
  display:
    fontFamily: "Schibsted Grotesk, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "1.5rem"
    fontWeight: 720
    lineHeight: 1.2
    letterSpacing: "-0.022em"
  lede:
    fontSize: "1.0625rem"
    fontWeight: 400
    lineHeight: 1.5
  body:
    fontSize: "0.9375rem"
    fontWeight: 400
    lineHeight: 1.45
  body-sm:
    fontSize: "0.84375rem"
    fontWeight: 400
    lineHeight: 1.45
  caption:
    fontSize: "0.75rem"
    fontWeight: 400
    lineHeight: 1.45
  mono:
    fontFamily: "ui-monospace, SF Mono, Cascadia Mono, Menlo, Consolas, monospace"
    fontSize: "0.8125rem"
    lineHeight: 1.75
rounded:
  control: "8px"
  box: "10px"
  thumb: "4px"
spacing:
  gutter: "28px"
  detail-width: "clamp(340px, 30.5vw, 460px)"
components:
  bar:
    minHeight: "56px"
  button:
    height: "40px"
    rounded: "{rounded.control}"
  button-plate:
    backgroundColor: "{colors.plate}"
    textColor: "{colors.plate-ink}"
    height: "44px"
  switch:
    width: "36px"
    height: "18px"
  thumb:
    width: "72px"
    height: "54px"
    rounded: "{rounded.thumb}"
  drop-zone:
    border: "1px dashed {colors.control}"
    rounded: "{rounded.box}"
---

# Design System: imgstrip

## Overview

**Creative North Star: "Two Panes, One Ground"**

imgstrip is a plain white window with two parts. On the left is the list: a dashed drop zone, then one full-width row per file with its thumbnail, name, what it gives away, its SHA-256 and MD5, and a status on the right. On the right is the chosen file: a sentence in plain words about what it gives away, then the evidence in short sections. A bar at the bottom holds the one switch that belongs to the whole run (Deep scan), a line saying what has and has not changed, and the one dark button that does the next thing.

Nothing is boxed in a card and nothing floats. Parts are separated by hairlines and by one tonal fill (the right pane, and the selected row). Colour is kept for meaning: **vermilion** is what a file gives away, **teal** is what has been verified, **amber** is what did not go to plan. Every state has exactly one reversed ink plate, and it is the next action.

The signature is the engraved seal: a guilloché rosette computed from the cleaned file's SHA-256, so the same file always earns the same seal. Theme follows the operating system with no toggle.

**Key characteristics**
- Two panes from 900px up (list, then a 340 to 460px detail). Below 900px one column; the detail opens as a full-screen sheet.
- One family (Schibsted Grotesk) for every word, 12 to 17px, plus a 24px step for the detail's heading and the empty state. System mono for hashes and coordinates only.
- Meaning-only colour; one reversed plate per state.
- The right pane scrolls on its own; so does the list. On a wide screen the page itself never scrolls.
- Warnings roll up into a single amber line (the review tray) that opens into a short, capped list grouped by cause.
- Light and dark as exact token pairs under `prefers-color-scheme`.

## Colors

- **Green-black ink** (#101a17 / #e9efec): text, the checked switch, the plate. 17:1 on white.
- **Second ink** (#3e4c46 / #b8c4be) and **quiet ink** (#56645e / #98a59f): supporting text and the quietest text. Quiet ink is still above 4.5:1 on both the white list and the grey pane, which the test suite measures for every piece of text on screen in light and dark.
- **Vermilion** (#a8400a / #ff9568): only what a file gives away: the "N items found" count, tags, coordinates.
- **Teal** (#0a5c70 / #62c9de): verified results, the seal, copy-done ticks, a matching hash, the focus ring. It replaced the green of earlier versions.
- **Amber** (#8a5400 / #f7c15f) on **warning wash** (#fff6e0 / #33270f): outcomes that need a look: "Payload in pixels", "Payload kept", the review tray, skipped-option notes, "No match".
- **Grounds**: white list (#ffffff / #0d1412), grey detail pane (#f3f5f4 / #111a17), selected row (#eaf0ee / #19241f), hairlines (#d9e0dd / #2a3833), control edge (#7e8d87, 3.4:1 on white).

**The Two Signals Rule.** Vermilion marks only what a file reveals; teal marks only what has been verified. Neither decorates anything else.
**The One Plate Rule.** Each state has exactly one reversed ink plate: "Clean N images", then "Download all", or "Save" while the folder row is open.
**The Paired Theme Rule.** Every colour is a light and dark pair under one custom property name. A new colour is not done until both halves exist.

## Typography

Schibsted Grotesk ships with the app (SIL Open Font License) and nothing is loaded from the internet. Weights: 400 body, 560 to 620 controls, 700 names and headings. Counts and statuses use tabular figures; sentences do not (this face's tabular period is too wide). Hashes are mono, shown shortened in a row (`57cbf5d1…a2fdd91d`) and in groups of eight in the detail; copying gives the plain hex.

## Layout

**Wide (900px and up).** The bar (56px) on top. Under it a grid: the list on the left, the detail pane on the right (`clamp(340px, 30.5vw, 460px)`), and the bottom bar under the list. The list and the detail scroll independently; the count line stays at the top of the list. On the empty state the detail is hidden and the drop zone is centred.

**Narrow (below 900px).** One column; the page scrolls and the bottom bar sticks to the bottom. Picking a row opens the detail as a full-screen sheet with a "Back to list" bar. While it is open the page behind it is inert, focus is on the sheet's heading, Escape or Back closes it and puts focus on the row, and widening the window puts everything back. The list's rows tighten (container query on the list, not on the window) and the Add button appears, since the drop zone is then far away.

**Touch.** Where the pointer is coarse, every control is at least 44px: buttons, selects, text fields, the (i) buttons, the copy buttons, the switch (its checkbox grows to 44px over the track) and the links.

## Components

### The (i) button
A round, quiet button right after each switch's name. It opens a dark bubble with a one-to-three sentence explanation in plain words, placed beside the button and clamped to the screen. It is a toggle: click or Enter opens it, a second click, Escape, a click elsewhere, scrolling, resizing or moving focus away closes it. The text goes into an always-present live region, so it is read out when it opens. It never changes the switch.

### Switch row
The switch and its name are one label; the (i) comes next; the one-line hint is a separate label underneath. An option that does not apply to the images in the list is greyed out with a reason; one that cannot be done is greyed out with a yellow triangle. A greyed switch shows as off and is never sent.

### File row
Full width, 72 x 54 thumbnail, bold name, "FORMAT · W×H · size", up to three red-dot tags (or a sentence once cleaned), and a hash line: `SHA-256 … ⧉  MD5 … ⧉`. After cleaning the line becomes two, tagged *Before* and *After*. On the right: a status ("9 items found", "Scanning" with an indeterminate bar, or the seal, "Verified" and a Download link). The name is a button stretched over the whole row; the selected row is tinted and marked `aria-current`. Arrow keys move between rows.

### Detail pane
Heading (the file's name), a lede with the important phrases in bold, then sections with a heading, a hairline and a count: Identifying metadata, Hidden data, Colour profile, In the pixels, File fingerprints. After cleaning: the verified panel (seal and the two checks), what was removed, what changed, the fingerprints, and Download and Remove from list.

### File fingerprints
SHA-256 and MD5 in groups of eight with a copy button each. After cleaning, a *Before cleaning* set and an *After cleaning* set, and a note that they differ because the file was rebuilt. Below them, **Compare with a hash you already have**: paste a hash and it says *Match* (teal, with which file and which algorithm) or *No match* (amber), or what is wrong with what was pasted. MD5 matches carry a reminder that MD5 can be forged.

### Hash list
A quiet button beside Clear. It downloads a CSV of every file's before and after hashes, built in the page.

### Review tray
One amber line: "N things need a look · M images". It opens into a list grouped by cause, never taller than 40% of the screen, with chips that take you to the file.

### Bottom bar
Deep scan with its (i) and a one-line hint; "Nothing is changed yet. N files are ready to clean." (or what was cleaned); Save to folder; and the plate. The plate is never `disabled`, so it keeps keyboard focus.

### Drop zone
A dashed outline with "Drop images here", one line of explanation and "Choose images". A drag over the page shows a teal ring and an ink pill near the bottom of the screen; paste works too.

### Motion
Short, quiet, and off when `prefers-reduced-motion` is set. The seal is drawn stroke by stroke when verification passes. The scan shows an indeterminate bar, never a percentage: the work cannot know how far along it is.

### Forced colours
The switch, the verified panel, the selected row and the bubble get outlines from system colours, so nothing depends on a fill that high-contrast mode removes.

## Do's and Don'ts

**Do**
- Say plainly that nothing has changed yet, until something has.
- Say what a scan that found nothing means: not proof that nothing is hidden.
- Keep every explanation in short, plain sentences.
- Show hashes as text a person can select, copy and compare.
- Test every new screen at 320, 375, 768, 900 and 1440px, light and dark.

**Don't**
- Don't colour anything vermilion or teal that is not a leak or a verification.
- Don't show a made-up percentage.
- Don't put the one plate on two things at once.
- Don't add a card, a shadow or a gradient to a part that a hairline can hold.
- Don't call MD5 a proof.
