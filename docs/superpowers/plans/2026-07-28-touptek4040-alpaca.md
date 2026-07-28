# ToupTek 4040 Alpaca Camera V4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Windows 64-bit ToupTek `sMAX16AM-U3-CL-GPS` control package that implements the complete ASCOM ICameraV4 Alpaca interface and reproduces the approved COSMOS66 Web, FITS, logging, simulator, diagnostic, and packaging scope.

**Architecture:** Derive the Alpaca transport and responder behavior from the official Platform 7 AlpycaDevice templates, while keeping all hardware access behind a focused `CameraContract`. A single `CameraCoordinator` owns the camera handle and state machine; Alpaca, Web, GUI, ImageBytes, FITS, and preview consume that coordinator without opening additional SDK handles.

**Tech Stack:** 64-bit Python 3.13, `toupcam.py` and `toupcam.dll` from `toupcamsdk.20250623`, NumPy, Falcon, Waitress, native HTML/CSS/JavaScript, pytest, official `alpyca` client, ConformU, PyInstaller.

## Global Constraints

- Implement ASCOM Alpaca Device API v1 and return `InterfaceVersion = 4`.
- Treat `G:\ToupTek_4040\toupcamsdk.20250623\doc\en.html` as authoritative for SDK behavior.
- Report `CameraXSize = CameraYSize = 4096`, `PixelSizeX = PixelSizeY = 9.0`.
- Default to RAW 12-bit and expose exactly `["RAW 12-bit", "RAW 8-bit"]`.
- Support symmetric saturating-add binning from 1×1 through 8×8; `CanAsymmetricBin = False`.
- Use software-triggered single-frame acquisition for formal exposures and preview.
- Set `CanAbortExposure = True` and `CanStopExposure = False`.
- Never silently fall back from real hardware to the simulator.
- Put every test source, fixture, helper, test configuration, script, and result under root `test/`.
- Use a new 64-bit environment; never use the existing 32-bit `.venv`.
- Preserve unrelated SDK, driver, IDE, and sample files already present in the workspace.

---

## File Map

| Path | Responsibility |
|---|---|
| `pyproject.toml` | Package metadata and runtime dependency declaration |
| `.gitignore` | Ignore environments, caches, builds, runtime logs, and generated test results |
| `touptek4040.ini` | Production defaults |
| `touptek4040/__init__.py` | Version and package identity |
| `touptek4040/config.py` | INI, CLI overrides, paths, and logging configuration |
| `touptek4040/errors.py` | Internal typed errors and SDK HRESULT translation |
| `touptek4040/camera_contract.py` | Camera protocol, immutable capabilities, frame metadata |
| `touptek4040/sim_camera.py` | Explicitly selected bounded-memory simulator |
| `touptek4040/toupcam_loader.py` | 64-bit validation and deterministic SDK/DLL loading |
| `touptek4040/toupcam_camera.py` | ToupTek hardware adapter and acquisition primitives |
| `touptek4040/camera_coordinator.py` | Connection, exposure, preview, session, and settings state machine |
| `touptek4040/alpaca/responses.py` | Standard response envelopes and error codes |
| `touptek4040/alpaca/request.py` | Case-insensitive parameter parsing and request validation |
| `touptek4040/alpaca/common_api.py` | Common Platform 7 device members |
| `touptek4040/alpaca/camera_api.py` | ICameraV4 capability, setting, cooling, and exposure responders |
| `touptek4040/alpaca/image_api.py` | JSON ImageArray and ImageBytes streaming |
| `touptek4040/alpaca/management.py` | Management and setup endpoints |
| `touptek4040/alpaca/discovery.py` | Shared-port IPv4/IPv6 discovery |
| `touptek4040/alpaca/app.py` | Falcon route assembly and server lifecycle |
| `touptek4040/event_broker.py` | Bounded status/log event fan-out |
| `touptek4040/image_workspace.py` | Immutable image generations, statistics, and viewport access |
| `touptek4040/png_stream.py` | Dependency-free grayscale PNG output |
| `touptek4040/fits_io.py` | Streaming unsigned FITS output |
| `touptek4040/web_console.py` | Same-origin Web API and SSE |
| `touptek4040/web/*` | Adapted COSMOS66 native Web interface |
| `touptek4040/gui.py` | Local hardware diagnostic interface |
| `run_alpaca.py` / `run_gui.py` | Executable entry points |
| `ToupTek4040Control.spec` | Two-executable PyInstaller package |
| `使用说明.txt` | Chinese installation and operating guide |
| `test/*` | All automated, hardware, conformance, packaging tests and outputs |

---

### Task 1: 64-bit Project Foundation and Configuration

**Files:**
- Create: `.gitignore`
- Create: `pyproject.toml`
- Create: `touptek4040.ini`
- Create: `touptek4040/__init__.py`
- Create: `touptek4040/config.py`
- Create: `test/pytest.ini`
- Create: `test/requirements.txt`
- Create: `test/unit/test_config.py`
- Create: `test/unit/test_environment.py`

**Interfaces:**
- Produces: `AppConfig`, `ServerConfig`, `CameraConfig`, `WebConfig`, `LoggingConfig`
- Produces: `load_config(path: Path | None, overrides: Mapping[str, object]) -> AppConfig`
- Produces: `runtime_base_dir() -> Path`

- [ ] **Step 1: Create the 64-bit environment and write failing configuration tests**

Run:

```powershell
py -3.13 -m venv .venv64
.\.venv64\Scripts\python.exe -m pip install --upgrade pip
```

Write tests that assert production defaults: host `0.0.0.0`, HTTP port `11111`, discovery port `32227`, source `toupcam`, RAW 12-bit, Web enabled, and explicit simulator selection.

```python
def test_production_defaults_use_real_toupcam():
    cfg = load_config(None, {})
    assert cfg.camera.source == "toupcam"
    assert cfg.camera.readout_mode == "RAW 12-bit"
    assert cfg.server.port == 11111
    assert cfg.server.discovery_port == 32227
```

- [ ] **Step 2: Run the tests and confirm the package is absent**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_config.py test/unit/test_environment.py -v
```

Expected: collection fails because `touptek4040.config` does not exist.

- [ ] **Step 3: Implement package metadata, configuration dataclasses, INI precedence, and logging paths**

Use frozen dataclasses and make explicit CLI values override INI values while absent CLI options preserve INI values.

```python
@dataclass(frozen=True)
class CameraConfig:
    source: str = "toupcam"
    serial: str | None = None
    readout_mode: str = "RAW 12-bit"
    gain: int | None = None

@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig
    camera: CameraConfig
    web: WebConfig
    logging: LoggingConfig
    config_path: Path | None
```

Add runtime dependencies `numpy>=2.0,<3`, `falcon>=4,<5`, and `waitress>=3,<4`. Put pytest, alpyca, requests, psutil, and PyInstaller in `test/requirements.txt`.

- [ ] **Step 4: Install dependencies and pass foundation tests**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pip install -e .
.\.venv64\Scripts\python.exe -m pip install -r test/requirements.txt
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_config.py test/unit/test_environment.py -v
```

Expected: all tests pass and `platform.architecture()[0] == "64bit"`.

- [ ] **Step 5: Commit the foundation**

```powershell
git add .gitignore pyproject.toml touptek4040.ini touptek4040 test/pytest.ini test/requirements.txt test/unit/test_config.py test/unit/test_environment.py
git commit -m "build: establish 64-bit ToupTek project"
```

---

### Task 2: Camera Contract, Capabilities, Errors, and Simulator

**Files:**
- Create: `touptek4040/errors.py`
- Create: `touptek4040/camera_contract.py`
- Create: `touptek4040/sim_camera.py`
- Create: `test/fixtures/fake_toupcam.py`
- Create: `test/unit/test_camera_contract.py`
- Create: `test/unit/test_sim_camera.py`

**Interfaces:**
- Produces: `CameraCapabilities`, `Frame`, `ReadoutMode`, `CameraContract`
- Produces: `SimCamera(CameraContract)`
- Produces: `CameraError`, `CameraBusyError`, `CameraNotConnectedError`, `CameraInvalidValueError`

- [ ] **Step 1: Write failing contract and bounded-memory simulator tests**

```python
def test_simulator_reports_approved_capabilities():
    cam = SimCamera(serial="SIM-4040")
    caps = cam.capabilities
    assert (caps.width, caps.height) == (4096, 4096)
    assert caps.readout_modes == ("RAW 12-bit", "RAW 8-bit")
    assert caps.max_bin == 8
    assert caps.can_abort is True
    assert caps.can_stop is False

def test_simulator_raw12_roi_is_uint16():
    cam = SimCamera(serial="SIM-4040")
    frame = cam.acquire(0.001, Roi(0, 0, 64, 32), 1, ReadoutMode.RAW12)
    assert frame.pixels.shape == (32, 64)
    assert frame.pixels.dtype == np.uint16
    assert int(frame.pixels.max()) <= 4095
```

- [ ] **Step 2: Verify tests fail**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_camera_contract.py test/unit/test_sim_camera.py -v
```

Expected: imports fail.

- [ ] **Step 3: Implement focused immutable types and the simulator**

```python
class ReadoutMode(IntEnum):
    RAW12 = 0
    RAW8 = 1

@dataclass(frozen=True)
class Frame:
    pixels: np.ndarray
    started_utc: datetime
    duration_s: float
    binning: int
    roi: Roi
    readout_mode: ReadoutMode

class CameraContract(Protocol):
    @property
    def capabilities(self) -> CameraCapabilities: ...
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def acquire(self, duration_s: float, roi: Roi, binning: int,
                readout_mode: ReadoutMode) -> Frame: ...
    def abort(self) -> None: ...
```

Generate simulator rows lazily or by bounded tiles so a full 4096 × 4096 frame is the only large allocation.

- [ ] **Step 4: Pass simulator tests**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_camera_contract.py test/unit/test_sim_camera.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit contract and simulator**

```powershell
git add touptek4040/errors.py touptek4040/camera_contract.py touptek4040/sim_camera.py test/fixtures test/unit/test_camera_contract.py test/unit/test_sim_camera.py
git commit -m "feat: define camera contract and simulator"
```

---

### Task 3: Deterministic ToupTek SDK Loading and Device Identity

**Files:**
- Create: `touptek4040/toupcam_loader.py`
- Create: `touptek4040/toupcam_camera.py`
- Create: `test/unit/test_toupcam_loader.py`
- Create: `test/unit/test_toupcam_identity.py`
- Create: `test/hardware/test_hardware_identity.py`

**Interfaces:**
- Consumes: `CameraCapabilities`, `CameraContract`, internal errors
- Produces: `load_toupcam_module(base_dir: Path) -> ModuleType`
- Produces: `enumerate_cameras() -> list[CameraIdentity]`
- Produces: `ToupTekCamera(serial: str | None)`

- [ ] **Step 1: Write failing loader and fake-SDK identity tests**

```python
def test_loader_rejects_32_bit_python(monkeypatch):
    monkeypatch.setattr(platform, "architecture", lambda: ("32bit", "WindowsPE"))
    with pytest.raises(RuntimeError, match="64-bit"):
        validate_runtime_architecture()

def test_identity_uses_serial_not_usb_path(fake_sdk):
    identities = enumerate_cameras(fake_sdk)
    assert identities[0].model == "sMAX16AM-U3-CL-GPS"
    assert identities[0].serial == "TP241025143218C92B01151AEC40CB7"
```

- [ ] **Step 2: Verify failure**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_toupcam_loader.py test/unit/test_toupcam_identity.py -v
```

Expected: loader and adapter imports fail.

- [ ] **Step 3: Implement deterministic loading and read-only capability discovery**

Resolve `toupcam.py` and the x64 DLL from the executable directory first, then the project SDK path. On Windows, add the DLL directory with `os.add_dll_directory()` before importing the wrapper.

```python
def validate_runtime_architecture() -> None:
    if struct.calcsize("P") != 8:
        raise RuntimeError("ToupTek4040 requires 64-bit Python and 64-bit toupcam.dll")

@dataclass(frozen=True)
class CameraIdentity:
    device_id: str
    display_name: str
    model: str
    serial: str
```

Read serial, firmware, hardware, production date, FPGA, model flags, resolution, pixel size, exposure range, gain range, and TEC target range after opening, then close on every exit path.

- [ ] **Step 4: Run unit tests and opt-in hardware identity test**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_toupcam_loader.py test/unit/test_toupcam_identity.py -v
$env:TOUPTEK_HARDWARE_TEST='1'
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/hardware/test_hardware_identity.py -v
Remove-Item Env:TOUPTEK_HARDWARE_TEST
```

Expected: identity equals the approved model, serial, 4096 dimensions, and 9.0 µm pixels.

- [ ] **Step 5: Commit SDK loading and identity**

```powershell
git add touptek4040/toupcam_loader.py touptek4040/toupcam_camera.py test/unit/test_toupcam_loader.py test/unit/test_toupcam_identity.py test/hardware/test_hardware_identity.py
git commit -m "feat: load ToupTek SDK and identify camera"
```

---

### Task 4: ToupTek Settings, Triggered Acquisition, and Abort

**Files:**
- Modify: `touptek4040/toupcam_camera.py`
- Expand: `test/fixtures/fake_toupcam.py`
- Create: `test/unit/test_toupcam_settings.py`
- Create: `test/unit/test_toupcam_acquisition.py`
- Create: `test/hardware/test_hardware_acquisition.py`

**Interfaces:**
- Produces: `apply_settings(settings: CaptureSettings) -> AppliedSettings`
- Produces: `acquire(...) -> Frame`
- Produces: `abort() -> None`
- Produces: temperature, TEC, and cooler-power property methods required by `CameraContract`

- [ ] **Step 1: Write failing SDK call-order, conversion, and abort tests**

```python
def test_settings_are_applied_only_before_trigger(fake_camera):
    fake_camera.acquire(0.25, Roi(10, 12, 100, 80), 2, ReadoutMode.RAW12)
    assert fake_camera.sdk.calls[:6] == [
        ("put_Option", OPTION_RAW, 1),
        ("put_Option", OPTION_BITDEPTH, 1),
        ("put_Option", OPTION_BINNING, 2),
        ("put_Roi", 20, 24, 200, 160),
        ("put_ExpoTime", 250000),
        ("put_Option", OPTION_TRIGGER, 1),
    ]

def test_abort_cancels_trigger_and_discards_frame(fake_camera):
    worker = start_acquire(fake_camera, 30.0)
    fake_camera.abort()
    worker.join(1)
    assert ("Trigger", 0xFFFF) in fake_camera.sdk.calls
    assert fake_camera.last_frame is None
```

- [ ] **Step 2: Verify tests fail**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_toupcam_settings.py test/unit/test_toupcam_acquisition.py -v
```

- [ ] **Step 3: Implement configuration, `TriggerSyncV4`, frame validation, and cancellation**

Preallocate one `uint16` or `uint8` buffer using final SDK dimensions. Never call ROI, RAW, bit-depth, binning, or trigger-mode configuration from an SDK callback.

```python
def acquire(self, duration_s, roi, binning, readout_mode):
    applied = self.apply_settings(CaptureSettings(duration_s, roi, binning, readout_mode))
    timeout_ms = math.ceil(duration_s * 1000 * 1.02 + 4000)
    self._handle.TriggerSyncV4(timeout_ms, buffer, 0, -1, frame_info)
    if self._abort_generation != generation:
        raise CameraOperationCancelledError("Exposure was aborted; frame discarded")
    return Frame(pixels=array.copy(), started_utc=started, duration_s=applied.duration_s,
                 binning=binning, roi=roi, readout_mode=readout_mode)
```

Map SDK `E_INVALIDARG`, `E_NOTIMPL`, busy/state errors, timeout, disconnect, and generic HRESULTs into internal typed errors.

- [ ] **Step 4: Pass unit tests, then run the short hardware matrix**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_toupcam_settings.py test/unit/test_toupcam_acquisition.py -v
$env:TOUPTEK_HARDWARE_TEST='1'
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/hardware/test_hardware_acquisition.py -v
Remove-Item Env:TOUPTEK_HARDWARE_TEST
```

The hardware test must restore RAW mode, gain, ROI, binning, TEC target, TEC state, and fan setting in `finally`.

- [ ] **Step 5: Commit acquisition support**

```powershell
git add touptek4040/toupcam_camera.py test/fixtures/fake_toupcam.py test/unit/test_toupcam_settings.py test/unit/test_toupcam_acquisition.py test/hardware/test_hardware_acquisition.py
git commit -m "feat: acquire and abort ToupTek exposures"
```

---

### Task 5: Camera Coordinator and Formal State Machine

**Files:**
- Create: `touptek4040/camera_coordinator.py`
- Create: `touptek4040/event_broker.py`
- Create: `test/unit/test_camera_coordinator.py`
- Create: `test/unit/test_event_broker.py`
- Create: `test/unit/test_camera_concurrency.py`

**Interfaces:**
- Consumes: `CameraContract`
- Produces: `CameraCoordinator(factory, source)`
- Produces: `connect_async()`, `disconnect_async()`, `set_connected_sync(bool)`
- Produces: `start_exposure(duration_s, light)`, `abort_exposure()`
- Produces: `start_preview(callback)`, `stop_preview()`, `snapshot()`

- [ ] **Step 1: Write failing connection, state, stale-frame, and preview tests**

```python
def test_start_exposure_is_non_blocking(coordinator):
    started = time.monotonic()
    coordinator.start_exposure(10.0, True)
    assert time.monotonic() - started < 0.1
    assert coordinator.snapshot().camera_state == CameraState.EXPOSING

def test_late_frame_after_abort_is_never_published(coordinator, controlled_camera):
    coordinator.start_exposure(10.0, True)
    coordinator.abort_exposure()
    controlled_camera.release_late_frame()
    assert coordinator.snapshot().image_ready is False
```

- [ ] **Step 2: Verify tests fail**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_camera_coordinator.py test/unit/test_event_broker.py test/unit/test_camera_concurrency.py -v
```

- [ ] **Step 3: Implement the lock-protected state machine and bounded events**

```python
@dataclass(frozen=True)
class CameraSnapshot:
    connected: bool
    connecting: bool
    camera_state: CameraState
    image_ready: bool
    percent_completed: int | None
    error: CameraError | None

def start_exposure(self, duration_s: float, light: bool) -> None:
    with self._lock:
        self._require_idle_locked()
        self._validate_capture_locked(duration_s)
        self._session_id += 1
        session = self._session_id
        self._image_ready = False
        self._state = CameraState.EXPOSING
    self._start_worker(session, duration_s)
```

Preview loops through repeated single-frame acquisitions, writes only to the preview slot, and is stopped before formal exposure. Worker completion publishes only if its session ID is still current.

- [ ] **Step 4: Pass coordinator tests**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_camera_coordinator.py test/unit/test_event_broker.py test/unit/test_camera_concurrency.py -v
```

- [ ] **Step 5: Commit the coordinator**

```powershell
git add touptek4040/camera_coordinator.py touptek4040/event_broker.py test/unit/test_camera_coordinator.py test/unit/test_event_broker.py test/unit/test_camera_concurrency.py
git commit -m "feat: coordinate camera lifecycle and exposure state"
```

---

### Task 6: Alpaca Protocol Primitives and Complete API Manifest

**Files:**
- Create: `touptek4040/alpaca/__init__.py`
- Create: `touptek4040/alpaca/responses.py`
- Create: `touptek4040/alpaca/request.py`
- Create: `touptek4040/alpaca/manifest.py`
- Create: `test/alpaca/test_responses.py`
- Create: `test/alpaca/test_request_parsing.py`
- Create: `test/alpaca/test_camera_manifest.py`

**Interfaces:**
- Produces: `AlpacaError(number, message)`
- Produces: `property_response(value, request, transaction_id)`
- Produces: `method_response(request, transaction_id)`
- Produces: `CAMERA_V4_ENDPOINTS: tuple[EndpointSpec, ...]`

- [ ] **Step 1: Write failing protocol and manifest tests**

```python
def test_success_response_has_all_required_fields():
    body = property_response(4, FakeRequest(client_transaction_id=17), 9)
    assert body == {
        "Value": 4,
        "ClientTransactionID": 17,
        "ServerTransactionID": 9,
        "ErrorNumber": 0,
        "ErrorMessage": "",
    }

def test_manifest_contains_every_official_camera_v4_route():
    assert ("GET", "imagearrayvariant") in manifest_pairs()
    assert ("PUT", "subexposureduration") in manifest_pairs()
    assert len(CAMERA_V4_ENDPOINTS) == 88
```

- [ ] **Step 2: Verify tests fail**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/alpaca/test_responses.py test/alpaca/test_request_parsing.py test/alpaca/test_camera_manifest.py -v
```

- [ ] **Step 3: Implement official error numbers, transaction allocation, and case-insensitive parameters**

Use `0x400`, `0x401`, `0x402`, `0x407`, `0x40B`, `0x40C`, and `0x40E` exactly. Include every common and Camera-specific GET/PUT endpoint listed in the approved design.

```python
@dataclass(frozen=True)
class EndpointSpec:
    http_method: Literal["GET", "PUT"]
    command: str
    category: Literal["common", "camera", "image"]

def get_parameter(req: falcon.Request, name: str, required: bool = True) -> str | None:
    fields = {key.casefold(): value for key, value in req.params.items()}
    if req.method == "PUT":
        fields.update({key.casefold(): value for key, value in req.media.items()})
    return fields.get(name.casefold())
```

- [ ] **Step 4: Pass protocol tests**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/alpaca/test_responses.py test/alpaca/test_request_parsing.py test/alpaca/test_camera_manifest.py -v
```

- [ ] **Step 5: Commit protocol primitives**

```powershell
git add touptek4040/alpaca test/alpaca/test_responses.py test/alpaca/test_request_parsing.py test/alpaca/test_camera_manifest.py
git commit -m "feat: define Alpaca protocol and Camera V4 manifest"
```

---

### Task 7: Common Members and Camera V4 Capabilities/Settings API

**Files:**
- Create: `touptek4040/alpaca/common_api.py`
- Create: `touptek4040/alpaca/camera_api.py`
- Create: `touptek4040/alpaca/app.py`
- Create: `test/alpaca/conftest.py`
- Create: `test/alpaca/test_common_api.py`
- Create: `test/alpaca/test_camera_capabilities.py`
- Create: `test/alpaca/test_camera_settings.py`

**Interfaces:**
- Consumes: `CameraCoordinator`, response/request primitives
- Produces: `create_app(coordinator, config) -> falcon.App`
- Produces: all non-image Camera V4 routes

- [ ] **Step 1: Write parameterized failing endpoint tests**

```python
@pytest.mark.parametrize(("command", "value"), [
    ("interfaceversion", 4),
    ("cameraxsize", 4096),
    ("cameraysize", 4096),
    ("pixelsizex", 9.0),
    ("pixelsizey", 9.0),
    ("canabortexposure", True),
    ("canstopexposure", False),
    ("canasymmetricbin", False),
    ("maxbinx", 8),
    ("maxbiny", 8),
    ("sensorname", "sMAX16AM-U3-CL-GPS"),
])
def test_capability(command, value, client):
    response = client.simulate_get(f"/api/v1/camera/0/{command}")
    assert response.json["Value"] == value
```

Add explicit NotImplemented tests for Bayer offsets, FastReadout, offset family, Gains, ElectronsPerADU, FullWellCapacity, HeatSinkTemperature, SubExposureDuration, ImageArrayVariant, commands, and PulseGuide.

- [ ] **Step 2: Verify tests fail**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/alpaca/test_common_api.py test/alpaca/test_camera_capabilities.py test/alpaca/test_camera_settings.py -v
```

- [ ] **Step 3: Implement routes from the manifest with explicit member behavior**

```python
CAPABILITY_GETTERS = {
    "interfaceversion": lambda c: 4,
    "cameraxsize": lambda c: c.capabilities.width,
    "cameraysize": lambda c: c.capabilities.height,
    "canabortexposure": lambda c: True,
    "canstopexposure": lambda c: False,
    "readoutmodes": lambda c: ["RAW 12-bit", "RAW 8-bit"],
}
```

Ensure `BinX` and `BinY` writes synchronize, Gain uses numeric mode, `ReadoutMode` updates MaxADU immediately, and all hardware-dependent properties return NotConnected while disconnected.

- [ ] **Step 4: Pass common, capability, and setting tests**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/alpaca/test_common_api.py test/alpaca/test_camera_capabilities.py test/alpaca/test_camera_settings.py -v
```

- [ ] **Step 5: Commit Camera V4 property API**

```powershell
git add touptek4040/alpaca/common_api.py touptek4040/alpaca/camera_api.py touptek4040/alpaca/app.py test/alpaca
git commit -m "feat: expose Camera V4 properties and settings"
```

---

### Task 8: Exposure, Cooling, State, and Failure API

**Files:**
- Modify: `touptek4040/alpaca/camera_api.py`
- Create: `test/alpaca/test_exposure_api.py`
- Create: `test/alpaca/test_cooling_api.py`
- Create: `test/alpaca/test_async_failures.py`

**Interfaces:**
- Consumes: coordinator exposure and cooling methods
- Produces: StartExposure, AbortExposure, StopExposure, CameraState, PercentCompleted, ImageReady, DeviceState, cooling endpoints

- [ ] **Step 1: Write failing end-to-end API state tests**

```python
def test_exposure_state_sequence(client, controlled_camera):
    client.simulate_put("/api/v1/camera/0/startexposure",
                        params={"Duration": "1.0", "Light": "true"})
    assert get_value(client, "imageready") is False
    assert get_value(client, "camerastate") == 2
    controlled_camera.finish()
    assert get_value(client, "imageready") is True
    assert get_value(client, "camerastate") == 0

def test_stop_exposure_is_idle_noop_but_not_implemented_while_exposing(client):
    assert put_error(client, "stopexposure") == 0
    start_long_exposure(client)
    assert put_error(client, "stopexposure") == 0x400
```

- [ ] **Step 2: Verify tests fail**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/alpaca/test_exposure_api.py test/alpaca/test_cooling_api.py test/alpaca/test_async_failures.py -v
```

- [ ] **Step 3: Implement exact ASCOM behavior and async error propagation**

Validate exposure duration, binned ROI combinations, state, and connection before starting. Ignore `Light` because `HasShutter=False`. Calculate cooler power from current and maximum TEC voltage.

```python
def cooler_power(coordinator: CameraCoordinator) -> float:
    current, maximum = coordinator.tec_voltage()
    if maximum <= 0:
        raise AlpacaError(0x40B, "TEC maximum voltage is unavailable")
    return min(100.0, max(0.0, current * 100.0 / maximum))
```

Return `InvalidOperation` for PercentCompleted outside applicable states and expose the stored async failure through state/image queries with diagnostic text.

- [ ] **Step 4: Pass state, cooling, and failure tests**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/alpaca/test_exposure_api.py test/alpaca/test_cooling_api.py test/alpaca/test_async_failures.py -v
```

- [ ] **Step 5: Commit operational Camera API**

```powershell
git add touptek4040/alpaca/camera_api.py test/alpaca/test_exposure_api.py test/alpaca/test_cooling_api.py test/alpaca/test_async_failures.py
git commit -m "feat: implement Camera V4 exposure and cooling API"
```

---

### Task 9: Image Workspace, ImageBytes, Streaming JSON, and FITS

**Files:**
- Create: `touptek4040/image_workspace.py`
- Create: `touptek4040/alpaca/image_api.py`
- Create: `touptek4040/fits_io.py`
- Create: `test/unit/test_image_workspace.py`
- Create: `test/alpaca/test_imagebytes.py`
- Create: `test/alpaca/test_imagearray_json.py`
- Create: `test/unit/test_fits_io.py`
- Create: `test/fixtures/reference_pixels.py`

**Interfaces:**
- Produces: `ImageWorkspace.publish(frame) -> ImageGeneration`
- Produces: `stream_imagebytes(generation, transactions) -> Iterable[bytes]`
- Produces: `stream_image_json(generation, transactions) -> Iterable[bytes]`
- Produces: `stream_fits(generation) -> Iterable[bytes]`

- [ ] **Step 1: Write failing pixel-orientation and binary-header tests**

```python
def test_json_is_x_first(reference_generation):
    value = json.loads(b"".join(stream_image_json(reference_generation, txns())))["Value"]
    assert value[2][1] == reference_generation.pixels[1, 2]

def test_raw12_imagebytes_uses_uint16(reference_generation):
    payload = b"".join(stream_imagebytes(reference_generation, txns()))
    metadata = decode_imagebytes_header(payload)
    assert metadata.image_element_type == 8
    assert metadata.transmission_element_type == 8
    assert metadata.dimension1 == reference_generation.width
```

- [ ] **Step 2: Verify tests fail**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_image_workspace.py test/alpaca/test_imagebytes.py test/alpaca/test_imagearray_json.py test/unit/test_fits_io.py -v
```

- [ ] **Step 3: Implement immutable generations and bounded streaming**

Use column chunks for JSON and ImageBytes; never call `.T.copy()` on a full frame. FITS writes unsigned 16-bit data using `BZERO=32768`, `BSCALE=1`, UTC `DATE-OBS`, exposure, binning, ROI, gain, sensor, and readout metadata.

```python
@dataclass(frozen=True)
class ImageGeneration:
    generation_id: int
    pixels: np.ndarray
    frame: Frame

def iter_x_first_columns(pixels: np.ndarray, columns_per_chunk: int = 16):
    for x0 in range(0, pixels.shape[1], columns_per_chunk):
        yield pixels[:, x0:x0 + columns_per_chunk]
```

Register `imagearray` with content negotiation and make `imagearrayvariant` always return `0x400`.

- [ ] **Step 4: Pass image and FITS tests, including memory assertions**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_image_workspace.py test/alpaca/test_imagebytes.py test/alpaca/test_imagearray_json.py test/unit/test_fits_io.py -v
```

- [ ] **Step 5: Commit scientific image transport**

```powershell
git add touptek4040/image_workspace.py touptek4040/alpaca/image_api.py touptek4040/fits_io.py test/unit/test_image_workspace.py test/alpaca/test_imagebytes.py test/alpaca/test_imagearray_json.py test/unit/test_fits_io.py test/fixtures/reference_pixels.py
git commit -m "feat: stream Alpaca images and FITS"
```

---

### Task 10: Management, Setup, Discovery, and Protocol Conformance

**Files:**
- Create: `touptek4040/alpaca/management.py`
- Create: `touptek4040/alpaca/discovery.py`
- Modify: `touptek4040/alpaca/app.py`
- Create: `test/alpaca/test_management.py`
- Create: `test/alpaca/test_setup_pages.py`
- Create: `test/alpaca/test_discovery.py`
- Create: `test/alpaca/test_protocol_errors.py`

**Interfaces:**
- Produces: management/setup routes
- Produces: `DiscoveryResponder(alpaca_port, discovery_port=32227)`

- [ ] **Step 1: Write failing management, discovery, and malformed-request tests**

```python
def test_configured_devices_contains_stable_uid(client):
    value = client.simulate_get("/management/v1/configureddevices").json["Value"]
    assert value == [{
        "DeviceName": "ToupTek 4040 Alpaca Camera",
        "DeviceType": "Camera",
        "DeviceNumber": 0,
        "UniqueID": expected_uid(),
    }]

def test_discovery_response_uses_exact_key():
    assert decode_discovery_response(valid_request()) == {"AlpacaPort": 11111}
```

- [ ] **Step 2: Verify tests fail**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/alpaca/test_management.py test/alpaca/test_setup_pages.py test/alpaca/test_discovery.py test/alpaca/test_protocol_errors.py -v
```

- [ ] **Step 3: Implement official management contract and shared-port discovery**

Bind IPv4 UDP with address reuse/shared options before bind. Join IPv6 `ff12::a1:9aca` when available and log one actionable warning when IPv6 setup is unavailable.

```python
DISCOVERY_QUERY = b"alpacadiscovery1"

def discovery_payload(alpaca_port: int) -> bytes:
    return json.dumps({"AlpacaPort": alpaca_port},
                      separators=(",", ":")).encode("ascii")
```

Reject incorrect device types, device numbers, paths, and required parameters with HTTP 400; return device-level ASCOM errors with HTTP 200.

- [ ] **Step 4: Pass management and protocol tests**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/alpaca/test_management.py test/alpaca/test_setup_pages.py test/alpaca/test_discovery.py test/alpaca/test_protocol_errors.py -v
```

- [ ] **Step 5: Commit management and discovery**

```powershell
git add touptek4040/alpaca/management.py touptek4040/alpaca/discovery.py touptek4040/alpaca/app.py test/alpaca/test_management.py test/alpaca/test_setup_pages.py test/alpaca/test_discovery.py test/alpaca/test_protocol_errors.py
git commit -m "feat: add Alpaca management and discovery"
```

---

### Task 11: Web Image Services and COSMOS66 Console Adaptation

**Files:**
- Create: `touptek4040/png_stream.py`
- Create: `touptek4040/web_console.py`
- Create: `touptek4040/web/__init__.py`
- Create: `touptek4040/web/index.html`
- Create: `touptek4040/web/tokens.css`
- Create: `touptek4040/web/app.css`
- Create: `touptek4040/web/icons.svg`
- Create: `touptek4040/web/api.js`
- Create: `touptek4040/web/app.js`
- Create: `touptek4040/web/image-viewer.js`
- Create: `touptek4040/web/settings-sync.js`
- Create: `touptek4040/web/log-view.js`
- Create: `test/web/test_web_assets.py`
- Create: `test/web/test_web_api.py`
- Create: `test/web/test_web_images.py`
- Create: `test/web/test_web_security.py`

**Interfaces:**
- Consumes: coordinator, image workspace, event broker
- Produces: `/ui/`, same-origin `/web/v1/*`, SSE, viewport PNG, statistics, FITS download

- [ ] **Step 1: Write failing static, state, image, and security tests**

```python
def test_ui_uses_touptek_capabilities(client):
    html = client.simulate_get("/ui/").text
    assert "ToupTek 4040" in html
    assert "RAW 12-bit" in html
    assert "COSMOS-66" not in html

def test_web_mutations_require_same_origin_json_header(client):
    response = client.simulate_post("/web/v1/exposure", json={"duration": 1})
    assert response.status_code == 403
```

- [ ] **Step 2: Verify tests fail**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/web -v
```

- [ ] **Step 3: Port focused COSMOS66 modules and replace all device assumptions**

Preserve the native no-build frontend and image-first layout. Replace 8120, HDR 18-bit, PICam, gain labels, and 1/2/4-only binning with 4096, RAW8/12, ToupTek, numeric gain, and 1–8 binning.

```python
WEB_CAPABILITIES = {
    "sensor": {"width": 4096, "height": 4096, "pixelMicrons": 9.0},
    "readoutModes": ["RAW 12-bit", "RAW 8-bit"],
    "binning": list(range(1, 9)),
    "gain": {"min": 100, "max": 1650},
    "temperature": {"min": -50.0, "max": 40.0},
}
```

Formal exposure and preview call only the coordinator. Viewport rendering, statistics, and FITS use immutable image generations.

- [ ] **Step 4: Pass all Web tests**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/web -v
```

- [ ] **Step 5: Commit Web console**

```powershell
git add touptek4040/png_stream.py touptek4040/web_console.py touptek4040/web test/web
git commit -m "feat: add ToupTek Web control console"
```

---

### Task 12: Entry Points, Local GUI, Logging, and Simulator Smoke Test

**Files:**
- Create: `touptek4040/gui.py`
- Create: `run_alpaca.py`
- Create: `run_gui.py`
- Modify: `touptek4040/alpaca/app.py`
- Create: `test/unit/test_entrypoints.py`
- Create: `test/smoke/test_simulator_roundtrip.py`
- Create: `test/scripts/run_all.ps1`

**Interfaces:**
- Produces: `run_alpaca.main(argv=None) -> int`
- Produces: `run_gui.main(argv=None) -> int`

- [ ] **Step 1: Write failing CLI and full simulator round-trip tests**

```python
def test_source_sim_must_be_explicit():
    cfg = config_from_args(["--source", "sim"])
    assert cfg.camera.source == "sim"

def test_real_failure_never_falls_back(monkeypatch):
    monkeypatch.setattr(camera_factory, "open_toupcam", raising_failure)
    with pytest.raises(CameraError):
        build_server(AppConfig.production_defaults())
```

The smoke test starts on an ephemeral HTTP port and exercises management, Connect, settings, exposure, ImageBytes, JSON, Web PNG, FITS, Abort, and Disconnect.

- [ ] **Step 2: Verify tests fail**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_entrypoints.py test/smoke/test_simulator_roundtrip.py -v
```

- [ ] **Step 3: Implement entry points, graceful shutdown, and diagnostic GUI**

```python
def main(argv: Sequence[str] | None = None) -> int:
    config = config_from_args(argv)
    server = build_server(config)
    try:
        server.serve_forever()
        return 0
    finally:
        server.shutdown()
```

The GUI exposes enumeration, identity, RAW mode, gain, ROI, binning, temperature, cooler, one exposure, preview, and log view. It must display an ownership warning and fail clearly if the Alpaca process already owns the camera.

- [ ] **Step 4: Pass entry-point and round-trip tests**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_entrypoints.py test/smoke/test_simulator_roundtrip.py -v
powershell -ExecutionPolicy Bypass -File test/scripts/run_all.ps1
```

- [ ] **Step 5: Commit runnable applications**

```powershell
git add touptek4040/gui.py touptek4040/alpaca/app.py run_alpaca.py run_gui.py test/unit/test_entrypoints.py test/smoke test/scripts/run_all.ps1
git commit -m "feat: add runnable Alpaca and diagnostic apps"
```

---

### Task 13: Full Hardware Matrix and ConformU

**Files:**
- Create: `test/hardware/test_hardware_matrix.py`
- Create: `test/hardware/test_hardware_abort.py`
- Create: `test/hardware/test_hardware_cooling.py`
- Create: `test/hardware/test_hardware_orientation.py`
- Create: `test/scripts/run_hardware.ps1`
- Create: `test/scripts/run_conformu.ps1`
- Create: `test/conformu/README.md`
- Generate: `test/results/hardware/*`
- Generate: `test/results/conformu/*`

**Interfaces:**
- Consumes: complete server and real camera
- Produces: reproducible hardware and conformance evidence

- [ ] **Step 1: Write the hardware matrix and restoration fixture**

```python
@pytest.mark.hardware
@pytest.mark.parametrize("mode", [ReadoutMode.RAW12, ReadoutMode.RAW8])
@pytest.mark.parametrize("binning", range(1, 9))
def test_real_camera_mode_and_binning(real_camera, mode, binning):
    frame = real_camera.acquire(0.01, full_frame_roi(binning), binning, mode)
    expected = ((4096 // binning) // 2) * 2
    assert frame.pixels.shape == (expected, expected)
    assert int(frame.pixels.max()) <= (4095 if mode is ReadoutMode.RAW12 else 255)
```

Use a session fixture that snapshots and restores RAW, bit depth, gain, ROI, binning, trigger mode, TEC target, TEC state, and fan in `finally`.
Define `full_frame_roi(binning: int) -> Roi` in `test/hardware/conftest.py`; it returns the largest SDK-valid even square whose binned output dimension is `((4096 // binning) // 2) * 2`.

- [ ] **Step 2: Run the matrix and fix only evidence-backed failures**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File test/scripts/run_hardware.ps1
```

Expected: identity, RAW8/12, full frame, ROI, bins 1–8, short/continuous exposure, Abort, reconnect, temperature, cooler, and orientation all pass.

- [ ] **Step 3: Run ConformU Alpaca Protocol and Camera V4 profiles**

`run_conformu.ps1` starts the packaged or source simulator/server, launches ConformU with device type Camera, device number 0, and the configured host/port, then copies protocol and validation logs to `test/results/conformu/`.

```powershell
powershell -ExecutionPolicy Bypass -File test/scripts/run_conformu.ps1
```

- [ ] **Step 4: Record and resolve every conformance result**

Create `test/results/conformu/summary.md` with counts for pass, warning, issue, and error. Each remaining warning must quote the affected member, observed behavior, and the ASCOM rule that permits it. There must be zero unexplained failures.

- [ ] **Step 5: Commit tests and stable evidence summaries**

```powershell
git add test/hardware test/scripts/run_hardware.ps1 test/scripts/run_conformu.ps1 test/conformu test/results/hardware test/results/conformu
git commit -m "test: validate real camera and ASCOM conformance"
```

---

### Task 14: Packaging, User Guide, and Final Verification

**Files:**
- Create: `ToupTek4040Control.spec`
- Create: `使用说明.txt`
- Create: `test/packaging/test_distribution_assets.py`
- Create: `test/packaging/test_packaged_roundtrip.py`
- Create: `test/scripts/build_release.ps1`
- Create: `test/scripts/run_package_smoke.ps1`
- Generate: `test/results/final-verification.md`

**Interfaces:**
- Produces: `dist/ToupTek4040Control/ToupTek4040Alpaca.exe`
- Produces: `dist/ToupTek4040Control/ToupTek4040GUI.exe`

- [ ] **Step 1: Write failing distribution-content and packaged-roundtrip tests**

```python
def test_distribution_contains_required_files(dist_dir):
    required = {
        "ToupTek4040Alpaca.exe",
        "ToupTek4040GUI.exe",
        "toupcam.dll",
        "touptek4040.ini",
        "使用说明.txt",
    }
    assert required <= {path.name for path in dist_dir.iterdir()}
```

- [ ] **Step 2: Verify packaging tests fail before the package exists**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/packaging/test_distribution_assets.py -v
```

Expected: failure because `dist/ToupTek4040Control` does not exist.

- [ ] **Step 3: Implement two-executable PyInstaller packaging and Chinese guide**

Bundle `toupcam.py`, `toupcamsdk.20250623/win/x64/toupcam.dll`, Web assets, INI, and guide. Do not bundle the x86 DLL.

```python
datas = [
    ("touptek4040/web", "touptek4040/web"),
    ("touptek4040.ini", "."),
    ("使用说明.txt", "."),
]
binaries = [
    ("toupcamsdk.20250623/win/x64/toupcam.dll", "."),
]
```

The guide covers drivers, firewall, discovery, N.I.N.A., trusted-LAN security, real/simulator selection, RAW modes, binning, cooling, FITS, logs, diagnostics, and troubleshooting.

- [ ] **Step 4: Build and run complete final verification**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File test/scripts/build_release.ps1
powershell -ExecutionPolicy Bypass -File test/scripts/run_package_smoke.ps1
powershell -ExecutionPolicy Bypass -File test/scripts/run_all.ps1
powershell -ExecutionPolicy Bypass -File test/scripts/run_hardware.ps1
powershell -ExecutionPolicy Bypass -File test/scripts/run_conformu.ps1
```

Write `test/results/final-verification.md` with exact commands, test counts, hardware identity, full-frame transfer sizes, peak memory, ConformU result, package paths, and remaining hardware/operator checks.

- [ ] **Step 5: Commit the release definition and verification**

```powershell
git add ToupTek4040Control.spec 使用说明.txt test/packaging test/scripts/build_release.ps1 test/scripts/run_package_smoke.ps1 test/results/final-verification.md
git commit -m "build: package verified ToupTek Alpaca release"
```

---

## Final Acceptance Checklist

- [ ] `git status --short` contains no unexpected tracked changes.
- [ ] All test sources, fixtures, scripts, configuration, and results are under `test/`.
- [ ] The complete Camera V4 manifest test passes with no missing route.
- [ ] Simulator suite and full source round trip pass.
- [ ] Real camera RAW8/12, ROI, bins 1–8, cooling, Abort, reconnect, and orientation pass.
- [ ] Full 4096 × 4096 RAW12 JSON, ImageBytes, FITS, and Web pixels agree.
- [ ] ConformU Alpaca Protocol and Camera V4 have zero unexplained failures.
- [ ] Packaged simulator and real-camera smoke tests pass.
- [ ] N.I.N.A. discovery, connect, exposure, Abort, ROI/binning, cooling, ImageBytes, and disconnect are manually verified.
- [ ] Real hardware failure never switches to simulator.
- [ ] Chinese guide matches the delivered UI, ports, filenames, and behavior.
