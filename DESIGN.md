---
name: ToupTek 4040 Control Console
description: An image-first scientific camera workstation adapted from the approved COSMOS66 console.
---

<!-- SEED: established with the user before implementation; re-run $impeccable document once there's code to capture the actual tokens and components. -->

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

Use a restrained dark instrument palette: graphite work surfaces, high-contrast neutral text, one cool acquisition/status accent, amber for warnings, and red only for destructive or failed states. Exact tokens are resolved during the first implementation and then extracted back into this document.

**The Signal Integrity Rule.** Accent colors never decorate neutral content; each colored element must communicate selection, activity, warning, or failure.

## Typography

Use a native Windows-compatible workhorse sans-serif for interface copy. Use a compact monospace only for pixels, temperatures, coordinates, timestamps, transaction identifiers, and logs—not as a costume for the whole product.

Hierarchy is functional: one restrained product title, clear pane titles, compact control labels, and tabular numeric readouts. Controls use sentence case and name the action directly.

## Layout

The desktop composition is a persistent top status strip, a three-column working area, and a bottom log region. The center image viewport receives the flexible width; the left acquisition rail and right analysis rail remain bounded and independently scrollable. Controls follow the actual workflow: connect, preview, exposure, Abort, geometry/readout/gain, then cooling.

At narrower widths, analysis moves below the viewport before acquisition controls do. On small screens the surface becomes a single ordered flow, while connection state and Abort remain sticky and immediately reachable.

**The Image Majority Rule.** At workstation widths, the viewport owns more area than either side rail and cannot be reduced to a thumbnail by secondary panels.

## Elevation & Depth

Depth is structural and restrained. Pane boundaries use tonal layering and fine separators; floating shadows are reserved for transient menus or focused overlays. The image canvas sits visually deeper than controls so black image borders remain distinguishable from the surrounding workspace.

## Shapes

Controls use compact, gently squared corners. Small pills are reserved for state indicators and filters. Main panes are not presented as a collection of floating rounded cards; they form one continuous instrument chassis.

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
