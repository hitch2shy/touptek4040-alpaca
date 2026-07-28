# Task 9 Report: Image Workspace, ImageBytes, Streaming JSON, and FITS

## Initial TDD RED evidence

Task 9 tests were added before any Task 9 production module existed.

Command:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_image_workspace.py test/alpaca/test_imagebytes.py test/alpaca/test_imagearray_json.py test/unit/test_fits_io.py -v
```

Result: pytest exited `1` with four expected collection errors:

- `touptek4040.image_workspace` did not exist;
- `touptek4040.alpaca.image_api` did not exist for the ImageBytes tests;
- `touptek4040.alpaca.image_api` did not exist for the JSON tests;
- `touptek4040.fits_io` was unreachable because the image API import was
  missing.

The original console evidence is retained in `test/results/task9-red.txt`.

## Initial implementation

- Added a thread-safe `ImageWorkspace` with monotonic generation identifiers
  and independent formal and preview slots.
- Every generation owns an independent, immutable, bytes-backed NumPy array.
  Reusing or changing a backend buffer cannot change a published generation,
  and callers cannot re-enable writes on its array.
- The coordinator is the only formal-generation publisher. A completed
  exposure publishes one immutable generation and exposes its immutable Frame;
  Abort, Disconnect, a new exposure, and asynchronous failure clear the formal
  slot.
- Added bounded JSON streaming with the complete Alpaca envelope and ASCOM
  `Value[x][y]` ordering.
- Added official ImageBytes metadata v1 as the fixed 44-byte little-endian
  structure. RAW12 uses UInt16 type 8 and RAW8 uses Byte type 6. Pixel payload
  order is X outer / Y inner, and device errors carry transaction identifiers,
  error metadata, and UTF-8 diagnostic text.
- Registered ImageArray and ImageArrayVariant in the existing Falcon
  application. Explicit `application/imagebytes` negotiation selects binary
  transfer; otherwise ImageArray streams JSON. ImageArrayVariant remains
  `0x400`.
- Added streaming unsigned-16 FITS output with `BZERO=32768`, `BSCALE=1`,
  big-endian signed storage, UTC `DATE-OBS`, exposure, symmetric binning,
  physical ROI origins, gain, sensor, readout metadata, and correct 2880-byte
  header/data padding. RAW8 values retain the same scientific values.
- JSON and ImageBytes use bounded column blocks; FITS uses bounded row blocks.
  No transport creates a full-frame transpose, Python pixel list, output
  accumulator, or temporary file.

Initial implementation commit:

`6dff4f301db4822e275c01a72e7cf3c4ea578939`

Commit subject:

`feat: stream Alpaca images and FITS`

Initial post-commit verification was `570 passed, 4 skipped`; compilation and
diff validation exited `0`. The four skips were the existing opt-in hardware
tests, and no hardware access was attempted.

## Review fix round 1/5

Three Important findings were closed. The separate Accept quality parsing
Minor was intentionally left unchanged and was not added to this task's scope.

### Formal image read linearization

The initial API read connection/error state and formal generation in separate
critical sections. A Disconnect, asynchronous failure, or new exposure could
land between those reads and create a mixed result.

Six deterministic barrier tests cover JSON and ImageBytes for all three
transitions. They pause the workspace read after the old snapshot boundary,
observe the transition thread attempting to enter the coordinator condition,
then release the read. Initial result: `6 failed`; every case returned the
mixed no-image error `0x40B` instead of one stable old generation.

The coordinator now returns one `FormalImageRead` captured under its condition:
effective connection, stored asynchronous error, ImageReady, and the immutable
generation. Image API uses only this result. The strict mapping is:

- disconnected or disconnecting: `0x407`;
- connected stored asynchronous failure: the same bounded `0x500` diagnostic
  returned by the Task 8 state endpoints;
- connected without a ready generation, including a new exposure: `0x40B`;
- otherwise: the captured immutable generation.

The barrier tests now return the stable generation when the read linearizes
first, followed by the correct result after the transition completes.

### Immutable preview generations and complete invalidation

Initial preview publication retained and delivered the backend Frame directly.
Five focused cases failed: a formal exposure retained stale preview state,
callbacks could write the backend array, no preview generation existed, and
Disconnect, preview failure, Stop/New Preview did not consistently clear both
preview representations.

Preview acquisition now publishes through `ImageWorkspace.publish_preview()`.
The coordinator stores and calls back only the immutable generated Frame.
Backend reuse across successive preview frames cannot change earlier callback
frames, callback writes fail, and formal generation pixels and identity remain
unchanged.

One condition-locked clear operation removes both `_preview_frame` and the
workspace preview slot. It is used for StopPreview, Disconnect, formal
StartExposure/session invalidation, preview error or worker termination, and
the beginning of a new preview session. Preview publication never replaces or
clears the formal generation.

### Complete 4096 by 4096 memory verification

The original memory test advanced only the first two stream chunks. It now
fully consumes JSON, ImageBytes, and FITS without retaining output while
`tracemalloc` measures the complete iteration. It verifies:

- each stream emits the expected full-image byte volume;
- every individual chunk remains below 2 MiB;
- additional traced peak memory remains below 16 MiB.

JSON uses a fixed 0 through 4095 ASCII lookup so full RAW12 formatting avoids
millions of transient Python strings while remaining column-streamed.

Two deterministic mutation probes wrap ImageBytes and execute after half of
the image payload has streamed. One retains a full-frame transpose copy and
one retains a full-frame `tolist()` result. Both make the same memory guard
fail, proving that the test detects forbidden materialization in the latter
half rather than only checking startup behavior.

Initial complete-stream and mutation baseline: `3 passed`; the test weakness
was coverage extent rather than an observed production over-allocation.

## Round-1 verification

Focused image, preview, concurrency, JSON, ImageBytes, FITS, and memory set:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_image_workspace.py test/unit/test_camera_concurrency.py test/alpaca/test_imagebytes.py test/alpaca/test_imagearray_json.py test/unit/test_fits_io.py -q
```

Result: `44 passed`.

Complete unit and Alpaca suites:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit test/alpaca -q
```

Result: `582 passed`.

Complete test tree:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test -q
```

Result: `582 passed, 4 skipped`. The skips remain the existing
`TOUPTEK_HARDWARE_TEST=1` opt-in cases; the environment variable was unset and
no hardware access was attempted.

Compilation and whitespace checks both exited `0`:

```powershell
.\.venv64\Scripts\python.exe -m compileall -q touptek4040 test
git diff --check
```

No ledger, approved plan, approved design, or deferred Minor review item was
modified.

## Fix commit

Fix commit subject:

`fix: linearize formal and preview image generations`

Fix commit reference: `HEAD`. The exact immutable hash is recorded in the
Task 9 round-1 handoff after commit because a Git commit cannot contain its own
final hash without changing that hash.
