# Task 5 Report: Camera Coordinator and Formal State Machine

## TDD failure evidence

### RED 1: coordinator and broker behavior

Command:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_camera_coordinator.py test/unit/test_event_broker.py test/unit/test_camera_concurrency.py -v
```

Result: pytest exited 1 during collection with three expected errors:

- `ModuleNotFoundError: No module named 'touptek4040.camera_coordinator'`
- `ModuleNotFoundError: No module named 'touptek4040.event_broker'`
- The concurrency suite failed to collect for the same missing coordinator module.

This was the intended failure: the tests named the new public behavior before
either production module existed.

### RED 2: coordinator-only capabilities and cooling gateway

Command:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_camera_coordinator.py -v
```

Result: 9 passed and 2 failed. Both failures were the intended missing
behavior:

- `CameraCoordinator` had no `capabilities` attribute.
- `CameraCoordinator` had no `temperature_c` attribute.

The forwarding gateway was then implemented so downstream API layers never
need to bypass the coordinator to reach the existing `CameraContract`
capabilities or cooling interface.

## Implementation summary

- Added a lock-protected `CameraCoordinator` with the formal ASCOM
  `CameraState` values and immutable `CameraSnapshot` state.
- Connection and disconnection run in background lifecycle workers.
  `set_connected_sync(bool)` waits on the same condition without holding a
  hardware call lock.
- Formal exposure starts non-blockingly and delegates the synchronous
  `CameraContract.acquire()` call to a daemon worker.
- Exposure session IDs are invalidated by abort, disconnect, and each new
  exposure. A worker may publish a frame or error only when its session,
  camera owner, connection, and state are still current.
- `ImageReady` becomes false at exposure start and true only when the current
  formal frame commits. Abort returns to Idle, asynchronous failure enters
  Error, and `PercentCompleted` exists only while Exposing.
- Preview uses repeated single-frame acquisitions, a separate preview frame
  slot, and a separate preview session. Starting a formal exposure invalidates
  Preview first; the formal worker aborts and joins the Preview worker before
  acquiring, so the two modes cannot overlap.
- Added a bounded, thread-safe event broker. Every subscriber has an
  independent fixed-size queue; a full queue drops its oldest event and keeps
  the newest event without blocking publishers.
- Added coordinator-only forwarding for capabilities and the existing cooling
  contract.

No shared contract, error, model, simulator, or ToupCam adapter type required
modification. ROI passed to `CameraContract.acquire()` remains binned-output
geometry.

## Verification

- Focused Task 5 suite: `18 passed`
- Complete unit suite: `101 passed`
- Complete test tree: `101 passed, 4 skipped`
- Hardware tests: skipped by the existing
  `TOUPTEK_HARDWARE_TEST=1` opt-in rule
- Concurrency stress repetition: `30 concurrency iterations passed`
- `python -m compileall -q touptek4040 test`: exit 0
- `git diff --check`: exit 0

## Commit

Commit subject: `feat: coordinate camera lifecycle and exposure state`

Commit reference: `HEAD`. The exact immutable hash is recorded in the Task 5
handoff because a Git commit cannot contain its own final hash without changing
that hash.

---

## Fix round 1/5

### TDD failure evidence

The deterministic regression suite was run before production changes:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_camera_coordinator.py test/unit/test_event_broker.py test/unit/test_camera_concurrency.py -v
```

Result: `15 failed, 23 passed`. The failures reproduced all six Important
review findings:

- Duration values below 0.5 microseconds caused `ZeroDivisionError`; 11 µs and
  3,600.000001 s were incorrectly accepted.
- Both connect-to-disconnect and disconnect-to-connect supersede paths lost
  the old owner's cleanup errors.
- Completion, Exposing, and Error events could be published after newer
  exposure, abort, disconnect, or reconnect events.
- Abort followed by exposure or Preview entered a second camera acquisition
  while the old worker was still in delayed cleanup.
- Busy, NotConnected, and multi-error Preview abort failures returned before
  the Preview worker exited and left the snapshot healthy.
- PercentCompleted reached 100 while Preview cancellation or its callback
  still prevented the formal acquisition from starting.

### Fix implementation

- State mutation and event publication now occur under the same coordinator
  condition lock, establishing one event order matching state transitions.
- A coordinator acquisition gate spans each complete low-level acquisition
  and its state commit. New formal and Preview workers wait behind stale
  workers in the background while `start_exposure()` remains non-blocking.
- Superseded lifecycle workers propagate flattened cleanup causes through the
  serialized lifecycle operation. A new owner is not opened when the previous
  owner cleanup failed.
- Exposure input is rounded once to integer microseconds, then synchronously
  validated against the inclusive 12 through 3,600,000,000 µs range before
  any state mutation.
- Preview stopping always joins the invalidated worker. Abort errors retain
  their CameraBusy, CameraNotConnected, or CameraCleanup type and causes;
  NotConnected also updates the coordinator connection snapshot.
- Exposing is immediately visible with PercentCompleted 0, but the monotonic
  exposure clock starts only after Preview teardown and acquisition-gate
  entry, immediately before the formal `acquire()` call.

The bottom-level `CameraContract`, simulator, and ToupCam adapter remain
unchanged. The three reviewer-recorded Minor findings were not modified.

### Fix verification

- Focused Task 5 suite: `38 passed`
- Complete unit suite: `121 passed`
- Final complete test, compile, and diff results are recorded in the fix
  handoff.

Fix commit subject:
`fix: linearize coordinator events and acquisition cleanup`

Fix commit reference: `HEAD`; the final immutable hash is recorded in the
handoff for the same self-reference reason described above.
