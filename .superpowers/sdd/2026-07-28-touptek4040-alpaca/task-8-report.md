# Task 8 Report: Exposure, Cooling, State, and Failure API

## TDD failure evidence

The three Task 8 end-to-end API suites were written before the operational
settings path, gain contract, asynchronous failure responses, and official
Camera `DeviceState` field set were implemented.

Command:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/alpaca/test_exposure_api.py test/alpaca/test_cooling_api.py test/alpaca/test_async_failures.py -v
```

Result: `28 collected / 5 failed / 23 passed`.

The five intended failures showed that:

- the backend still received full-frame, 1x1, RAW12, gain 100 after the API
  had accepted non-default ROI, binning, RAW8, and gain 1650;
- completed frames did not yet carry gain metadata;
- CameraState, ImageReady, and PercentCompleted did not surface the stored
  asynchronous failure as a diagnostic driver error;
- an Error state could not start a recovery exposure and clear the old error;
- DeviceState returned non-standard `CoolerOn`, omitted `IsPulseGuiding` and
  `TimeStamp`, and returned an unknown PercentCompleted value while idle.

Cooling behavior already present from Task 7 passed its initial 17 Task 8
cases, including current/max TEC power, clamping, unavailable maximum voltage,
connection and Busy errors, native failures, and strict finite target parsing.

Two additional deterministic concurrency RED cycles were recorded:

1. While duration validation was paused, a concurrent Gain PUT completed and
   changed the settings captured by the same StartExposure call. The test
   failed because duration validation and the settings snapshot did not yet
   share one coordinator linearization point.
2. While gain application was paused in the exposure worker, Abort returned
   Idle but the worker subsequently entered `acquire()`. The test failed
   because the exposure session was not rechecked after the native gain write.

Both tests passed unchanged after the corresponding coordinator fixes.

## Operational exposure closure

- Added immutable `ExposureSettings` containing ROI, symmetric binning,
  readout mode, and numeric gain.
- StartExposure parses both Duration and Light first. Light remains required
  and must be a legal Alpaca Boolean, but is ignored because HasShutter is
  False.
- Connected state, exposure-startable state, duration quantization, final
  binned ROI validation, and the Task 7 settings snapshot now linearize under
  one coordinator condition lock. The existing lock order remains coordinator
  condition first and Camera API settings lock second.
- Duration is rounded to the nearest integer microsecond and checked against
  the inclusive 12 through 3,600,000,000 microsecond range inside that atomic
  operation.
- The background worker applies the frozen gain, rechecks the exposure session
  against Abort, and calls `camera.acquire(duration, roi, binning,
  readout_mode)` with the frozen non-default settings. It no longer reloads
  capabilities or hard-codes full-frame, 1x1, RAW12 acquisition.
- StartExposure remains non-blocking and publishes Exposing immediately.
- Starting a new exposure clears the previous formal image and stored
  asynchronous failure. A connected camera in Error can begin a recovery
  exposure.
- Abort returns the camera to Idle, invalidates the session, cancels an active
  backend acquisition, and never publishes a late frame. Abort during gain
  application cannot start the acquisition after returning.
- StopExposure remains an Idle no-op and returns `0x400` while exposing.
- PercentCompleted is available only while exposing and otherwise returns
  `0x40B`.
- Completed Frame metadata contains the applied duration, UTC start, ROI,
  binning, readout mode, and gain. LastExposureDuration and
  LastExposureStartTime come from that published frame.

## Gain contract and ToupCam SDK alignment

The local SDK `en.html` and Python wrapper were checked before implementation.
They define exposure gain through:

- `Toupcam_get_ExpoAGain`;
- `Toupcam_put_ExpoAGain`;
- `Toupcam_get_ExpoAGainRange`.

The value is an unsigned-short percentage, for example 200 means 200 percent.

`CameraContract`, `SimCamera`, and `ToupTekCamera` now expose the same numeric
gain property with the approved 100 through 1650 semantics. The hardware
adapter validates against the SDK-reported range, calls `put_ExpoAGain()` under
its native idle lease, and reads the applied gain before the camera starts.
No gain or acquisition configuration is performed in a callback.

## State and asynchronous failures

- An asynchronous acquisition failure stores one translated diagnostic
  `CameraError`, publishes Error state, clears the formal frame, and keeps
  ImageReady false.
- CameraState, ImageReady, and PercentCompleted return stable Alpaca driver
  error `0x500` with the stored diagnostic text until a new exposure clears
  the failure.
- DeviceState follows the official Camera operational field set and
  best-efforts unavailable telemetry. It returns CameraState, CCDTemperature
  when available, CoolerPower when available, ImageReady, IsPulseGuiding,
  PercentCompleted, and an ISO-8601 UTC TimeStamp. It does not include the
  non-standard CoolerOn field or the unsupported HeatSinkTemperature field.

## Cooling behavior

- CanSetCCDTemperature and CanGetCoolerPower are True.
- CCDTemperature, SetCCDTemperature, CoolerOn, and CoolerPower require an
  active connection.
- Target temperatures accept only finite Alpaca numeric values from -50.0
  through 40.0 degrees Celsius; malformed and non-finite forms never reach the
  backend or leak a built-in Python exception.
- CoolerPower uses current TEC voltage divided by maximum TEC voltage, clamps
  the result to 0 through 100, and returns `0x40B` when the maximum is
  non-finite or non-positive.
- Backend Busy, disconnected, and native errors retain the approved `0x40B`,
  `0x407`, and `0x500` mappings.
- HeatSinkTemperature remains explicitly not implemented because the SDK does
  not provide that measurement.

## Verification

Task 8 API suites:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/alpaca/test_exposure_api.py test/alpaca/test_cooling_api.py test/alpaca/test_async_failures.py -q
```

Result: `29 passed`.

Complete unit and Alpaca suites:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit test/alpaca -q
```

Result: `514 passed`.

Complete current test tree:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test -q
```

Result: `514 passed, 4 skipped`. All four skips are the existing
`TOUPTEK_HARDWARE_TEST=1` opt-in hardware cases. The environment variable was
not enabled, so no unrequested hardware access was attempted. The existing
hardware fixture saves and restores RAW mode, bit depth, gain, ROI, binning,
trigger mode, TEC target/state, and fan state in `finally`.

Compilation and whitespace validation:

```powershell
.\.venv64\Scripts\python.exe -m compileall -q touptek4040 test
git diff --check
```

Both commands exited `0`.

No image transfer route was implemented; that remains Task 9.

No ledger, approved plan, or approved design file was modified.

## Commit

Commit subject:

`feat: implement Camera V4 exposure and cooling API`

Commit reference: `HEAD`. The exact immutable hash is recorded in the Task 8
handoff immediately after commit because a Git commit cannot contain its own
final hash without changing that hash.

## Review fix round 1/5

Four Important review findings were closed with deterministic RED/GREEN
coverage.

### Acquisition and Abort linearization

- Added an acquisition-control handshake shared by the coordinator, simulator,
  and ToupCam backend.
- The coordinator rechecks the exposure session and commits the backend claim
  under one condition-lock critical section. If Abort returns first, a waiting
  worker cannot later claim the backend or issue `StartPullModeWithCallback`
  or `Trigger`.
- The ToupCam claim preserves its abort generation and acquisition flags while
  moving all native configuration before the claim.
- Initial RED evidence: the controlled backend recorded an acquisition after
  Abort, and the simulator/ToupCam backends did not accept the new control
  handshake (`3 failed`). The unchanged tests passed after implementation.

### PercentCompleted start point

- Exposure progress now starts at the committed backend acquisition claim.
  Preview teardown, acquisition-gate waiting, gain application, and ToupCam
  native configuration all continue to report zero percent.
- Slow-gain and slow-native-configuration tests verify StartExposure remains
  non-blocking, CameraState remains Exposing, and PercentCompleted remains
  zero.
- A deliberate mutation restoring the old pre-gain timestamp made both tests
  fail at 100 percent; removing the mutation made both pass.

### Stable asynchronous diagnostics

- Gain and frame-acquisition failures now persist one stable, bounded,
  single-line diagnostic containing the operation, Error state, frozen
  duration/ROI/binning/readout/gain, exception class, allow-listed HRESULT or
  event metadata, a safe summary, and actionable recovery.
- Arbitrary backend text, including multiline or secret-like native content,
  is not reflected.
- CameraState, ImageReady, and PercentCompleted return exactly the same stored
  diagnostic with Alpaca driver error `0x500`.
- Initial RED evidence: all three diagnostic tests failed because raw backend
  strings were exposed and contextual fields were absent. All three passed
  after implementation.

### Failed-owner lifecycle cleanup

- A CameraNotConnectedError during gain or acquisition marks the camera
  unavailable but retains its owner reference.
- Reconnect serializes through the lifecycle lock, aborts and disconnects the
  old owner, and does not construct the replacement until cleanup completes.
- Cleanup failures remain diagnostic and prevent a replacement owner from
  opening. A superseded disconnect releases ownership after its single cleanup
  attempt so saved cleanup errors are not duplicated by a second attempt.
- Initial RED evidence: gain, acquisition, and cleanup-failure tests all showed
  the old owner was dropped before cleanup (`3 failed`). All three passed after
  implementation.

### Review-fix verification

Focused coordinator/backend/Alpaca regression set:

```powershell
.\.venv64\Scripts\python.exe -m pytest -q test/unit/test_camera_coordinator.py test/unit/test_sim_camera.py test/unit/test_toupcam_acquisition.py test/unit/test_toupcam_settings.py test/unit/test_camera_concurrency.py test/alpaca/test_async_failures.py
```

Result: `123 passed`.

Unit suite: `141 passed`.

Alpaca suite: `383 passed`.

Complete test tree: `524 passed, 4 skipped`. The four existing hardware tests
remain opt-in; `TOUPTEK_HARDWARE_TEST` was unset, so no hardware access was
attempted.

Compilation and whitespace validation both exited `0`:

```powershell
.\.venv64\Scripts\python.exe -m compileall -q touptek4040 test
git diff --check
```

No ledger, approved plan, or approved design file was modified.

## Review fix round 2/5

Three remaining Important findings were closed with focused RED/GREEN coverage.

### Bounded, allow-listed asynchronous diagnostics

- HRESULT metadata now accepts only a labeled, canonical eight-digit
  hexadecimal value in the unsigned 32-bit range.
- Event metadata accepts only the fixed ToupCam error tokens `0x0080` through
  `0x0083`; arbitrary decimal, long hexadecimal, and secret-like tokens are
  discarded.
- Exception causes are normalized through an exact allow-list. Dynamic or
  attacker-controlled class names become `Exception`.
- Every numeric exposure field uses bounded serialization, and the final
  diagnostic has an unconditional 700-character ceiling with a safe structured
  fallback.
- A combined adversarial case uses a 2,000-character class name, multiple
  2,000-character strings, oversized and secret-like event values, an invalid
  nine-digit HRESULT, and one valid HRESULT/event pair. CameraState,
  ImageReady, and PercentCompleted return the same safe text without reflecting
  the untrusted content.
- Initial RED result: `1 failed`; the diagnostic was 4,312 characters and
  reflected the dynamic cause and event content. The complete async-failure
  file passed after the fix (`6 passed`).

### Persistent failed-owner quarantine

- The coordinator retains a failed owner and its cleanup diagnostic across
  every Connect or Disconnect retry. Lifecycle cleanup remains serialized;
  concurrent Connect requests collapse into the active attempt.
- Repeated abort/disconnect failures are de-duplicated by error type and text.
  No replacement factory is called until one complete retry succeeds.
- ToupCam no longer clears its handle before `Close` succeeds. A failed close
  leaves the handle disconnecting and unusable, so no native operation or
  reconnect can reuse it. A later serialized disconnect retries `Close`;
  success then releases the handle. This treats a Close exception as
  indeterminate and quarantines indefinitely rather than assuming the
  non-idempotent operation succeeded.
- Initial RED result: `2 failed`; a second Connect opened a replacement and a
  failed Close had already discarded the handle. The focused owner lifecycle
  set passed after the fix (`5 passed`).

### Pre-claim native configuration exclusivity

- ToupCam now publishes an explicit acquisition-configuring state before the
  first capture-setting native call.
- The state remains active through ROI/bin/readout/exposure/trigger
  configuration and ends atomically when the acquisition claim begins or when
  configuration/cancellation fails.
- Idle native settings and telemetry operations return CameraBusyError during
  this interval. Tests cover CCD temperature, TEC voltage and cooler power,
  cooler state, target temperature, gain, and explicit capture settings; none
  enters the handle while configuration is blocked.
- Disconnect continues to wait on the native lease. Abort invalidates the
  coordinator control without taking the configuration handle, so releasing
  the blocked configuration exits without StartPull or Trigger and without
  deadlock. PercentCompleted remains zero throughout.
- Initial RED result: all ten idle-operation cases failed because they entered
  or waited inside the handle; the existing disconnect wait case already
  passed. The completed focused configuration set passed (`12 passed`).

### Round-2 verification

Focused coordinator, ToupCam acquisition/settings, and async API files:
`107 passed`.

Unit suite: `154 passed`.

Alpaca suite: `384 passed`.

Complete test tree: `538 passed, 4 skipped`. The four existing hardware tests
remain opt-in; `TOUPTEK_HARDWARE_TEST` was unset, so no hardware access was
attempted.

Compilation and whitespace checks both exited `0`:

```powershell
.\.venv64\Scripts\python.exe -m compileall -q touptek4040 test
git diff --check
```

No Minor review item, ledger, approved plan, or approved design file was
modified.
