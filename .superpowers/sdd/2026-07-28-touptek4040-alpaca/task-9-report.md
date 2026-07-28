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

## Review fix round 2/5

The RAW12 scientific-range regression was closed without changing content
negotiation or any other deferred review item.

### RED evidence

The focused boundary set initially produced `7 failed, 10 passed`:

- formal and preview workspace publication accepted 4096 and 65535 in all
  four slot/value combinations;
- a formal exposure containing 4096 completed successfully instead of
  atomically entering Error through the Task 8 asynchronous-failure path;
- an invalid preview containing 4096 reached preview publication instead of
  terminating the preview and preserving the formal generation;
- JSON and ImageBytes did not receive the required stable publication-failure
  `0x500` diagnostic.

The passing baseline cases established that 4095 already streamed consistently
through JSON, ImageBytes, and FITS, RAW8 255 remained valid, RAW8 256 remained
rejected, and a 4096 by 4096 validation probe stayed within its memory limit.

### Range validation and publication semantics

- `ImageWorkspace` now completes RAW12 dtype and range validation before
  immutable storage, generation ID allocation, or either slot update.
- UInt16 guarantees non-negative values; one NumPy scalar `max()` verifies the
  remaining upper bound without allocating a frame-sized Boolean array.
- 4095 is inclusive. 4096 and 65535 raise a stable range error.
- Failed direct formal or preview publication leaves the prior slot identity
  unchanged.
- A failed formal exposure publication follows the existing Task 8
  publication-error boundary. CameraState, stored error, cleared formal slot,
  and ImageReady false are committed atomically; state and both image
  transports return the same bounded driver diagnostic as `0x500`.
- A failed preview publication clears preview state and enters preview error
  handling without replacing or mutating the existing formal generation.
- Every successful generation is now guaranteed safe for the JSON 0 through
  4095 ASCII table, UInt16 ImageBytes payload, and unsigned-16 FITS conversion.
- RAW8 normalization and its inclusive 0 through 255 range are unchanged.

### Memory verification

A full 4096 by 4096 RAW12 array filled with 4095 is published while
`tracemalloc` is active. The immutable science copy is 32 MiB and total traced
peak must remain below 40 MiB. This catches an implementation that creates a
16 MiB frame-sized comparison mask while allowing the one required immutable
copy. The scalar maximum implementation passes this bound.

### Round-2 verification

Focused workspace, coordinator, preview, async API, JSON, ImageBytes, and FITS
set: `103 passed`.

Complete test tree: `593 passed, 4 skipped`. The four skips remain the existing
hardware opt-in tests; `TOUPTEK_HARDWARE_TEST` was unset and no hardware access
was attempted.

Compilation and whitespace validation both exited `0`:

```powershell
.\.venv64\Scripts\python.exe -m compileall -q touptek4040 test
git diff --check
```

No Accept quality Minor, ledger, approved plan, or approved design file was
modified.

Round-2 fix commit subject:

`fix: reject out-of-range RAW12 generations`

Round-2 commit reference: `HEAD`; its exact immutable hash is recorded in the
handoff after commit.
