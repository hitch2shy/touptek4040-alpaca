# Scientific Image Viewport Zoom Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add generation-pinned, original-pixel crop viewing with scoped zoom, pan, box zoom, and keyboard controls to the ToupTek 4096 × 4096 Web scientific-image viewport without changing camera ROI or scientific data.

**Architecture:** Extend the existing bounded PNG stream with an optional validated source crop. Keep viewport geometry in a pure JavaScript module, put request cancellation and DOM gestures in a focused viewport controller, and leave `image-viewer.js` responsible for generation, stretch, analysis, and raw ADU composition. FITS remains a download derived from the same immutable `ImageGeneration`; it is not parsed in the browser.

**Tech Stack:** Python 3.13, Falcon, NumPy, streaming PNG/zlib, browser ES modules, HTML/CSS, Node-based JavaScript tests invoked by pytest, PyInstaller, Windows PowerShell release scripts.

## Global Constraints

- Phase 1 is view-only; no gesture or shortcut may change camera ROI, binning, exposure, preview, owner-process, heater, cooling, ASCOM Camera V4, or FITS semantics.
- `ImageGeneration.pixels` remains the sole canonical scientific pixel source.
- FITS, raw ADU, statistics, ImageArray/ImageBytes, and Web crops must remain pinned to the same explicit generation.
- At 100%, one original image pixel maps to one CSS display pixel with no spatial downsampling.
- Above 100%, use nearest-neighbour/pixelated display and do not invent image detail.
- A single PNG response is limited to 2048 × 2048 pixels and 4,194,304 output pixels; larger display viewports use adjacent crop segments without gaps or overlap.
- Image shortcuts work only while the image stage is active, are disabled in editable/control elements, and never intercept `Ctrl`, `Alt`, or `Meta` combinations.
- Box zoom changes only the browser view and must be visibly labelled view-only.
- Same-sized preview generations preserve scale, centre, stretch, and inspected coordinate; dimension changes reset to Fit.
- Superseded requests are aborted and late responses must not replace the current view.
- Do not add a frontend framework, browser FITS parser, minimap, zoom history, annotations, saved selections, or screenshot feature.
- All new test artifacts stay under `test/`; real-camera verification requires fresh user authorization before temporarily disconnecting port 11111 and must restore the same service PID, identity, connected state, and idle state.

---

## File Structure

| Path | Responsibility |
|---|---|
| `touptek4040/png_stream.py` | Validate and stream bounded stretched PNGs from an optional original-pixel crop |
| `touptek4040/web_console.py` | Parse the all-or-none crop query and pin one generation for each viewport response |
| `touptek4040/web/viewport-math.js` | Pure viewport geometry, coordinate conversion, selection normalization, and crop-segment planning |
| `touptek4040/web/image-viewport.js` | Focus, toolbar, pointer/keyboard gestures, request cancellation, layers, and stale-response protection |
| `touptek4040/web/image-viewer.js` | Compose generation/stretch/statistics/raw-ADU behavior with the viewport controller |
| `touptek4040/web/app.js` | Own application lifetime and destroy the viewport controller before unload |
| `touptek4040/web/index.html` | Viewport toolbar, layer host, selection overlay, retry state, and accessible labels |
| `touptek4040/web/app.css` | Focus, cursor, overlay, toolbar, pixelated rendering, and responsive viewport styles |
| `test/web/test_web_images.py` | Crop API, orientation, ETag, validation, immutability, and memory tests |
| `test/web/test_viewport_interactions.py` | Node tests for pure geometry, shortcuts, request order, and generation transitions |
| `test/web/test_web_assets.py` | Static asset allow-list, HTML/CSS IDs/classes, accessibility, and CSP compatibility |
| `test/smoke/test_simulator_roundtrip.py` | Simulator crop/FITS/ADU generation consistency |
| `test/packaging/test_packaged_smoke.py` | Frozen asset and cropped viewport smoke |
| `test/scripts/run_test_package_hardware.ps1` | Authorized packaged real-camera crop/FITS/ADU and restoration evidence |
| `使用说明.txt` | Operator controls, focus rules, view-only statement, and troubleshooting |

---

### Task 1: Generation-Pinned Original-Pixel Crop API

**Files:**
- Modify: `touptek4040/png_stream.py`
- Modify: `touptek4040/web_console.py:42-60,590-631`
- Modify: `test/web/test_web_images.py`

**Interfaces:**
- Consumes: existing `ImageGeneration`, `_require_generation`, `_query_int`, `_query_float`, and `stream_viewport_png` behavior.
- Produces: `ViewportCrop(x: int, y: int, width: int, height: int)` and `stream_viewport_png(generation: ImageGeneration, *, black: int, white: int, gamma: float, max_width: int, max_height: int, crop: ViewportCrop | None = None) -> Iterable[bytes]`.
- Produces: optional `/web/v1/viewport.png` query tuple `x`, `y`, `width`, `height`; the existing no-crop URL and ETag format remain backward compatible.

- [ ] **Step 1: Add failing renderer crop/orientation tests**

Add tests that build a known 4 × 4 RAW8 generation and require an exact 2 × 2 crop:

```python
def test_viewport_crop_uses_exact_original_coordinates_without_mutation():
    pixels = np.array(
        [[0, 10, 20, 30], [40, 50, 60, 70],
         [80, 90, 100, 110], [120, 130, 140, 150]],
        dtype=np.uint8,
    )
    generation = _generation_for(pixels)
    before = pixels.tobytes()
    payload = b"".join(stream_viewport_png(
        generation,
        black=0,
        white=255,
        gamma=1.0,
        max_width=2,
        max_height=2,
        crop=ViewportCrop(1, 1, 2, 2),
    ))

    assert _png_pixels(payload) == (
        2,
        2,
        bytes([0, 50, 60, 0, 90, 100]),
    )
    assert pixels.tobytes() == before
```

Add parameterized cases for top-left, bottom-right, a single pixel, odd width/height, RAW12, exact 2048 output edges, and crops whose source is larger than output.

- [ ] **Step 2: Run the renderer tests and verify RED**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/web/test_web_images.py -k "viewport_crop" -v
```

Expected: collection/import failure because `ViewportCrop` and the `crop` parameter do not exist.

- [ ] **Step 3: Implement the minimal crop-aware stream**

Add the exact public type and default-preserving signature:

```python
@dataclass(frozen=True, slots=True)
class ViewportCrop:
    x: int
    y: int
    width: int
    height: int


def stream_viewport_png(
    generation: ImageGeneration,
    *,
    black: int,
    white: int,
    gamma: float,
    max_width: int,
    max_height: int,
    crop: ViewportCrop | None = None,
) -> Iterable[bytes]:
```

Resolve `None` to the full generation. Validate integer fields, positive size,
`x + width <= generation.width`, and `y + height <= generation.height` before
yielding the PNG signature. Compute the common spatial step from crop width and
height. Slice each row as:

```python
source = generation.pixels[
    crop.y + source_y,
    crop.x : crop.x + crop.width : step,
][:output_width]
```

Retain the existing float32 row-local stretch so the immutable generation is
never modified and no full-frame display copy is materialized.

- [ ] **Step 4: Add failing HTTP crop-contract tests**

Cover:

```python
@pytest.mark.parametrize("query", [
    "x=0", "x=0&y=0", "x=0&y=0&width=1",
    "x=-1&y=0&width=1&height=1",
    "x=15&y=15&width=2&height=2",
    "x=0&y=0&width=0&height=1",
])
def test_viewport_crop_rejects_incomplete_or_out_of_bounds_tuples(
    web_client, query
):
    response = web_client.simulate_get(
        f"/web/v1/viewport.png?generation=1&{query}"
    )
    assert response.status_code == 400


def test_viewport_crop_etag_pins_crop_and_stretch(web_client):
    response = web_client.simulate_get(
        "/web/v1/viewport.png?generation=1&x=2&y=3&width=4&height=5"
        "&black=10&white=2000&gamma=1.25&maxWidth=4&maxHeight=5"
    )
    assert response.status_code == 200
    assert response.headers["etag"] == (
        '"generation-1-c2-3-4x5-b10-w2000-g1.25-4x5"'
    )
```

- [ ] **Step 5: Run HTTP tests and verify RED**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/web/test_web_images.py -k "crop" -v
```

Expected: incomplete tuples are currently ignored and cropped ETag/content is not produced.

- [ ] **Step 6: Parse all-or-none crop fields and bind them to the ETag**

Add a private parser with this exact interface:

```python
def _viewport_crop(
    request: falcon.Request,
    generation: ImageGeneration,
) -> ViewportCrop | None:
```

Inspect `request.get_param(name)` for all four names. Return `None` if all are
missing, raise `ValueError("viewport crop requires x, y, width and height")` if
only some are present, and parse strict bounds using `_query_int`. Include the
crop identity before stretch fields in cropped ETags; keep the original ETag
unchanged when crop is `None`. Pass the result to `stream_viewport_png`.

- [ ] **Step 7: Run focused and adjacent Web image tests**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/web/test_web_images.py test/unit/test_fits_io.py test/alpaca/test_imagearray_json.py test/alpaca/test_imagebytes.py -q
```

Expected: all pass; existing full-frame URLs and ETags remain green.

- [ ] **Step 8: Commit Task 1**

```powershell
git add touptek4040/png_stream.py touptek4040/web_console.py test/web/test_web_images.py
git commit -m "feat: stream generation-pinned viewport crops"
```

---

### Task 2: Pure Viewport Geometry and Segment Planning

**Files:**
- Create: `touptek4040/web/viewport-math.js`
- Create: `test/web/test_viewport_interactions.py`

**Interfaces:**
- Consumes: image dimensions, viewport CSS dimensions, and pointer positions.
- Produces: `fitView`, `visibleRect`, `zoomAt`, `panBy`, `screenToImage`, `selectionToImageRect`, and `planCropSegments` named exports.
- View shape: `{scale, centerX, centerY}`, where `scale` is CSS pixels per original pixel and centre coordinates are floating-point original-image coordinates.
- Segment shape: `{x, y, width, height, left, top, displayWidth, displayHeight}` with integer source crop fields and CSS layer placement fields.

- [ ] **Step 1: Write failing Node-driven geometry tests**

Create a pytest helper that imports the ES module through `Path.as_uri()` and
runs Node with `--input-type=module`. Cover exact examples:

```javascript
const fit = math.fitView(4096, 4096, 1024, 768);
const one = math.zoomAt(fit, 1, 512, 384, 4096, 4096, 1024, 768);
const pointer = math.screenToImage(one, 512, 384, 4096, 4096, 1024, 768);
const reversed = math.selectionToImageRect(
  one, {x: 700, y: 500}, {x: 300, y: 200},
  4096, 4096, 1024, 768,
);
const segments = math.planCropSegments(
  {scale: 1, centerX: 2048, centerY: 2048},
  4096, 4096, 3000, 1400, 2048,
);
```

Assert Fit scale is `0.1875`; centre is `(2048, 2048)`; pointer-centred zoom
keeps the pointed original coordinate stationary; scale clamps to Fit and 16;
pan cannot expose space beyond an edge; reverse selection matches forward
selection; and segment plans cover the visible rectangle without gaps,
overlap, or an edge above 2048.

- [ ] **Step 2: Run geometry tests and verify RED**

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/web/test_viewport_interactions.py -k "math" -v
```

Expected: FAIL because `viewport-math.js` does not exist.

- [ ] **Step 3: Implement the minimal pure geometry module**

Export `MAX_SCALE = 16`, `MAX_SEGMENT_EDGE = 2048`, and the seven named
functions in the Interfaces block. Use these exact core formulas:

```javascript
const fitScale = Math.min(
  viewportWidth / imageWidth,
  viewportHeight / imageHeight,
);
const visibleWidth = Math.min(imageWidth, viewportWidth / view.scale);
const visibleHeight = Math.min(imageHeight, viewportHeight / view.scale);
const x = Math.min(
  imageWidth - visibleWidth,
  Math.max(0, view.centerX - visibleWidth / 2),
);
const y = Math.min(
  imageHeight - visibleHeight,
  Math.max(0, view.centerY - visibleHeight / 2),
);
```

`fitView` returns the fit scale and image centre. `zoomAt` converts the pointer
to an original coordinate before changing scale, then solves the new centre so
that original coordinate remains under the same pointer. `panBy` subtracts CSS
delta divided by scale from the centre. `screenToImage` uses the visible origin
plus screen coordinate divided by scale. `selectionToImageRect` converts both
corners, floors minima, ceils exclusive maxima, and clamps to the image.
`planCropSegments` walks the visible integer crop in rows and columns no larger
than `maxEdge`, deriving CSS placement from `(source - visibleOrigin) * scale`.

Use one internal `clampView` function. Reject non-finite/non-positive dimensions
with `TypeError`. `visibleRect` returns a fully in-bounds source rectangle.
Adjacent segments share only boundaries, never pixels.

- [ ] **Step 4: Run geometry tests GREEN**

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/web/test_viewport_interactions.py -k "math" -v
```

Expected: all geometry and segment cases pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add touptek4040/web/viewport-math.js test/web/test_viewport_interactions.py
git commit -m "feat: add bounded scientific viewport geometry"
```

---

### Task 3: Ordered Crop Rendering and Generation Transitions

**Files:**
- Create: `touptek4040/web/image-viewport.js`
- Modify: `test/web/test_viewport_interactions.py`

**Interfaces:**
- Consumes: Task 2 geometry exports and existing `getBlob` / `generationUrl` helpers.
- Produces: `createImageViewport(options) -> ImageViewportController`.
- Controller methods: `setGeneration(generation, dimensions)`, `setStretch(stretch)`, `refresh()`, `fit()`, `oneToOne()`, and `destroy()`.
- Callback options: `onInspect(x, y)`, `onGenerationConflict(error)`, and `onError(error)`.

- [ ] **Step 1: Add failing ordered-request and generation-state tests**

Use the existing Node fake-DOM style. Supply a deferred `fetchBlob` dependency:

```javascript
const controller = viewport.createImageViewport({
  elements,
  fetchBlob,
  makeObjectUrl: (blob) => `blob:${blob.id}`,
  revokeObjectUrl: (url) => revoked.push(url),
  onInspect: (x, y) => inspected.push([x, y]),
  onGenerationConflict: (error) => conflicts.push(error.status),
  onError: (error) => errors.push(error.message),
});

controller.setGeneration(7, {width: 4096, height: 4096});
controller.setStretch({black: 100, white: 2000, gamma: 1.2});
const first = controller.refresh();
const second = controller.refresh();
```

Assert the second refresh aborts the first, resolving the first later cannot
replace layers, the URL contains generation/crop/stretch/output values, and
only the latest object URL remains owned. Then set generation 8 with the same
dimensions and assert scale/centre are preserved. Set generation 9 with
different dimensions and assert Fit is restored and an out-of-range inspected
coordinate is cleared.

- [ ] **Step 2: Run request lifecycle tests and verify RED**

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/web/test_viewport_interactions.py -k "request or generation" -v
```

Expected: FAIL because `createImageViewport` is not defined.

- [ ] **Step 3: Implement the controller lifecycle without gestures**

Implement the exact `createImageViewport` signature from the Interfaces block
with destructured options `elements`, `fetchBlob`, `makeObjectUrl`,
`revokeObjectUrl`, `onInspect`, `onGenerationConflict`, and `onError`.
Production defaults are `getBlob`, `URL.createObjectURL`, and
`URL.revokeObjectURL`. Return this exact method surface after defining each
closure:

```javascript
return {
  setGeneration,
  setStretch,
  refresh,
  fit,
  oneToOne,
  destroy,
};
```

`elements` contains `stage`, `layerHost`, `selection`, `selectionLabel`,
`zoomOutput`, `retry`, and the six toolbar buttons. Keep one monotonically
increasing render sequence and one `AbortController` per segment request.
Publish all segments for a sequence atomically only after every segment has
resolved. Revoke unpublished and replaced object URLs exactly once. Keep the
last valid layers visible while loading or after a temporary error.

`setGeneration` rules:

- null generation: cancel requests, clear layers and view state;
- same width/height: preserve view and inspected coordinate;
- changed width/height: Fit and clear an out-of-range inspected coordinate;
- every generation change: increment sequence before any new request.

- [ ] **Step 4: Add failing stable-error tests**

Verify a rejected request with `status === 409` calls only
`onGenerationConflict`; other errors retain layers, unhide retry, and call
`onError`; `destroy()` aborts requests, removes owned object URLs, and prevents
late publication.

- [ ] **Step 5: Implement error and teardown behavior**

Treat `AbortError` as silent supersession. Do not report aborted renders. The
retry button calls `refresh()` for the current generation/stretch. This module
must not import or call any camera mutation API.

- [ ] **Step 6: Run Task 3 tests GREEN**

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/web/test_viewport_interactions.py -q
```

Expected: lifecycle, ownership, state preservation, errors, and teardown pass.

- [ ] **Step 7: Commit Task 3**

```powershell
git add touptek4040/web/image-viewport.js test/web/test_viewport_interactions.py
git commit -m "feat: render ordered viewport crop layers"
```

---

### Task 4: Scoped Toolbar, Mouse, Keyboard, Pan, and Box Zoom

**Files:**
- Modify: `touptek4040/web/index.html:139-176`
- Modify: `touptek4040/web/app.css:369-445`
- Modify: `touptek4040/web/image-viewport.js`
- Modify: `touptek4040/web/image-viewer.js`
- Modify: `touptek4040/web/app.js`
- Modify: `touptek4040/web_console.py:45-58`
- Modify: `test/web/test_viewport_interactions.py`
- Modify: `test/web/test_web_assets.py`

**Interfaces:**
- Consumes: Task 3 `ImageViewportController` and existing image viewer generation/stretch/pixel functions.
- Produces: visible IDs `viewport-zoom-out`, `viewport-zoom`, `viewport-zoom-in`, `viewport-fit`, `viewport-one-to-one`, `viewport-box-zoom`, `viewport-layer-host`, `viewport-selection`, `viewport-selection-label`, and `viewport-retry`.
- Produces: static modules `/ui/viewport-math.js` and `/ui/image-viewport.js`.
- Produces: `destroyImageViewer()` for app-lifetime cleanup.

- [ ] **Step 1: Add failing static structure/accessibility tests**

Extend the selector inventory test to require every produced ID. Assert toolbar
buttons use `type="button"` and exact accessible labels; the image stage keeps
`tabindex="0"` and has an updated shortcut description; the selection label
contains `View only`; both new modules are served as JavaScript with no-cache;
and the current CSP continues to permit same-origin scripts and blob images.

- [ ] **Step 2: Run static tests and verify RED**

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/web/test_web_assets.py -v
```

Expected: missing controls and static assets.

- [ ] **Step 3: Add the toolbar, layer host, overlay, and allow-listed assets**

Replace the single display image with a layer host inside the existing stage,
while retaining empty/loading/error states and the pixel cursor. Add the six
toolbar controls beside generation/FITS controls. Add both module names to
`_STATIC_ASSETS`.

CSS requirements:

- absolute clipped layer host inside `.image-stage`;
- `.viewport-layer` with `position: absolute` and pixelated rendering at scale
  greater than or equal to 1;
- distinct `:focus-visible` stage outline;
- pan, inspect, and box-select cursor states;
- translucent selection with readable coordinates and `View only` label;
- toolbar wrapping that never covers FITS on narrow screens;
- reduced-motion coverage for any new loading transition.

- [ ] **Step 4: Add failing gesture and shortcut tests**

In the Node fake DOM, dispatch and assert:

- click/focus then wheel keeps the pointer coordinate stationary;
- wheel before activation is not prevented;
- `+`, `-`, `0`, `1` perform the documented transitions;
- zoom toolbar buttons perform the same transitions as their keyboard forms;
- double-click toggles Fit and 1:1 around the clicked original coordinate;
- `Ctrl`, `Alt`, or `Meta` combinations are not prevented and change no state;
- editable/range/select targets do not trigger viewport keys;
- click below the 4-pixel threshold calls `onInspect(x, y)` once;
- normal drag above the threshold pans only;
- Space plus drag pans and prevents scrolling only while active;
- Shift plus drag in every direction yields a normalized view-only crop;
- Escape cancels Shift-drag without changing the view;
- arrow and Shift-arrow move inspected coordinates by one and ten.

- [ ] **Step 5: Run gesture tests and verify RED**

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/web/test_viewport_interactions.py -k "gesture or shortcut or selection or inspect" -v
```

Expected: event bindings and controls are absent.

- [ ] **Step 6: Implement scoped gestures and controls**

Use Pointer Events with pointer capture and a 4 CSS-pixel drag threshold. Plain
drag pans only after the threshold; Shift establishes box mode at pointer-down;
Space establishes forced-pan mode. Wheel calls `preventDefault()` only while
the stage is active and no forbidden modifier is held. Keyboard handling
returns immediately for control/editable targets or `ctrlKey`, `altKey`, or
`metaKey`.

Use `event.key` plus `event.code` so main-keyboard and numpad `+/-` work. Do not
bind `Home` or `End` to zoom; retain existing pixel behavior only while the
stage is active. Update percentage output after every view change.

- [ ] **Step 7: Compose the controller with the existing viewer**

`image-viewer.js` creates one controller in `bindImageViewer()`. Its exported
`setGeneration(generation, dimensions)` delegates to the controller, and
stretch changes call `controller.setStretch(stretch)` then
`controller.refresh()`.
Supply `onInspect(x, y)` using the existing pinned `/web/v1/pixel` request and
ADU text. Supply the existing generation-conflict recovery callback. Position
the marker through controller transforms, not the removed full-image bounds.
Export `destroyImageViewer()` and call it from the existing `beforeunload`
handler in `app.js` before closing the event stream.

- [ ] **Step 8: Run complete Web frontend tests GREEN**

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/web -q
```

Expected: current heater/security/log/image tests and new viewport tests pass.

- [ ] **Step 9: Commit Task 4**

```powershell
git add touptek4040/web/index.html touptek4040/web/app.css touptek4040/web/image-viewport.js touptek4040/web/image-viewer.js touptek4040/web/app.js touptek4040/web_console.py test/web/test_viewport_interactions.py test/web/test_web_assets.py
git commit -m "feat: add scoped scientific image navigation"
```

---

### Task 5: Simulator, Packaging, Operator Guide, and Authorized Hardware Proof

**Files:**
- Modify: `test/smoke/test_simulator_roundtrip.py`
- Modify: `test/packaging/test_packaged_smoke.py`
- Modify: `test/scripts/run_test_package_hardware.ps1`
- Modify: `使用说明.txt`
- Update through existing scripts only: `test/results/final-package/*`, `test/results/final-verification.md`, `dist/ToupTek4040Control*`

**Interfaces:**
- Consumes: final crop API and browser assets from Tasks 1–4.
- Produces: generation-consistency smoke evidence, packaged asset/crop proof,
  updated instructions, and a rebuilt formal ZIP from a clean source commit.

- [ ] **Step 1: Add failing simulator and packaged crop tests**

Extend simulator roundtrip to request a non-square crop from the same explicit
generation as FITS and raw pixel:

```python
crop_status, crop_headers, crop_payload = base.request(
    "GET",
    f"/web/v1/viewport.png?generation={generation}"
    "&x=3&y=5&width=7&height=9&maxWidth=7&maxHeight=9",
)
pixel_status, pixel_headers, pixel_payload = base.request(
    "GET",
    f"/web/v1/pixel?generation={generation}&x=3&y=5",
)
fits_status, fits_headers, fits_payload = base.request(
    "GET", f"/web/v1/image.fits?generation={generation}"
)
assert crop_status == pixel_status == fits_status == 200
assert crop_headers["content-type"].startswith("image/png")
assert json.loads(pixel_payload)["generation"] == generation
assert fits_headers["content-type"].startswith("image/fits")
```

In packaged smoke, assert `/ui/viewport-math.js` and
`/ui/image-viewport.js` are served, and a crop returns expected PNG dimensions
and a generation/crop-specific ETag.

- [ ] **Step 2: Run simulator/package tests and verify RED before rebuilding**

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/smoke/test_simulator_roundtrip.py test/packaging/test_packaged_smoke.py -q
```

Expected: source simulator crop passes after Tasks 1–4, while the old frozen
package is missing the new assets and crop contract.

- [ ] **Step 3: Update the operator guide**

Document FITS versus display PNG, click-to-activate, wheel and `+/-`, Fit `0`,
1:1 `1`, double-click toggle, drag/Space-drag, Shift-drag view-only box zoom,
Escape, click/arrow/Shift-arrow ADU inspection, reserved browser `Ctrl/Alt`
shortcuts, the fact that box zoom never changes ROI, and Retry behavior.

- [ ] **Step 4: Add authorized real-package evidence checks**

Extend `run_test_package_hardware.ps1` to record, for one pinned formal
generation: crop query and decoded dimensions; crop-origin raw ADU; FITS from
the same generation; camera settings, preview state, owner PID, heater, and
connection state before and after viewing; exact equality of control state;
and existing owner/shared-memory cleanup evidence.

The script retains its `finally` restoration. Obtain fresh authorization before
temporarily disconnecting/reconnecting port 11111. Never stop the user-service
PID; restore the exact PID, model, serial, `connected=true`, and `idle` state.

- [ ] **Step 5: Run source verification before the release commit**

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/web test/smoke/test_simulator_roundtrip.py test/unit/test_fits_io.py test/alpaca/test_imagearray_json.py test/alpaca/test_imagebytes.py -q
powershell -ExecutionPolicy Bypass -File test/scripts/run_all.ps1
git diff --check
```

Expected: focused and full suites have zero failures; diff check is clean.

- [ ] **Step 6: Commit release inputs before building**

```powershell
git add test/smoke/test_simulator_roundtrip.py test/packaging/test_packaged_smoke.py test/scripts/run_test_package_hardware.ps1 使用说明.txt
git commit -m "test: cover scientific viewport release"
git status --short
```

Expected: clean worktree. Build from this exact committed source, never from
uncommitted release inputs.

- [ ] **Step 7: Build and run frozen simulator/package verification**

```powershell
powershell -ExecutionPolicy Bypass -File test/scripts/build_release.ps1
$env:TOUPTEK4040_TEST_DIST = (Resolve-Path dist/ToupTek4040Control).Path
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/packaging -q
Remove-Item Env:TOUPTEK4040_TEST_DIST
```

Expected: build exit 0; locked SDK inputs match; package tests pass; ZIP has
both new JS modules and exactly one approved x64 `toupcam.dll`.

- [ ] **Step 8: Perform authorized real-camera verification and restore service**

Only after explicit authorization, run the package hardware command recorded
by `run_test_package_hardware.ps1`. Verify the view-only state snapshot is
identical, crop/FITS/ADU generation is the same, all observed package owner
PIDs are dead after shutdown, registry/live shared-memory lists are empty,
ports 11113/11114 are closed, and the original service is the same PID, exact
approved identity, connected, and idle.

- [ ] **Step 9: Normalize and commit evidence only**

Use existing evidence normalization scripts; do not hand-edit dynamic PID,
timestamp, or hash fields. Confirm the release-input diff from the source
commit to the evidence commit is empty.

```powershell
git add test/results/final-package test/results/final-verification.md
git commit -m "test: verify scientific viewport release"
git diff --check
git status --short
```

- [ ] **Step 10: Final independent verification**

```powershell
Get-Item dist/ToupTek4040Control.zip | Select-Object Length
Get-FileHash -Algorithm SHA256 dist/ToupTek4040Control.zip
$buildInfo = Get-Content dist/ToupTek4040Control/BUILD-INFO.txt
$releaseSourceCommit = (($buildInfo | Select-String '^SourceCommit: ').Line -replace '^SourceCommit:\s*', '')
git diff --name-only "$releaseSourceCommit..HEAD" -- ToupTek4040Control.spec official-touptek-sdk.lock.json run_alpaca.py run_gui.py touptek4040 touptek4040.json 使用说明.txt
git status --short
```

Expected: the build-info source commit is non-empty; ZIP size/hash match disk;
release-input diff is empty; worktree is clean; final review has no Critical or
Important findings.

---

## Plan Self-Review Checklist

- Every approved specification section maps to Tasks 1–5.
- Crop type, controller methods, DOM IDs, math exports, and segment fields use
  the same names throughout the plan.
- Existing no-crop viewport URLs and ETags remain backward compatible.
- Server validation, client request ownership, interaction scope, generation
  transition, package contents, hardware non-mutation, and service restoration
  each have explicit tests.
- Phase-1 exclusions are not reintroduced by any task.
- There are no new production dependencies or deferred implementation steps.
