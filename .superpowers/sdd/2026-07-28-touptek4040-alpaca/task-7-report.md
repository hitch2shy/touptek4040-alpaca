# Task 7 Report: Common Members and Camera V4 Capabilities/Settings API

## TDD failure evidence

The Task 7 endpoint suites and Falcon fixtures were written before any
application or API resource module existed.

Command:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/alpaca/test_common_api.py test/alpaca/test_camera_capabilities.py test/alpaca/test_camera_settings.py -v
```

Result: pytest exited `1` with `197 collected / 197 errors`. The first and
repeated root cause was the intended missing-production failure:

```text
ModuleNotFoundError: No module named 'touptek4040.alpaca.app'
```

The parameterized RED covered every manifest entry outside the two image
routes, as well as common members, capabilities, settings, parsing failures,
connection errors, coordinator state, exposure metadata, Abort/Stop, and
cooling behavior.

A later focused RED established the exposure-duration quantization boundary:
`Duration=0.0000116` was incorrectly rejected before it could round to the
minimum supported 12 microseconds. The unchanged test passed after validation
was aligned with the coordinator's nearest-integer-microsecond rule.

## Manifest and routing

- `create_app(coordinator, config)` creates a Falcon application and registers
  routes directly from `CAMERA_V4_ENDPOINTS`.
- All 86 non-image method-command entries are registered. A parameterized
  coverage test verifies each manifest entry returns an Alpaca JSON envelope
  rather than 404 or 405.
- Commands having both GET and PUT are combined into one Falcon resource so
  neither method shadows the other.
- `imagearray` and `imagearrayvariant` remain unregistered for the image task.
- Unsupported common commands, properties, and methods return explicit
  `0x400`; unsupported Action returns `0x40C`.

## Common API behavior

- Common identification reports Camera interface version 4, the approved
  service name and device model, an ASCII description under 64 characters,
  semantic driver version, and SDK/firmware/hardware/FPGA information.
- `SupportedActions` is an explicit empty array.
- `Connect` and `Disconnect` use the coordinator's asynchronous operations.
- legacy `Connected` PUT parses an Alpaca Boolean case-insensitively and waits
  through `set_connected_sync()`.
- `DeviceState` is assembled from one coordinator snapshot and connected
  temperature/cooler telemetry.

## Camera V4 behavior

- Static geometry is 4096 by 4096 with 9.0 micrometre pixels, monochrome
  sensor type, 1 through 8 symmetric binning, and the approved sensor name.
- Writing either `BinX` or `BinY` synchronizes both values. Each ROI field
  validates its own type/range while temporarily invalid combinations remain
  writable as required by the approved design.
- `StartExposure` validates the final binned ROI against sensor bounds and SDK
  even-coordinate/even-size/minimum-size constraints before delegating to the
  coordinator.
- Numeric gain uses the approved SDK range 100 through 1650. Readout modes are
  `RAW 12-bit` and `RAW 8-bit`; changing mode immediately changes `MaxADU`
  between 4095 and 255.
- `CameraState`, `ImageReady`, and `PercentCompleted` come from coordinator
  snapshots. Last exposure duration and UTC start time come from the
  coordinator's published frame.
- `CanStopExposure=False`; idle `StopExposure` is a no-op and an exposure-time
  call is explicitly `0x400`. `AbortExposure` delegates to the coordinator.
- CCD temperature, target temperature, cooler enable, and clamped cooler
  power use the coordinator. Target values are finite and bounded to
  -50.0 through 40.0 degrees Celsius.
- Bayer offsets, FastReadout, Offset members, Gains, ElectronsPerADU,
  FullWellCapacity, HeatSinkTemperature, SubExposureDuration, PulseGuide, and
  common command members have explicit NotImplemented behavior.

## Protocol and error handling

- GET query and PUT media parameters use the Task 6 case-insensitive request
  primitive. Boolean, integer, float, and enum parsing rejects malformed,
  null, Boolean-as-number, non-finite, and out-of-range values as `0x401`.
- Empty no-argument PUT requests correctly default an absent
  `ClientTransactionID` to zero. Transaction validation occurs before device
  side effects.
- Every device result is a standard JSON Alpaca envelope with a non-zero
  server transaction ID. Expected device errors use HTTP 200.
- Domain errors map to InvalidValue, NotConnected, InvalidOperation,
  NotImplemented, OperationCancelled, or driver error semantics. Unexpected
  native/Falcon/Python errors return a generic HTTP 500 envelope without
  leaking exception details.

## Verification

Focused Task 7 suite:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/alpaca/test_common_api.py test/alpaca/test_camera_capabilities.py test/alpaca/test_camera_settings.py -q
```

Result: `198 passed`.

Complete unit and Alpaca suites:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit test/alpaca -q
```

Result: `381 passed`.

Complete current test tree:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test -q
```

Result: `381 passed, 4 skipped`. The four skips are the existing
`TOUPTEK_HARDWARE_TEST=1` hardware opt-in cases.

Compilation and whitespace validation:

```powershell
.\.venv64\Scripts\python.exe -m compileall -q touptek4040 test
git diff --check
```

Both commands exited `0`.

Scope validation found changes only in:

- `touptek4040/alpaca/`;
- `test/alpaca/`;
- this Task 7 report.

No progress, ledger, approved plan, or approved design file was modified.

## Known contract boundary

The current coordinator accepts only `start_exposure(duration_s, light)` and
internally owns its acquisition configuration. Task 7 therefore persists and
validates the Camera V4 ROI/bin/readout/gain settings at the API boundary, but
does not alter the previously approved coordinator contract to pass those
settings into hardware acquisition.

## Commit

Commit subject:

`feat: expose Camera V4 properties and settings`

Commit reference: `HEAD`. The exact immutable hash is recorded in the Task 7
handoff immediately after commit because a Git commit cannot contain its own
final hash without changing that hash.
