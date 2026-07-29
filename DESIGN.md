---
name: ToupTek 4040 Control Console
description: An image-first scientific camera workstation adapted from the approved COSMOS66 console.
colors:
  surface-base: "#080b0f"
  surface-chassis: "#0d1117"
  surface-pane: "#111720"
  surface-raised: "#18202a"
  surface-input: "#0a0f15"
  line-subtle: "#293442"
  line-strong: "#3d4d5e"
  text-primary: "#f1f5f8"
  text-secondary: "#aebbc8"
  text-muted: "#8997a5"
  signal: "#58b8d1"
  button-primary: "#236b7d"
  button-primary-hover: "#287c90"
  warning: "#e4ac55"
  danger: "#f06e70"
  success: "#70c99a"
  focus: "#9fe6f3"
typography:
  title:
    fontFamily: "Segoe UI Variable, Segoe UI, system-ui, sans-serif"
    fontSize: "17px"
    fontWeight: 650
    lineHeight: 1.2
    letterSpacing: "-0.015em"
  body:
    fontFamily: "Segoe UI Variable, Segoe UI, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.45
    letterSpacing: "normal"
  label:
    fontFamily: "Segoe UI Variable, Segoe UI, system-ui, sans-serif"
    fontSize: "11px"
    fontWeight: 650
    lineHeight: 1.4
    letterSpacing: "0.055em"
  data:
    fontFamily: "Cascadia Mono, SFMono-Regular, Consolas, monospace"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
rounded:
  control: "6px"
  panel: "10px"
spacing:
  xs: "5px"
  sm: "8px"
  md: "16px"
  lg: "20px"
components:
  button-primary:
    backgroundColor: "{colors.button-primary}"
    textColor: "{colors.text-primary}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "7px 11px"
    height: "34px"
  button-secondary:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.text-primary}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "7px 11px"
    height: "34px"
  input:
    backgroundColor: "{colors.surface-input}"
    textColor: "{colors.text-primary}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "6px 9px"
    height: "34px"
---

# Design System: ToupTek 4040 Control Console

## Overview

**Creative North Star: "The Calibrated Control Room"**

This is an operating surface for acquiring and judging scientific images, not a dashboard assembled from interchangeable cards. It preserves the approved COSMOS66 image-first three-column workstation: controls form a disciplined left rail, the current image dominates the center, and measurement tools occupy the right rail. A persistent status strip and log timeline make ownership, exposure state, cooling, and recovery visible without obscuring the frame.

The console should feel like a precise optical instrument used in low ambient light. Expression comes from measured alignment, legible state color, dense but calm data, and the changing image itself. Decorative “space” motifs, neon spectacle, and marketing-style hero composition are outside the world.

**Key Characteristics:**

- The scientific image is always the largest visual object.
- Controls are grouped by acquisition sequence, not by equal-sized cards.
- State, warning, and error colors carry operational meaning only.
- Numeric data aligns for rapid comparison.
- Logs remain visible, filterable, pausable, and exportable.

## Colors

Use a restrained dark instrument palette: graphite work surfaces, high-contrast neutral text, one cool acquisition/status accent, amber for warnings, and red only for destructive or failed states. The normative values live in the frontmatter and map one-to-one to `tokens.css`.

**The Signal Integrity Rule.** Accent colors never decorate neutral content; each colored element must communicate selection, activity, warning, or failure.

## Typography

Use Segoe UI Variable with Segoe UI and system UI fallbacks for interface copy. Use Cascadia Mono with platform monospace fallbacks only for pixels, temperatures, coordinates, timestamps, transaction identifiers, and logs—not as a costume for the whole product.

Hierarchy is functional: pane titles use the 17-pixel title role, body and controls use the 14-pixel body role, compact uppercase pane indices use the 11-pixel label role, and numeric readouts use the 12-pixel data role. Controls use sentence case and name the action directly.

**The Measurement Type Rule.** Monospace is reserved for values that operators compare or copy; actions, headings, and explanatory text remain sans-serif.

## Layout

The desktop composition is a persistent 66-pixel top status strip, a three-column working area, and a 192-pixel bottom log region. The left rail stays between 258 and 284 pixels, the image viewport flexes from a 440-pixel minimum, and the right rail stays between 286 and 320 pixels. Controls follow the actual workflow: connect, preview, exposure, Abort, geometry/readout/gain, then cooling.

At 1180 pixels and below, analysis moves beneath the viewport before acquisition controls do. At 760 pixels and below, the surface becomes a single ordered flow. A compact status row keeps Link, CameraState, RAW mode, and CCD temperature visible; the Session strip with connection, Preview, and Abort sits immediately before the image and remains sticky beneath it.

**The Image Majority Rule.** At workstation widths, the viewport owns more area than either side rail and cannot be reduced to a thumbnail by secondary panels.

## Elevation & Depth

Depth is structural and restrained. Pane boundaries use tonal layering and one-pixel separators; the image stage drops to the deepest base surface. The only overlay shadow is a downward, blurred shadow used when the mobile Session strip becomes sticky (`0 8px 18px rgb(0 0 0 / 28%)`). Toasts use the stronger transient overlay shadow recorded in the sidecar.

**The Chassis Rule.** Resting panes share edges and tonal layers. Shadows indicate transient overlap or sticky state, never ordinary containment.

## Shapes

Controls use compact, gently squared six-pixel corners. Ten-pixel panel corners are reserved for true raised overlays. Small pills are reserved for state indicators and filters. Main panes are not presented as a collection of floating rounded cards; they form one continuous instrument chassis.

## Components

### Buttons

Buttons share a 34-pixel minimum height, six-pixel corners, a one-pixel state border, and a 160-millisecond color transition. Primary acquisition actions use the button-primary role; secondary actions sit on the raised neutral surface; Abort alone uses the danger vocabulary. Every variant has hover, focus-visible, active, disabled, and loading-safe behavior.

### Inputs and fields

Inputs use the deepest input well, a strong one-pixel neutral border, six-pixel corners, and compact padding. Labels sit above controls, units occupy a distinct trailing measurement cell, and native controls retain keyboard behavior. Validation and synchronization copy appears inline below the owning section.

### Status and Session strips

The status strip is a continuous instrument rail, not a row of cards. At mobile sizes it becomes a compact two-row readout while retaining Link, CameraState, RAW mode, CCD temperature, and serial identity. The Session strip keeps connection, Preview, and Abort adjacent and remains immediately reachable.

### Scientific image viewport

The viewport owns the flexible center column and uses the image itself as the dominant visual object. Empty, loading, image, and error states occupy the same stage. Display stretch is explicitly labeled as non-scientific; pointer and keyboard pixel inspection announce coordinates and ADU through a live readout.

### Analysis and log regions

Statistics use aligned definition lists rather than metric cards. The histogram is a real bounded canvas plot. The bottom log timeline remains visible, filterable, pausable, exportable, and renders all untrusted messages through text nodes.

## Do's and Don'ts

### Do:

- **Do** keep connection, CameraState, RAW mode, temperature, and serial identity visible.
- **Do** show loading, empty, disconnected, exposing, aborted, and failed states explicitly.
- **Do** preserve keyboard focus, readable contrast, and reduced-motion behavior.
- **Do** keep display stretching visibly separate from scientific data and downloads.

### Don't:

- **Don't** reuse COSMOS66 device names, 8120 dimensions, HDR 18-bit, PICam, labeled gain modes, or 1/2/4-only binning.
- **Don't** hide Abort inside a menu or modal.
- **Don't** use equal-weight metric cards as the primary layout.
- **Don't** invent stars, benchmark claims, customers, or decorative telemetry.
