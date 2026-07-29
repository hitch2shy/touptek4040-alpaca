# Isolated Camera Owner Process Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move every real ToupTek SDK call into a supervised child process so a hung DLL call cannot hang the Alpaca/Web application, while adding strict JSON configuration and preserving the complete Camera V4 contract.

**Architecture:** `CameraOwnerProxy` remains behind the existing synchronous camera contract in the main process. A Windows-spawned owner process uniquely loads the x64 SDK and owns the handle; versioned control messages use private `multiprocessing.Connection` objects and frame pixels use validated shared memory. Any timeout terminates the owner generation, fails the current operation exactly once, creates a fresh disconnected owner, and requires an explicit client reconnect.

**Tech Stack:** Python 3.13, `multiprocessing` with Windows `spawn`, `multiprocessing.shared_memory`, NumPy 2, Falcon 4, Waitress 3, local ToupTek SDK, pytest, PowerShell, PyInstaller.

## Global Constraints

- Preserve every current uncommitted Task 13 review fix in the shared worktree; do not reset, restore or overwrite those files.
- The only approved physical device is model `sMAX16AM-U3-CL-GPS`, serial `TP241025143218C92B01151AEC40CB7`.
- Real-camera startup must never fall back to simulation.
- `SimCamera` remains in-process and keeps the fixed simulator UID.
- Only the owner process may import the ToupTek SDK wrapper or load `toupcam.dll` during real-camera runtime.
- The current `CameraContract`, `CameraCoordinator`, Alpaca Camera V4 routes, Web endpoints and GUI behavior remain externally compatible.
- A timed-out or crashed exposure is never automatically retried and never publishes an image.
- A replacement owner remains disconnected until an explicit Connect.
- Dew-heater control is Web-only: do not add Alpaca Camera endpoints or
  advertise an Alpaca `Action`.
- Dew-heater levels use the device-reported integer range
  `0..TOUPCAM_OPTION_HEAT_MAX`; `0` means off. Do not assume a fixed maximum.
- Heater writes are rejected during exposure, while the confirmed heater level
  remains readable. Normal Disconnect and shutdown restore the level captured
  at Connect.
- All owner timeout values are finite and configurable; defaults are startup 10 s, Connect 20 s, ordinary command 5 s, exposure `duration * 1.02 + 10 s`, Abort 2 s and Disconnect 10 s.
- `touptek4040.json` is the primary distributed configuration; explicitly selected legacy INI remains readable.
- All tests, fixtures, scripts and generated evidence belong under `test/`.
- Hardware tests must snapshot and restore every mutated setting in `finally`, close the handle and report restoration errors honestly.
- ConformU artifacts must be fresh, structurally validated and fail on every unexplained finding.
- Each task ends with a clean, independently reviewable commit. Do not commit unrelated dirty files with an earlier task.

---

### Task 1: Strict JSON Configuration and Timeout Model

**Files:**
- Modify: `touptek4040/config.py`
- Modify: `touptek4040/__init__.py`
- Modify: `run_alpaca.py`
- Modify: `touptek4040/alpaca/management.py`
- Create: `touptek4040.json`
- Modify: `test/unit/test_config.py`
- Modify: `test/alpaca/test_setup_pages.py`
- Create: `test/unit/test_json_config.py`

**Interfaces:**
- Produces: `OwnerConfig`
- Produces: `SecurityConfig`
- Produces: `load_config(path: Path | None, overrides: Mapping[str, object]) -> AppConfig`
- Produces: `load_json_object_strict(path: Path) -> dict[str, object]`
- Consumes: existing `AppConfig`, command-line dotted overrides and stable setup-file identity checks.

- [ ] **Step 1: Write strict JSON RED tests**

Add tests that require exact sections and types:

```python
def test_json_is_primary_and_owner_timeouts_are_loaded(tmp_path):
    path = tmp_path / "touptek4040.json"
    path.write_text(
        json.dumps(
            {
                "server": {"host": "127.0.0.1", "port": 11111, "discovery_port": 32227},
                "camera": {
                    "source": "toupcam",
                    "serial": EXPECTED_SERIAL,
                    "readout_mode": "RAW 12-bit",
                    "gain": None,
                },
                "owner": {
                    "startup_timeout_s": 10.0,
                    "connect_timeout_s": 20.0,
                    "command_timeout_s": 5.0,
                    "exposure_overhead_s": 10.0,
                    "exposure_multiplier": 1.02,
                    "abort_timeout_s": 2.0,
                    "disconnect_timeout_s": 10.0,
                },
                "web": {"enabled": True},
                "logging": {"level": "INFO", "directory": "logs"},
                "security": {"trusted_lan_only": True},
            }
        ),
        encoding="utf-8",
    )
    config = load_config(path, {})
    assert config.owner.connect_timeout_s == 20.0
    assert config.camera.serial == EXPECTED_SERIAL
    assert config.logging.directory == (tmp_path / "logs").resolve()
```

Cover duplicate JSON keys, unknown keys, missing required objects, booleans used as numbers, NaN/Infinity, zero/negative timeouts, wrong types, JSON-relative paths, override precedence, legacy INI compatibility and `.json`/`.ini` suffix selection.

- [ ] **Step 2: Run the RED configuration tests**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_json_config.py test/unit/test_config.py -v
```

Expected: failures because `OwnerConfig`, `SecurityConfig` and strict JSON loading do not exist.

- [ ] **Step 3: Implement exact dataclasses and strict loader**

Add:

```python
@dataclass(frozen=True)
class OwnerConfig:
    startup_timeout_s: float = 10.0
    connect_timeout_s: float = 20.0
    command_timeout_s: float = 5.0
    exposure_overhead_s: float = 10.0
    exposure_multiplier: float = 1.02
    abort_timeout_s: float = 2.0
    disconnect_timeout_s: float = 10.0


@dataclass(frozen=True)
class SecurityConfig:
    trusted_lan_only: bool = True
```

Use
`json.loads(raw_text, object_pairs_hook=reject_duplicate_pairs, parse_constant=reject_nonfinite)`
to reject duplicate keys and NaN/Infinity. Validate an explicit allow-list for
every section. Preserve the legacy INI path only when the selected suffix is
`.ini`; reject other suffixes.

- [ ] **Step 4: Convert stable setup writes to format-aware JSON**

Retain `_ConfigurationIdentity`, same-file checks, serialized POST handling,
temporary-file fsync and atomic replacement. For JSON, update only
`server.host`, `server.port` and `server.discovery_port` in the parsed object,
write UTF-8 with two-space indentation and a trailing newline. Continue using
the INI writer only for an explicitly selected legacy `.ini`.

- [ ] **Step 5: Add the distributed JSON example**

Create `touptek4040.json` with the approved serial and exact timeout defaults
from the design. Do not include secrets or machine-specific absolute paths.

- [ ] **Step 6: Run configuration and setup regression tests**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_json_config.py test/unit/test_config.py test/alpaca/test_setup_pages.py test/unit/test_entrypoints.py -v
```

Expected: all pass.

- [ ] **Step 7: Commit JSON configuration**

```powershell
git add touptek4040/config.py touptek4040/__init__.py run_alpaca.py touptek4040/alpaca/management.py touptek4040.json test/unit/test_json_config.py test/unit/test_config.py test/alpaca/test_setup_pages.py
git commit -m "feat: add strict JSON runtime configuration"
```

---

### Task 2: Versioned IPC Schema and Shared-Frame Ownership

**Files:**
- Create: `touptek4040/owner_protocol.py`
- Create: `touptek4040/shared_frames.py`
- Create: `test/unit/test_owner_protocol.py`
- Create: `test/unit/test_shared_frames.py`

**Interfaces:**
- Produces: `PROTOCOL_VERSION: int`
- Produces: `JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]`
- Produces: `OwnerRequest`, `OwnerResponse`, `OwnerError`, `FrameDescriptor`
- Produces: `encode_request(request: OwnerRequest) -> dict[str, object]`
- Produces: `decode_request(payload: object) -> OwnerRequest`
- Produces: `decode_response(payload: object, expected: OwnerRequest) -> OwnerResponse`
- Produces: `SharedFrameRegistry.publish(pixels: np.ndarray, metadata: Mapping[str, JsonValue]) -> FrameDescriptor`
- Produces: `SharedFrameRegistry.consume(descriptor) -> np.ndarray`
- Produces: `SharedFrameRegistry.release(descriptor) -> None`

- [ ] **Step 1: Write schema RED tests**

Require rejection of unknown fields, bool-as-int request IDs, negative or
overflow IDs, stale owner generations, wrong protocol versions, operation
mismatches, result-plus-error responses, malformed error payloads and duplicate
response IDs.

```python
def test_response_generation_and_request_id_must_match():
    expected = OwnerRequest(1, 7, 42, "connect", {})
    with pytest.raises(OwnerProtocolError):
        decode_response(
            {
                "protocol_version": 1,
                "owner_generation": 6,
                "request_id": 42,
                "operation": "connect",
                "result": {},
            },
            expected,
        )
```

- [ ] **Step 2: Write shared-memory RED tests**

Cover RAW8 and RAW12 round trips, read-only returned arrays, wrong byte length,
wrong shape, unsupported dtype, impossible dimensions, `max_adu` mismatch,
owner crash before publication, release idempotency and registry-file
replacement.

- [ ] **Step 3: Verify both test files fail**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_owner_protocol.py test/unit/test_shared_frames.py -v
```

Expected: collection failures because the modules do not exist.

- [ ] **Step 4: Implement frozen schema types**

Use frozen dataclasses with explicit validators. `arguments`, `result` and
diagnostics contain only `None`, bool, int, finite float, str, list and dict
values with string keys. Do not accept arbitrary Python objects from pickle.

- [ ] **Step 5: Implement atomic shared-memory registry**

Use product-prefixed names containing owner generation, request ID and a random
nonce. Persist only names created by this application to an atomically replaced
JSON registry. `consume` opens by name, validates exact `prod(shape) *
dtype.itemsize`, copies into a new array, marks it read-only, closes the mapping
and unlinks it. Cleanup only registry-listed entries whose recorded owner PID
is dead.

- [ ] **Step 6: Run protocol and shared-memory tests**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_owner_protocol.py test/unit/test_shared_frames.py -v
```

Expected: all pass, including a 4096 x 4096 RAW12 allocation/copy test with
bounded peak memory evidence.

- [ ] **Step 7: Commit the IPC foundation**

```powershell
git add touptek4040/owner_protocol.py touptek4040/shared_frames.py test/unit/test_owner_protocol.py test/unit/test_shared_frames.py
git commit -m "feat: define camera owner IPC and shared frames"
```

---

### Task 3: Spawn-Safe Camera Owner Process

**Files:**
- Create: `touptek4040/camera_owner.py`
- Create: `touptek4040/owner_backend.py`
- Modify: `touptek4040/camera_contract.py`
- Modify: `touptek4040/sim_camera.py`
- Modify: `touptek4040/toupcam_camera.py`
- Modify: `test/fixtures/fake_toupcam.py`
- Create: `test/fixtures/fake_owner_backend.py`
- Create: `test/unit/test_camera_owner.py`
- Modify: `test/unit/test_toupcam_acquisition.py`
- Modify: `test/unit/test_toupcam_settings.py`
- Modify: `test/unit/test_sim_camera.py`

**Interfaces:**
- Produces: `OwnerStartupSpec`
- Produces: `camera_owner_main(command_connection, control_connection, startup_spec, generation, registry_path) -> None`
- Produces: `OwnerBackend` protocol
- Produces: `CameraContract.heater_supported: bool`,
  `CameraContract.heater_max_level: int` and the read/write
  `CameraContract.heater_level: int` property.
- Consumes: `ToupTekCamera` only inside the owner process.

- [ ] **Step 1: Write real-spawn owner RED tests**

Use `multiprocessing.get_context("spawn")`, not fork or an in-process thread.
The fake backend records its PID and supports deterministic connect, settings,
exposure, Abort and disconnect operations.

```python
def test_owner_runs_backend_in_a_different_spawned_process(owner_harness):
    response = owner_harness.request("diagnostics", {})
    assert response["backend_pid"] != os.getpid()
```

Add tests proving the main process does not import the SDK loader when the
owner harness is constructed. Add heater tests proving the backend PID differs
from the parent PID.

- [ ] **Step 2: Write heater backend RED tests**

Require exact SDK alignment:

```python
assert camera.heater_supported is bool(
    camera.model_flag & sdk.TOUPCAM_FLAG_HEAT
)
assert camera.heater_max_level == handle.get_Option(
    sdk.TOUPCAM_OPTION_HEAT_MAX
)
camera.heater_level = 0
assert handle.get_Option(sdk.TOUPCAM_OPTION_HEAT) == 0
```

Also cover levels `1..heater_max_level`, bool, float, string, negative and
overflow rejection without an SDK write; unsupported devices; simulator
behavior; and proof that heater writes do not change TEC, TEC target or fan.

- [ ] **Step 3: Run owner and heater RED tests**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_camera_owner.py test/unit/test_toupcam_settings.py test/unit/test_sim_camera.py -v
```

Expected: failures because the owner entry point and internal heater contract
do not exist.

- [ ] **Step 4: Implement the internal heater contract**

Add exact integer-valued internal properties to `CameraContract`,
`ToupTekCamera` and `SimCamera`. `ToupTekCamera` checks
`TOUPCAM_FLAG_HEAT`, reads `TOUPCAM_OPTION_HEAT_MAX`, validates the current
level in `0..max`, and uses `TOUPCAM_OPTION_HEAT` for reads and writes.
The simulator uses a configurable finite maximum and never starts an owner.

- [ ] **Step 5: Implement sequential owner dispatch**

The owner validates every request, dispatches an explicit operation allow-list
and returns only schema-validated results. Supported operations are:

```text
handshake, connect, disconnect, capabilities, acquire, abort,
get_gain, set_gain, get_temperature, get_target_temperature,
set_target_temperature, get_cooler_on, set_cooler_on,
get_tec_voltage, get_cooler_power,
get_heater_capabilities, get_heater_level, set_heater_level,
diagnostics, release_frame, shutdown
```

No `getattr`-based arbitrary method dispatch is permitted.

- [ ] **Step 6: Add independent Abort control handling**

Run a small control thread in the owner that reads only `abort` and `shutdown`
control messages. It may call the backend Abort concurrently with an
acquisition, matching the existing native ownership model. Any other control
operation is rejected.

- [ ] **Step 7: Publish frames through shared memory**

On successful acquire, write pixels to `SharedFrameRegistry`, return a
`FrameDescriptor` plus the non-pixel `Frame` metadata and retain ownership
until the proxy acknowledges release. On any pre-response exception, unlink
the segment in the owner `finally`.

- [ ] **Step 8: Snapshot and restore the heater**

After successful Connect, snapshot the exact current heater level together
with the existing mutable hardware state. Normal Disconnect, shutdown and the
outer cleanup `finally` restore it before closing the handle. Restoration
errors are reported honestly. A forced owner termination makes no restoration
claim.

- [ ] **Step 9: Make owner cleanup unconditional**

The outermost `finally` aborts/stops the backend, calls disconnect, cleans
owner-created segments and closes both connections. It records cleanup errors
in a bounded final diagnostic message when the command pipe remains usable.

- [ ] **Step 10: Keep the in-process backend tests green**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_camera_owner.py test/unit/test_toupcam_acquisition.py test/unit/test_toupcam_settings.py test/unit/test_sim_camera.py -v
```

Expected: all pass.

- [ ] **Step 11: Commit the owner process**

```powershell
git add touptek4040/camera_owner.py touptek4040/owner_backend.py touptek4040/camera_contract.py touptek4040/sim_camera.py touptek4040/toupcam_camera.py test/fixtures/fake_toupcam.py test/fixtures/fake_owner_backend.py test/unit/test_camera_owner.py test/unit/test_toupcam_acquisition.py test/unit/test_toupcam_settings.py test/unit/test_sim_camera.py
git commit -m "feat: run ToupTek SDK in a camera owner process"
```

---

### Task 4: Camera Owner Proxy, Timeouts and Generation Recovery

**Files:**
- Create: `touptek4040/camera_proxy.py`
- Modify: `touptek4040/errors.py`
- Modify: `touptek4040/camera_contract.py`
- Create: `test/unit/test_camera_proxy.py`
- Create: `test/unit/test_owner_failures.py`

**Interfaces:**
- Produces: `CameraOwnerProxy(CameraContract)`
- Produces: `OwnerProcessFactory`
- Produces: `OwnerSupervisor`
- Produces: proxied heater capability, maximum, read and write operations.
- Consumes: `OwnerConfig`, `OwnerStartupSpec`, owner protocol and shared-frame registry.

- [ ] **Step 1: Write timeout and no-retry RED tests**

Inject hangs for startup, Open, a setting, Trigger, frame publication and
Disconnect. Count fake backend invocations.

```python
def test_timed_out_exposure_is_not_retried(proxy, backend_counter):
    backend_counter.hang_on("acquire")
    with pytest.raises(CameraTimeoutError):
        proxy.acquire(0.01, Roi(0, 0, 16, 16), 1, ReadoutMode.RAW12)
    assert backend_counter.calls("acquire") == 1
    assert proxy.connected is False
```

Require a new owner generation after termination and an explicit Connect
before another acquire. Add a heater-write timeout case: the fake backend
invocation count is one, the write is never retried, the replacement owner is
disconnected and no cached level is reported as confirmed.

- [ ] **Step 2: Write protocol-corruption and shared-memory RED tests**

Inject stale generation, wrong request ID, duplicate response, owner exit,
wrong frame length and orphaned segment. Each must terminate the owner and
leave no published frame.

- [ ] **Step 3: Verify proxy tests fail**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_camera_proxy.py test/unit/test_owner_failures.py -v
```

Expected: failures because `CameraOwnerProxy` does not exist.

- [ ] **Step 4: Implement one-command supervision**

Guard the normal connection with a lock. Allocate request IDs monotonically
within one owner generation. Poll the connection until the exact deadline.
On timeout or protocol error:

```python
def _fail_owner(self, error: CameraError) -> None:
    self._terminate_exact_process()
    self._registry.cleanup_generation(self._generation)
    self._connected = False
    self._start_idle_owner()
    raise error
```

Never resend the failed request.

- [ ] **Step 5: Implement operation-specific deadlines**

Use startup, connect, ordinary command, Abort and Disconnect defaults from
`OwnerConfig`. For acquire:

```python
deadline_seconds = (
    actual_clamped_duration_s * owner.exposure_multiplier
    + owner.exposure_overhead_s
)
```

The owner reports the clamped duration in its command-start acknowledgement;
the proxy rejects a non-finite or out-of-range value.

- [ ] **Step 6: Implement bounded process termination**

Close IPC endpoints, call `terminate`, join for a short fixed process
termination allowance, then call `kill` only if the exact child is still
alive. Join again and record the exit code. Do not enumerate or terminate any
unrelated process.

- [ ] **Step 7: Implement Frame reconstruction**

Validate the descriptor, copy the shared array, release/unlink it and create a
frozen `Frame`. A failure after opening shared memory still closes the mapping
and attempts exact-name cleanup.

- [ ] **Step 8: Implement heater proxy operations**

Use the ordinary command timeout for heater reads and writes. Reject writes
while `exposing` before sending IPC. Preserve one confirmed level only after a
successful owner response; invalidate it when the owner generation fails.

- [ ] **Step 9: Run proxy and coordinator tests**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_camera_proxy.py test/unit/test_owner_failures.py test/unit/test_camera_coordinator.py test/unit/test_camera_concurrency.py -v
```

Expected: all pass, including no heater retry and explicit reconnect after a
heater IPC timeout.

- [ ] **Step 10: Commit proxy supervision**

```powershell
git add touptek4040/camera_proxy.py touptek4040/errors.py touptek4040/camera_contract.py test/unit/test_camera_proxy.py test/unit/test_owner_failures.py
git commit -m "feat: supervise camera owner timeouts and recovery"
```

---

### Task 5: Runtime, GUI and Frozen-Application Integration

**Files:**
- Modify: `run_alpaca.py`
- Modify: `run_gui.py`
- Modify: `touptek4040/gui.py`
- Modify: `touptek4040/toupcam_loader.py`
- Modify: `test/unit/test_entrypoints.py`
- Create: `test/integration/test_owner_entrypoints.py`

**Interfaces:**
- `build_coordinator(config)` produces a `CameraCoordinator` backed by
  `CameraOwnerProxy` for `source=toupcam`.
- `build_coordinator(config)` continues to use `SimCamera` for `source=sim`.
- Both executable entry points call `multiprocessing.freeze_support()`.

- [ ] **Step 1: Write integration RED tests**

Require that real-mode coordinator construction starts an owner without
loading the SDK in the parent, simulator construction starts no child, and a
shutdown timeout leaves no child or shared memory. Also require that heater
reads and writes in real mode execute in the owner PID, while simulator heater
operations remain in-process.

- [ ] **Step 2: Verify RED**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/integration/test_owner_entrypoints.py test/unit/test_entrypoints.py -v
```

- [ ] **Step 3: Replace the in-process real-camera factory**

Resolve the configured serial without opening every device in the main
process. The owner performs authoritative raw enumeration and serial
verification on Connect. Construct:

```python
CameraCoordinator(
    lambda _source: CameraOwnerProxy(
        serial=resolved.camera.serial,
        owner_config=resolved.owner,
        runtime_directory=resolved.logging.directory,
    ),
    "toupcam",
)
```

No parent module imports `ToupTekCamera` or calls `enumerate_cameras`.

- [ ] **Step 4: Integrate deterministic shutdown**

Runtime and GUI cleanup call coordinator disconnect, then close the proxy
supervisor. If normal Disconnect times out, the proxy terminates the owner and
returns a stable cleanup error while completing main-process shutdown.

- [ ] **Step 5: Add Windows spawn/freeze guards**

Call `multiprocessing.freeze_support()` inside both `main()` functions before
building configuration or GUI state. Keep the owner target at module top
level.

- [ ] **Step 6: Run entry point, simulator and Web smoke tests**

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/integration/test_owner_entrypoints.py test/unit/test_entrypoints.py test/smoke/test_simulator_roundtrip.py test/web -v
```

Expected: all pass; simulator smoke spawns no owner.

- [ ] **Step 7: Commit runtime integration**

```powershell
git add run_alpaca.py run_gui.py touptek4040/gui.py touptek4040/toupcam_loader.py test/unit/test_entrypoints.py test/integration/test_owner_entrypoints.py
git commit -m "feat: integrate isolated owner into runtime apps"
```

---

### Task 6: Consolidate Camera V4 and Task 13 Review Fixes

**Files:**
- Preserve and modify: `touptek4040/alpaca/request.py`
- Preserve and modify: `touptek4040/alpaca/camera_api.py`
- Preserve and modify: `touptek4040/alpaca/app.py`
- Preserve and modify: `touptek4040/camera_coordinator.py`
- Preserve and modify: `touptek4040/toupcam_camera.py`
- Modify: `touptek4040/web_console.py`
- Modify: `touptek4040/web/index.html`
- Modify: `touptek4040/web/app.js`
- Modify: `touptek4040/web/settings-sync.js`
- Modify: `touptek4040/web/app.css`
- Preserve and modify: `test/alpaca/test_request_parsing.py`
- Preserve and modify: `test/alpaca/test_protocol_errors.py`
- Preserve and modify: `test/alpaca/test_exposure_api.py`
- Preserve and modify: `test/alpaca/test_camera_settings.py`
- Preserve and modify: `test/alpaca/test_imagearray_json.py`
- Preserve and modify: `test/alpaca/test_imagebytes.py`
- Preserve and modify: `test/unit/test_camera_coordinator.py`
- Preserve and modify: `test/unit/test_toupcam_acquisition.py`
- Modify: `test/web/conftest.py`
- Modify: `test/web/test_web_api.py`
- Create: `test/web/test_heater_control.py`

**Interfaces:**
- GET query parameter names remain case-insensitive.
- PUT form parameter names use exact API v12 casing.
- Explicit `ClientID` and `ClientTransactionID` accept the complete uint32
  range including zero.
- Idle `StopExposure` succeeds; unsupported in-progress Stop returns
  MethodNotImplemented.
- Dark/bias duration zero is clamped to the hardware minimum.
- Web status exposes `heaterSupported`, `heaterLevel`, `heaterMaxLevel` and
  `heaterWritable`; Web updates accept only exact integer `heaterLevel`.
- Produces:
  `CameraCoordinator.set_heater_level_if_idle(level: int) -> None`.
- Alpaca Camera V4 manifest, `SupportedActions` and `Action` remain unchanged.

- [ ] **Step 1: Audit the existing dirty patch without discarding it**

Use `git diff` to enumerate every current Task 13 review change. Confirm that
all dirty paths listed above belong to this task. Do not use reset, checkout or
restore.

- [ ] **Step 2: Run focused tests that already encode the accepted review**

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/alpaca/test_request_parsing.py test/alpaca/test_protocol_errors.py test/alpaca/test_exposure_api.py test/alpaca/test_camera_settings.py test/alpaca/test_imagearray_json.py test/alpaca/test_imagebytes.py test/unit/test_camera_coordinator.py test/unit/test_toupcam_acquisition.py -v
```

Expected: all pass after owner integration adjustments.

- [ ] **Step 3: Remove in-process hang containment claims**

`ToupTekCamera` may retain native acquisition behavior for the owner process,
but no comment or test may claim that an in-process lock/thread bounds a hung
SDK call. All hard timeout guarantees belong to `CameraOwnerProxy`.

- [ ] **Step 4: Test cross-process dark/bias and Stop semantics**

Add integration cases proving dark duration zero becomes 12 µs in the owner,
`LastExposureDuration` reports 12 µs, light duration zero is invalid, idle Stop
succeeds and in-progress unsupported Stop returns the ASCOM error.

- [ ] **Step 5: Write Web heater RED tests**

Require:

```python
assert state["heaterSupported"] is True
assert state["heaterLevel"] == 0
assert state["heaterMaxLevel"] == 5
assert state["heaterWritable"] is True
```

Cover off and every valid integer level, bool/float/string/negative/overflow
rejection, unsupported and disconnected state, read failure, write failure,
main-controller-connected operation, exposure read visibility, exposure write
rejection before SDK/IPC, and two concurrent Web writes serialized by the
coordinator.

- [ ] **Step 6: Verify Web heater tests fail**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/web/test_heater_control.py test/alpaca/test_camera_manifest.py test/alpaca/test_common_api.py -v
```

Expected: heater Web tests fail because the state and update fields do not
exist; Alpaca manifest and common action behavior remain green.

- [ ] **Step 7: Implement Web-only heater control**

Add the four state fields and accept a body containing only:

```json
{"heaterLevel": 0}
```

The coordinator validates connection, support, exact integer range and idle
exposure state under its existing synchronization barrier. The Web GUI renders
Off plus `1..heaterMaxLevel`, disables writes during exposure, displays the
last device-confirmed level, and rolls back its selection on error.

- [ ] **Step 8: Prove Alpaca Camera V4 remains unchanged**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/web test/alpaca/test_camera_manifest.py test/alpaca/test_common_api.py -v
```

Expected: all pass; `supportedactions` retains its prior value and no heater
endpoint appears in the Camera V4 manifest.

- [ ] **Step 9: Run the Alpaca manifest and full non-hardware suite**

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/alpaca test/unit test/web test/smoke test/integration -v
```

Expected: all pass with hardware tests gated.

- [ ] **Step 10: Commit the preserved review fixes and Web heater**

Stage only the reviewed product and test files belonging to the accepted
Task 13 feedback:

```powershell
git add touptek4040/alpaca/request.py touptek4040/alpaca/camera_api.py touptek4040/alpaca/app.py touptek4040/camera_coordinator.py touptek4040/toupcam_camera.py touptek4040/web_console.py touptek4040/web test/alpaca test/web test/unit/test_camera_coordinator.py test/unit/test_toupcam_acquisition.py
git commit -m "fix: align owner-backed camera with ASCOM V4"
```

---

### Task 7: Real Process Fault Injection and Leak Audit

**Files:**
- Create: `test/integration/test_owner_hangs.py`
- Create: `test/integration/test_owner_shared_memory.py`
- Create: `test/scripts/run_owner_faults.ps1`
- Generate: `test/results/owner-faults/summary.json`
- Generate: `test/results/owner-faults/summary.md`

**Interfaces:**
- Consumes: complete proxy and fake spawned owner.
- Produces: reproducible hard-timeout and cleanup evidence.

- [ ] **Step 1: Implement a spawned hanging backend fixture**

The fixture blocks indefinitely at exactly one selected operation and records
its PID, generation, request ID and call count in a parent-visible queue.

- [ ] **Step 2: Test every hard boundary**

Parameterize:

```text
startup, connect/Open, get setting, set setting, Trigger,
heater read, heater write, frame publication, Abort, Disconnect/Close
```

For each case assert elapsed time is bounded by configured timeout plus a
small process-termination allowance, exact child PID is gone, the operation
count is one, replacement generation differs and replacement is disconnected.

- [ ] **Step 3: Test shared-memory crash points**

Crash before creation, after creation, after descriptor send and after proxy
copy but before release ACK. Assert the registry and operating system contain
no application-owned live segment after cleanup.

- [ ] **Step 4: Run the fault script**

```powershell
powershell -ExecutionPolicy Bypass -File test/scripts/run_owner_faults.ps1
```

The script writes machine-readable results and exits non-zero for a live child,
orphaned segment, duplicate acquisition call or exceeded bound.

- [ ] **Step 5: Commit fault evidence**

```powershell
git add test/integration/test_owner_hangs.py test/integration/test_owner_shared_memory.py test/scripts/run_owner_faults.ps1 test/results/owner-faults
git commit -m "test: prove owner timeout and leak isolation"
```

---

### Task 8: Staged Real Hardware and Fresh ConformU Closure

**Files:**
- Preserve and modify: `test/hardware/conftest.py`
- Preserve and modify: `test/hardware/test_hardware_matrix.py`
- Modify: `test/hardware/test_hardware_abort.py`
- Modify: `test/hardware/test_hardware_cooling.py`
- Create: `test/hardware/test_owner_recovery.py`
- Preserve and modify: `test/scripts/run_hardware.ps1`
- Preserve and modify: `test/scripts/run_conformu.ps1`
- Preserve and modify: `test/scripts/summarize_conformu.py`
- Preserve: `test/unit/test_conformu_summary.py`
- Generate: `test/results/hardware/*`
- Generate: `test/results/conformu/*`

**Interfaces:**
- Consumes: packaged-equivalent owner/proxy and approved physical camera.
- Produces: final restoration, timeout recovery and conformance evidence.
- Consumes and restores the exact `TOUPCAM_OPTION_HEAT` level captured at
  Connect.

- [ ] **Step 1: Validate the raw pre-open gate without hardware mutation**

Require exactly one nonblank opaque `EnumV2.id`, exact model and display name.
Open only `sn:TP241025143218C92B01151AEC40CB7`, then verify serial/model and
close. Extra, wrong-model and empty-ID fakes must record zero Open calls.

- [ ] **Step 2: Run the first bounded hardware stage**

Run identity, Connect, one RAW12 16 x 16 exposure and Disconnect. Require all
snapshot fields, including heater level and heater maximum, equal and no live
owner process or shared-memory entry.

- [ ] **Step 3: Run the approved matrix**

Exercise RAW8/RAW12, native bins 1-8, ASCOM bin3 1365 fallback, small/odd/right
edge ROI, repeated acquisition, orientation, safe bias-zero clamping, gain,
cooling reads, Abort and reconnect. Inspect TEC target/state, fan and gain
before/after structural transitions. Read `TOUPCAM_OPTION_HEAT_MAX`, change
`TOUPCAM_OPTION_HEAT` to one valid level only in a bounded heater stage, then
restore and re-read the exact original level in `finally`.

- [ ] **Step 4: Inject a real owner termination without retrying exposure**

Start an exposure against a test owner command that deliberately withholds its
response after native ownership is established. Terminate only the owner
process at the configured deadline. Verify the main service remains
responsive, publishes no frame and requires explicit reconnect. Restore and
re-read the camera before continuing.

- [ ] **Step 5: Run fresh ConformU profiles**

`run_conformu.ps1` removes old per-profile artifacts, starts a fresh owner and
server for Protocol, restores and stops them, then repeats for Camera V4. The
validator permits no Protocol issues and only exact current-spec conflicts
whose messages and counts are explicitly allow-listed. Unknown or stale output
fails the script.

- [ ] **Step 6: Run final source verification**

```powershell
powershell -ExecutionPolicy Bypass -File test/scripts/run_all.ps1
powershell -ExecutionPolicy Bypass -File test/scripts/run_owner_faults.ps1
powershell -ExecutionPolicy Bypass -File test/scripts/run_hardware.ps1
powershell -ExecutionPolicy Bypass -File test/scripts/run_conformu.ps1
```

Require exact camera restoration, zero unexplained ConformU findings, no live
owner/ConformU process, no listening test port and no registered shared-memory
leak.

- [ ] **Step 7: Normalize and commit final evidence**

Replace workspace absolute paths with `<repo>`. Keep final logs, machine
results, restoration snapshots and concise root-cause evidence; remove only
Task-created superseded diagnostics.

```powershell
git add test/hardware test/scripts/run_hardware.ps1 test/scripts/run_conformu.ps1 test/scripts/summarize_conformu.py test/unit/test_conformu_summary.py test/results/hardware test/results/conformu
git commit -m "test: validate isolated owner on real camera"
```

---

### Task 9: Owner-Aware Packaging, Chinese Guide and Final Verification

**Files:**
- Create: `ToupTek4040Control.spec`
- Create: `使用说明.txt`
- Modify or create: `test/packaging/test_distribution_assets.py`
- Create: `test/packaging/test_packaged_owner.py`
- Create: `test/packaging/test_packaged_roundtrip.py`
- Create: `test/scripts/build_release.ps1`
- Create: `test/scripts/run_package_smoke.ps1`
- Generate: `test/results/final-verification.md`

**Interfaces:**
- Produces: `dist/ToupTek4040Control/ToupTek4040Alpaca.exe`
- Produces: `dist/ToupTek4040Control/ToupTek4040GUI.exe`
- Produces: distributed `touptek4040.json`

- [ ] **Step 1: Write packaging RED tests**

Require:

```python
required = {
    "ToupTek4040Alpaca.exe",
    "ToupTek4040GUI.exe",
    "toupcam.dll",
    "touptek4040.json",
    "使用说明.txt",
}
assert required <= {path.name for path in dist_dir.iterdir()}
assert "touptek4040.ini" not in {path.name for path in dist_dir.iterdir()}
```

Add a frozen-owner test that starts the packaged simulator without an owner,
then starts a fake owner mode and proves Windows spawn and shared memory work
from the distribution.

- [ ] **Step 2: Verify packaging RED**

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/packaging -v
```

Expected: failure because the owner-aware distribution does not exist.

- [ ] **Step 3: Build two executable entry points**

The PyInstaller spec includes:

```python
datas = [
    ("touptek4040/web", "touptek4040/web"),
    ("touptek4040.json", "."),
    ("使用说明.txt", "."),
]
binaries = [
    ("toupcamsdk.20250623/win/x64/toupcam.dll", "."),
]
```

Include the owner entry module, SDK Python wrapper and required
`multiprocessing` hidden imports. Do not bundle the x86 DLL.

- [ ] **Step 4: Write the Chinese guide**

Document JSON configuration fields and defaults, driver installation,
firewall/discovery, N.I.N.A., trusted-LAN security, real/simulator selection,
RAW modes, binning and software fallback, cooling, FITS, logs, diagnostics,
owner timeout behavior, explicit reconnect after failure, Web-only anti-dew
heater levels, exposure-time write lockout, disconnect restoration and
troubleshooting.

- [ ] **Step 5: Run packaged simulator and real-camera smoke**

The script verifies management, discovery, Connect, one exposure, ImageBytes,
JSON, PNG, FITS, Abort, Disconnect, owner process cleanup and JSON loading.
It also verifies the packaged Web heater UI and one restored real-camera heater
round trip. Real failure must never select simulation.

- [ ] **Step 6: Run complete final verification**

```powershell
powershell -ExecutionPolicy Bypass -File test/scripts/build_release.ps1
powershell -ExecutionPolicy Bypass -File test/scripts/run_package_smoke.ps1
powershell -ExecutionPolicy Bypass -File test/scripts/run_all.ps1
powershell -ExecutionPolicy Bypass -File test/scripts/run_owner_faults.ps1
powershell -ExecutionPolicy Bypass -File test/scripts/run_hardware.ps1
powershell -ExecutionPolicy Bypass -File test/scripts/run_conformu.ps1
```

Write `test/results/final-verification.md` with exact commands, counts, camera
identity, 4096 x 4096 transfer size, shared-memory peak, timeout evidence,
restoration, ConformU results, executable paths and remaining manual N.I.N.A.
operator checks.

- [ ] **Step 7: Commit the release**

```powershell
git add ToupTek4040Control.spec 使用说明.txt touptek4040.json test/packaging test/scripts/build_release.ps1 test/scripts/run_package_smoke.ps1 test/results/final-verification.md
git commit -m "build: package isolated ToupTek Alpaca release"
```

---

## Final Acceptance Checklist

- [ ] The worktree is clean and contains no unexpected tracked or untracked Task-created files.
- [ ] JSON is the distributed configuration and legacy INI loads only when explicitly selected.
- [ ] Simulator mode starts no owner process.
- [ ] Real mode loads the SDK only inside the owner process.
- [ ] Hung Open, Trigger and Close tests terminate only the exact owner within finite bounds.
- [ ] No timed-out exposure is retried or published.
- [ ] Replacement owner generation requires explicit Connect.
- [ ] Shared-memory registry and operating-system segments are clean after success, crash and timeout.
- [ ] Camera V4 manifest, simulator and Web/FITS/ImageBytes/JSON round trips pass.
- [ ] Real RAW8/RAW12, ROI, bins 1-8, bin3 1365 fallback, Abort, reconnect, cooling and orientation pass.
- [ ] Hardware settings match the connection-time snapshot after every final stage.
- [ ] Fresh ConformU Protocol and Camera V4 have zero unexplained findings.
- [ ] Packaged simulator and real-camera smoke pass with Windows spawn.
- [ ] The Chinese guide and `touptek4040.json` match delivered filenames, defaults and behavior.
- [ ] Web heater control uses only owner IPC, exposes the device-reported
  integer range, rejects writes during exposure and restores the
  connection-time level on normal Disconnect.
- [ ] Alpaca Camera V4 routes, `SupportedActions` and `Action` contain no
  heater extension.
