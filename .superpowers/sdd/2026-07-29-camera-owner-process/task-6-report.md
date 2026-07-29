# Task 6 report — Camera V4 review fixes and Web dew heater

## Status and commit

Status: **DONE**

Commit: this report is included in
`fix: align owner-backed camera with ASCOM V4`. The final commit SHA is
reported in the task handoff; a Git commit cannot embed its own SHA.

## Dirty-work audit

The task began at the required base
`9c02e33d79e472b04917acc98d571f9f21c8e181` with inherited Task 13 review
changes already present. Every dirty path was inspected before editing.

Accepted Task 13 product changes were preserved in:

```text
touptek4040/alpaca/app.py
touptek4040/alpaca/camera_api.py
touptek4040/alpaca/request.py
touptek4040/camera_coordinator.py
touptek4040/toupcam_camera.py
```

Accepted Task 13 tests were preserved in `test/alpaca`, plus
`test/unit/test_camera_coordinator.py` and
`test/unit/test_toupcam_acquisition.py`. The authorized full-suite
order-isolation correction is in
`test/integration/test_owner_entrypoints.py`.

Hardware fixtures, hardware tests and result evidence, ConformU scripts and
documentation, `test/fixtures/fake_toupcam.py`, diagnostic/recovery scripts,
temporary-package outputs, and local `toupcam.dll`/`toupcam.lib` artifacts were
kept untouched by Task 6 and left unstaged.

No owned comment or test claimed that an in-process lock or thread provides a
hard timeout for a hung SDK call. Native serialization tests describe ordering
only; hard timeout and exact-process termination guarantees remain in
`CameraOwnerProxy`. No timeout-claim removal was required.

## Camera V4 and owner proof

The preserved review fixes prove:

- GET parameters remain case-insensitive while PUT parameters require exact
  API v12 names.
- Explicit `ClientID` and `ClientTransactionID` accept the complete uint32
  range, including zero.
- Idle `StopExposure` succeeds, unsupported in-progress Stop returns
  `MethodNotImplemented`, and other non-idle states return
  `InvalidOperation`.
- Dark/bias durations below the hardware minimum clamp to 12 microseconds,
  `LastExposureDuration` reports 12 microseconds, and light duration zero is
  invalid.

Fresh spawned-owner tests prove the 12-microsecond acquisition reaches the
owner backend, a light-zero request never reaches acquisition, idle Stop
succeeds, and in-progress unsupported Stop returns the ASCOM error.

The complete official Camera V4 manifest tests remain green. No heater member
was added to the manifest, `SupportedActions`, or `Action`.

## Web heater RED evidence

After adding the Web contract tests and strengthening invalid-request checks
so a missing route could not pass accidentally, the required RED command
reported:

```text
149 collected
23 failed
126 passed
```

Every heater behavior failed for the intended missing state, route,
coordinator method, or UI control. The Camera V4 manifest and common API tests
were already green.

The coordinator heater selection separately reported `14 failed`, all because
`CameraCoordinator.set_heater_level_if_idle` did not yet exist.

## Web heater implementation

The Web-only contract now publishes:

```text
heaterSupported
heaterLevel
heaterMaxLevel
heaterWritable
```

`POST /web/v1/heater` accepts exactly one case-sensitive `heaterLevel` member.
Connection, idle state, support, exact integer type, and device-reported range
are checked under the coordinator synchronization barrier. Exposure rejection
happens before heater capability or write IPC. Successful writes are read back
from the device before the Web cache and response are updated.

The GUI renders Off plus `1..heaterMaxLevel`, reports disconnected,
unsupported, read-failed, current-level, and exposing states, disables changes
outside a supported connected idle state, and rolls back to the last
device-confirmed selection after a failed request.

Tests cover all valid levels, bool/float/string/negative/overflow rejection,
exact JSON shape, unsupported and disconnected cameras, read and write
failures, exposure cache visibility, pre-IPC exposure rejection, concurrent
serialization, and a real spawned owner while the main controller remains
connected. The owner PID differs from the parent PID and records exactly one
heater write.

## Test-order isolation correction

The first exact full run reported `1264 passed, 1 failed`. Full pytest
collection imports native-backend unit modules before executing integration
tests, while one spawned-runtime test asserted those modules were absolutely
absent from the process. The same test passed alone, proving an order-dependent
test assertion rather than a product failure.

The separate fresh-subprocess import proof remains unchanged and still asserts
absolute module absence. The in-process spawned-runtime test now snapshots the
three relevant SDK module names before runtime construction and proves the
runtime adds none. A native-backend-first reproduction then passed `47/47`,
and the fresh import plus spawned runtime proof passed `2/2`.

## Final verification

Required focused Camera V4/owner replay:

```text
363 passed in 5.02s
```

Required Web plus manifest/common suite:

```text
204 passed in 4.12s
```

Required full non-hardware suite:

```text
1265 passed in 67.59s
```

The UI detector reported advisory-only findings in the inherited page copy and
design-system literals; it found no functional or accessibility-blocking
heater issue. The final selectively staged diff passed `git diff --cached
--check`.

## Committed scope

Only the reviewed Task 13 product/tests, Task 6 Web product/tests, the
authorized integration isolation correction, and this report are committed:

```text
.superpowers/sdd/2026-07-29-camera-owner-process/task-6-report.md
test/alpaca
test/integration/test_owner_entrypoints.py
test/unit/test_camera_coordinator.py
test/unit/test_toupcam_acquisition.py
test/web
touptek4040/alpaca/app.py
touptek4040/alpaca/camera_api.py
touptek4040/alpaca/request.py
touptek4040/camera_coordinator.py
touptek4040/toupcam_camera.py
touptek4040/web
touptek4040/web_console.py
```

## Self-review and concerns

Self-review covered accidental Alpaca manifest expansion, parent-side SDK
imports, unconfirmed UI values, optimistic write publication, JSON key/type
coercion, unsupported/disconnected behavior, exposure-time SDK/IPC access,
write concurrency, cooling side effects, owner PID identity, and preservation
of inherited dirty work.

No known Task 6 product concern remains. Hardware was intentionally not
accessed; its inherited evidence and local binary artifacts remain outside the
commit.
