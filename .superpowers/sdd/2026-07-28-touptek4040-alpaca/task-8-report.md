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
