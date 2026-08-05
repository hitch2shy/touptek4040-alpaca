# Scientific image viewport zoom design

Date: 2026-08-05
Status: approved design, implementation not started

## 1. Goal

Improve the Web scientific-image viewport so an operator can inspect the real
detail in a 4096 × 4096 ToupTek frame without downloading and parsing a FITS
file for every preview frame. Add scoped mouse and keyboard navigation, pan,
and box zoom. Phase 1 is view-only: no interaction may change the camera ROI,
binning, exposure settings, preview state, or scientific image data.

## 2. Canonical image data

The immutable `ImageGeneration.pixels` array remains the only canonical
scientific pixel source. FITS, Alpaca ImageArray/ImageBytes, raw ADU lookup,
statistics, and Web display images continue to derive from the same pinned
generation.

FITS remains a complete scientific download format. The browser must not use
FITS as the normal display transport because a full 4096 × 4096 RAW12 frame is
about 32 MiB before browser parsing and display conversion. Loading it for each
preview generation would increase latency, traffic, and memory without making
the displayed pixels more authoritative.

Display stretch, spatial reduction below 100%, and display enlargement above
100% affect only display PNGs. They never modify the immutable pixels or the
FITS stream. Raw ADU inspection continues to read `generation.pixels[y, x]`;
it must never infer a scientific value from the displayed PNG.

## 3. Selected architecture

Use server-rendered crops from the pinned original generation.

1. Fit-to-window requests render the whole generation as the existing bounded
   PNG preview.
2. A zoomed viewport computes the visible rectangle in original sensor/image
   coordinates.
3. The browser requests only that rectangle, with the current black point,
   white point, gamma, and required display dimensions.
4. The server validates the generation and crop, slices the immutable pixel
   array, applies display stretch, and returns a grayscale PNG.
5. At 100%, one source pixel maps to one CSS display pixel and the server does
   no spatial downsampling. Above 100%, the browser enlarges those pixels with
   nearest-neighbour rendering; it does not invent additional detail.

The existing `/web/v1/viewport.png` contract will be extended with an optional
complete crop tuple: `x`, `y`, `width`, and `height`. Either all four fields are
present or none are present. Coordinates are integer, zero-based image
coordinates and the rectangle must be fully inside the pinned generation.
Existing generation, stretch, and output-bound parameters remain required or
defaulted as they are today.

Each PNG response is bounded to 2048 × 2048 output pixels and 4,194,304 output
pixels in total. A normal viewport uses one response. If a display viewport is
larger than the per-response bound, the browser divides it into adjacent,
non-overlapping crop segments and composes them without resampling. This keeps
the memory bound while preserving 1:1 display on a large monitor.

The cache validator includes generation, crop rectangle, stretch parameters,
and output size. The response is still private and must revalidate.

## 4. Component boundaries

### 4.1 Server crop renderer

`png_stream.py` owns pure crop validation, proportional reduction, display
stretch, and bounded PNG streaming. It does not know about DOM state, keyboard
input, camera commands, or preview control.

`web_console.py` pins one `ImageGeneration` for the complete request, parses
and bounds the crop tuple, and maps stable errors to HTTP responses. It does
not republish or copy the formal/preview workspace generation.

### 4.2 Viewport mathematics

A small DOM-independent JavaScript module owns:

- fit scale and 1:1 scale;
- zoom around a pointer/sensor coordinate;
- pan and clamp calculations;
- screen-to-original coordinate conversion;
- box-selection normalization;
- crop-segment planning for the 2048-pixel response bound.

Keeping this module pure makes coordinate and boundary behaviour testable with
the project's existing Node-based test pattern.

### 4.3 Browser viewport controller

The viewport controller owns focus, pointer gestures, keyboard scope, current
scale and centre, request cancellation, image-layer replacement, and stale
response rejection. Stretch controls remain independent inputs; changing them
rerenders the same crop without changing viewport coordinates.

HTML/CSS add a compact toolbar containing zoom out, current percentage, zoom
in, Fit, 1:1, and Box Zoom. Every keyboard action has a visible button or an
existing pointer equivalent. The image stage shows a visible but restrained
focus indicator when viewport shortcuts are active.

## 5. Interaction contract

### 5.1 Activation and browser shortcut isolation

Image shortcuts are active only after the image stage receives focus through
a click or keyboard navigation. They are disabled whenever an input, select,
textarea, button, range slider, or content-editable element owns focus.

The application never intercepts `Ctrl`, `Alt`, or `Meta` combinations. Browser
page zoom, refresh, address-bar, navigation, tab, and window shortcuts retain
their normal behaviour. Outside an active image stage, wheel, Space, arrows,
Home, and End retain normal page behaviour.

### 5.2 Zoom and pan

- Initial state: Fit.
- Wheel over an active image stage: zoom around the pointer by a factor of
  approximately 1.25 per step.
- `+` / `-`: zoom around the viewport centre.
- `0`: Fit.
- `1`: 1:1 (100%).
- Double-click: toggle Fit and 1:1 around the clicked pixel.
- Maximum scale: 1600%.
- Below 100%: server-rendered proportional reduction.
- At or above 100%: no spatial smoothing; CSS uses pixelated/nearest-neighbour
  rendering.
- A drag beyond the click threshold pans when the image is zoomed.
- `Space` plus drag always pans.
- A short, stationary click remains raw-pixel inspection.

### 5.3 Box zoom

`Shift` plus drag starts a translucent view-only selection. While dragging,
the overlay shows the normalized original coordinates and size, for example
`X 1200–1799 · Y 900–1399 · 600×500 px`.

Releasing a valid selection fits that rectangle into the viewport. A selection
below the click threshold is treated as pixel inspection. `Escape` cancels an
unfinished selection. No box action exposes an implicit or automatic camera
ROI command. The overlay and toolbar identify the mode as view-only.

### 5.4 Pixel inspection

Click selects the exact original coordinate and reads its ADU from
`/web/v1/pixel`. Arrow keys move one original pixel; `Shift` plus an arrow moves
ten original pixels. The marker is positioned from the same viewport transform
used for crop calculation, so it remains aligned during zoom and pan.

### 5.5 Continuous preview

When a new generation has the same width and height, preserve scale, centre,
stretch values, and the inspected original coordinate. This lets an operator
watch the same detail across preview frames.

If ROI, binning, or output dimensions change, reset to Fit and clear any
out-of-range inspected coordinate. When a new generation arrives, abort all
old-generation viewport requests. A late old response is ignored even if the
network cancellation did not stop it.

## 6. Request scheduling and performance

- Pointer movement and wheel input are coalesced with a short debounce; only
  the newest required viewport is rendered.
- The controller uses `AbortController` for superseded requests and also checks
  a monotonically increasing render sequence before publishing a response.
- The last valid image stays visible while a replacement loads.
- Requests render only visible crop segments and never create a full
  4096 × 4096 browser display buffer during zoomed inspection.
- Server bounds apply before allocation or PNG streaming.
- Camera state queries, exposure control, preview control, FITS downloads, and
  the camera owner process remain independent of viewport navigation.

## 7. Error behaviour

- Missing/incomplete/invalid crop tuple: stable HTTP 400.
- Crop outside the pinned generation: stable HTTP 400; the browser clamps its
  state and retries once.
- Generation changed: stable HTTP 409; refresh state, adopt the current
  generation, and rerender according to the continuous-preview rules.
- No generation: stable HTTP 404 and the existing empty state.
- Temporary network/render failure: keep the last valid image, show a compact
  retry message, and permit manual retry. Do not blank the viewport.
- Dimension change during a gesture: cancel the gesture, clear its overlay,
  reset to Fit, and render the new generation.

No viewport failure may abort an exposure, stop preview, disconnect the camera,
change heating/cooling, or mutate ASCOM Camera state.

## 8. Testing and acceptance

### 8.1 Server tests

- RAW8 and RAW12 crop pixels agree exactly with the source generation.
- Top-left, bottom-right, one-pixel, odd-sized, and maximum-bound crops preserve
  orientation and coordinates.
- A 100% crop has no spatial downsampling.
- Stretch changes PNG values but not source pixels, FITS bytes, or raw ADU.
- Crop parameters are all-or-none and strictly bounded before rendering.
- ETag/cache identity changes with generation, crop, stretch, or output size.
- Old and missing generations retain the existing 409/404 contracts.
- 4096 × 4096 source tests verify bounded temporary memory and output limits.

### 8.2 Browser/Node tests

Using the existing Node module-test pattern, verify:

- pointer-centred wheel zoom;
- centre zoom, Fit, 1:1, and 1600% clamp;
- pan clamping at every edge;
- screen/original coordinate round trips;
- box selection in every drag direction;
- 2048-bound segment planning without gaps or overlaps;
- request cancellation and late-response rejection;
- same-size generation state preservation and dimension-change reset;
- input/select/range focus disables shortcuts;
- `Ctrl`, `Alt`, and `Meta` combinations are not intercepted.

### 8.3 Integration and packaging tests

- Existing pixel, statistics, PNG, FITS, ImageArray, and ImageBytes tests remain
  green.
- Simulator Web smoke covers Fit, 1:1, pan, box zoom, and ADU alignment.
- Packaged static assets and executable smoke include the new module and UI.
- Authorized real-camera verification compares one local crop, its raw ADU,
  and the same FITS generation, then confirms preview/exposure state is
  unchanged.

## 9. Phase-1 exclusions

Phase 1 does not include:

- setting or suggesting the camera exposure ROI;
- browser-side FITS parsing;
- a minimap/navigator;
- zoom history or back/forward stack;
- annotations, saved selections, screenshots, or measurements beyond the
  existing pixel and statistics functions;
- changes to ASCOM Camera V4, owner-process, heater, cooling, acquisition, or
  FITS semantics.

## 10. Acceptance summary

The feature is accepted when an operator can fit a 4096 × 4096 frame, inspect
it at true 1:1 resolution, zoom around a pointer, pan, box-zoom without changing
camera ROI, inspect exact raw ADU values, and keep the same detail centred
across same-sized preview generations. Browser shortcuts remain available,
view rendering stays bounded, and all scientific outputs continue to agree on
the same immutable generation.
