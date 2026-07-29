# ToupTek 4040 Temporary Test Package Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a clearly labeled Windows x64 portable test package containing the current runnable GUI, Alpaca/Web service, selected deep-space-lens icon, strict JSON configuration, and a complete Chinese usage guide.

**Architecture:** PyInstaller creates two onedir executables that share one private `_runtime` directory. The official SDK Python wrapper is frozen as bytecode, the x64 DLL and operator files remain beside the executables, and a PowerShell build pipeline produces a traceable directory plus ZIP without cleaning unrelated worktree state.

**Tech Stack:** Python 3.13, PyInstaller 6, PowerShell 7/Windows PowerShell, System.Drawing, built-in ImageGen, ToupTek SDK 2025-06-23, Falcon 4, Waitress 3, pytest.

## Global Constraints

- The output is explicitly named and documented as `ToupTek4040-Test`; it is not the formal owner-isolated release.
- Preserve every existing Task 13 and owner-process worktree change; do not reset, restore, clean, or overwrite unrelated paths.
- The only approved physical device is model `sMAX16AM-U3-CL-GPS`, serial `TP241025143218C92B01151AEC40CB7`, resolution 4096 × 4096.
- Real-camera failure must never fall back to simulation.
- The package contains only the x64 `toupcam.dll`; it excludes `toupcam.lib`, x86 SDK files, SDK source, tests, and `touptek4040.ini`.
- The official SDK Python wrapper is frozen as bytecode, not shipped as a `.py` source file.
- Both executables load `touptek4040.json` from their executable directory and share the same multi-size application icon.
- `BUILD-INFO.txt` records the source commit, dirty-worktree state, build time with timezone, and `TEMPORARY TEST BUILD`.
- The guide and package must state that the current in-process SDK can hang and must not claim owner-process timeout containment.
- All packaging tests, scripts, fixtures, and generated evidence live under `test/`.
- Hardware verification snapshots and restores every mutated camera setting in `finally`, closes the exact application process, and reports restoration failures honestly.
- Generated `dist/` and `build/` artifacts are not committed.

---

### Task 1: Deep-Space-Lens Application Icon

**Files:**
- Create: `assets/touptek4040-icon.png`
- Create: `assets/touptek4040.ico`
- Create: `test/scripts/build_icon.ps1`
- Create: `test/packaging/test_icon_assets.py`

**Interfaces:**
- Produces: transparent 1024 × 1024 PNG source
- Produces: ICO entries at 16, 24, 32, 48, 64, 128 and 256 pixels
- Produces: `test/scripts/build_icon.ps1 -Source <png> -Output <ico>`

- [ ] **Step 1: Write icon asset RED tests**

Create tests that parse the PNG signature/IHDR and ICO directory directly:

```python
EXPECTED_ICON_SIZES = {16, 24, 32, 48, 64, 128, 256}


def test_icon_source_is_square_rgba_png():
    width, height, color_type = read_png_ihdr(Path("assets/touptek4040-icon.png"))
    assert (width, height, color_type) == (1024, 1024, 6)


def test_windows_icon_contains_every_required_size():
    entries = read_ico_entries(Path("assets/touptek4040.ico"))
    assert {entry.width for entry in entries} == EXPECTED_ICON_SIZES
    assert all(entry.payload.startswith(b"\x89PNG\r\n\x1a\n") for entry in entries)
```

The PNG test must also require transparent corners, a nonempty opaque center,
and no embedded text metadata.

- [ ] **Step 2: Verify RED**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/packaging/test_icon_assets.py -v
```

Expected: fail because the icon assets do not exist.

- [ ] **Step 3: Generate the approved icon source**

Use built-in ImageGen with this exact direction:

```text
Use case: logo-brand
Asset type: Windows scientific-camera application icon
Primary request: a professional deep-space camera lens icon
Subject: dark navy rounded-square camera body, luminous cyan optical lens, simplified sensor diamond inside the lens
Composition: centered, symmetric, bold silhouette, generous outer padding, readable at 16 px
Style: polished vector-friendly 3D icon, restrained scientific instrument appearance
Background: perfectly flat solid #00ff00 chroma-key background
Constraints: no words, no letters, no numbers, no stars, no watermark; do not use #00ff00 in the icon; no cast shadow outside the rounded body
```

Remove the chroma key with the installed ImageGen helper, validate transparent
corners, and save the final 1024 × 1024 RGBA PNG at the required path.

- [ ] **Step 4: Implement deterministic multi-size ICO conversion**

`build_icon.ps1` uses `System.Drawing.Bitmap` with high-quality bicubic
resampling to produce each required PNG payload, then writes an ICO header,
seven directory entries and the payload offsets with `BinaryWriter`. It must
write to a temporary sibling and atomically replace only the requested output.

- [ ] **Step 5: Run GREEN tests**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File test/scripts/build_icon.ps1 -Source assets/touptek4040-icon.png -Output assets/touptek4040.ico
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/packaging/test_icon_assets.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit icon assets**

```powershell
git add assets/touptek4040-icon.png assets/touptek4040.ico test/scripts/build_icon.ps1 test/packaging/test_icon_assets.py
git commit -m "assets: add ToupTek 4040 application icon"
```

---

### Task 2: Frozen SDK Loader Boundary

**Files:**
- Modify: `touptek4040/toupcam_loader.py`
- Modify: `test/unit/test_toupcam_loader.py`

**Interfaces:**
- Preserves: `load_default_toupcam_module() -> ModuleType`
- Produces: frozen-mode import of bundled `toupcam` bytecode
- Consumes: `sys.frozen`, `sys.executable`, `importlib.import_module`

- [ ] **Step 1: Write frozen-loader RED tests**

Add tests that set frozen runtime state without loading the real DLL:

```python
def test_frozen_loader_imports_bundled_wrapper_and_retains_dll_cookie(
    monkeypatch, tmp_path
):
    executable = tmp_path / "ToupTek4040GUI.exe"
    module = ModuleType("toupcam")
    imported = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(importlib, "import_module", lambda name: imported.append(name) or module)
    cookie = object()
    monkeypatch.setattr(os, "add_dll_directory", lambda path: cookie)

    assert load_default_toupcam_module() is module
    assert imported == ["toupcam"]
    assert module._touptek4040_dll_directory_cookie is cookie
```

Also prove source mode preserves the existing explicit wrapper-plus-DLL search
and that missing real runtime files still raise `ToupTekSdkNotFoundError`.

- [ ] **Step 2: Verify RED**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_toupcam_loader.py -v
```

Expected: frozen-mode test fails because the loader only searches source files.

- [ ] **Step 3: Implement minimal frozen loading**

When `sys.frozen` is true, add the executable directory with
`os.add_dll_directory`, import the PyInstaller-hidden `toupcam` module, retain
the directory cookie on that module, and return it. Do not catch a missing DLL
or wrapper and do not select simulation. Leave the source path unchanged.

- [ ] **Step 4: Run loader regression**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit/test_toupcam_loader.py test/unit/test_entrypoints.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit frozen loading**

```powershell
git add touptek4040/toupcam_loader.py test/unit/test_toupcam_loader.py
git commit -m "build: support frozen ToupTek SDK loading"
```

---

### Task 3: Chinese Guide, PyInstaller Package and Simulator Smoke

**Files:**
- Create: `使用说明.txt`
- Create: `ToupTek4040Test.spec`
- Create: `test/scripts/build_test_release.ps1`
- Create: `test/scripts/run_test_package_smoke.ps1`
- Create: `test/packaging/test_distribution_assets.py`
- Create: `test/packaging/test_packaged_smoke.py`
- Generate, do not commit: `dist/ToupTek4040-Test/`
- Generate, do not commit: `dist/ToupTek4040-Test.zip`

**Interfaces:**
- Produces: `test/scripts/build_test_release.ps1`
- Produces: `test/scripts/run_test_package_smoke.ps1 -DistDir <path>`
- Produces: Chinese operator guide encoded as UTF-8 with BOM
- Consumes: icon assets, frozen loader, guide, JSON, x64 SDK DLL and wrapper

- [ ] **Step 1: Write distribution and packaged-smoke RED tests**

Require the exact public distribution contract:

```python
REQUIRED_ROOT = {
    "ToupTek4040GUI.exe",
    "ToupTek4040Alpaca.exe",
    "toupcam.dll",
    "touptek4040.json",
    "使用说明.txt",
    "BUILD-INFO.txt",
    "_runtime",
}


def test_distribution_contains_public_contract(built_test_distribution):
    names = {path.name for path in built_test_distribution.iterdir()}
    assert REQUIRED_ROOT <= names
    assert "touptek4040.ini" not in names
    assert not any(path.suffix.casefold() == ".lib" for path in built_test_distribution.rglob("*"))
    assert not any(path.name == "toupcam.py" for path in built_test_distribution.rglob("*"))
```

Validate that the distributed JSON loads through `load_config`, contains the
approved serial, `source=toupcam`, relative logging directory, and approved
finite owner timeout defaults. Validate `BUILD-INFO.txt` field structure rather
than an exact timestamp.

The smoke helper starts only the exact packaged process it owns. Require:

```python
def test_packaged_alpaca_simulator_serves_management_and_ui(package_server):
    assert package_server.get("/management/v1/description").status_code == 200
    assert package_server.get("/ui/").status_code == 200


def test_packaged_simulator_connects_and_returns_one_image(package_server):
    package_server.web_command("connection", {"connected": True})
    package_server.web_settings(roi={"x": 0, "y": 0, "width": 32, "height": 32})
    package_server.web_command("exposure", {"duration": 0.01})
    state = package_server.wait_for_image(timeout_s=10)
    assert state["image"]["width"] == 32
    assert package_server.get("/web/v1/viewport.png").content.startswith(b"\x89PNG")
```

The fixture launches `ToupTek4040Alpaca.exe --source sim` on a dedicated
loopback port, records the exact PID, and stops only that PID in `finally`.
Add a GUI smoke that launches `ToupTek4040GUI.exe --source sim`, requires it to
remain alive long enough to create the window, then requests normal window
close and uses exact-PID termination only as cleanup fallback.

- [ ] **Step 2: Verify packaging RED**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/packaging/test_distribution_assets.py test/packaging/test_packaged_smoke.py -v
```

Expected: fail because the package and smoke fixtures do not exist.

- [ ] **Step 3: Write and self-review the Chinese guide**

Write the approved sections in direct operator language. Use the delivered
filenames verbatim, give `http://127.0.0.1:11111/ui/`, identify the approved
camera and serial, explain the one-application rule, and label the SDK hang
risk and recovery steps. State that GUI preview shows frame statistics while
the Web console displays the actual image.

Compare every filename, URL, default and button name against `run_gui.py`,
`touptek4040/gui.py`, `touptek4040/web/index.html`, `touptek4040.json` and the
design. Human prose itself is not tested by source-text assertions.

- [ ] **Step 4: Implement the PyInstaller spec**

Use two `Analysis`/`EXE` objects and one `COLLECT` directory named
`ToupTek4040-Test`. Set GUI `console=False`, Alpaca `console=True`,
`contents_directory="_runtime"`, and the same ICO on both. Add the official
SDK Python directory to `pathex`, include hidden import `toupcam`, include Web
assets under `touptek4040/web`, and do not include SDK source as data.

- [ ] **Step 5: Implement the build script**

The script:

1. resolves the repository and official SDK paths;
2. verifies Python, PyInstaller, icon, guide, wrapper and x64 DLL;
3. removes only `build/ToupTek4040Test`, `dist/ToupTek4040-Test` and the prior
   test ZIP after resolving each target inside the repository;
4. runs PyInstaller with the spec and a dedicated work path;
5. copies the x64 DLL, JSON and guide to the distribution root;
6. writes UTF-8 `BUILD-INFO.txt` with designation, commit, dirty state and
   ISO-8601 local build time;
7. rejects any `.lib`, x86 DLL, `toupcam.py`, INI or test path in the output;
8. creates the ZIP from the completed directory.

- [ ] **Step 6: Run distribution GREEN and simulator smoke**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File test/scripts/build_test_release.ps1
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/packaging/test_icon_assets.py test/packaging/test_distribution_assets.py test/packaging/test_packaged_smoke.py -v
powershell -ExecutionPolicy Bypass -File test/scripts/run_test_package_smoke.ps1 -DistDir dist/ToupTek4040-Test
```

Expected: all pass; no packaged process or dedicated listener remains.

- [ ] **Step 7: Commit package sources, not binaries**

```powershell
git add 使用说明.txt ToupTek4040Test.spec test/scripts/build_test_release.ps1 test/scripts/run_test_package_smoke.ps1 test/packaging/test_packaged_smoke.py test/packaging/test_distribution_assets.py
git commit -m "build: create temporary ToupTek test package"
```

---

### Task 4: Bounded Packaged Hardware Check and Delivery Evidence

**Files:**
- Create: `test/scripts/run_test_package_hardware.ps1`
- Create: `test/results/temporary-package/summary.md`
- Generate, do not commit: `test/results/temporary-package/hardware.json`
- Regenerate, do not commit: `dist/ToupTek4040-Test.zip`

**Interfaces:**
- Consumes: packaged Alpaca executable and approved physical camera
- Produces: exact process, exposure and restoration evidence

- [ ] **Step 1: Add a packaged hardware runner**

The script first calls the existing camera identity/snapshot helper, then starts
only `ToupTek4040Alpaca.exe` on a dedicated loopback port. Through the Web API
it Connects, configures RAW12/bin1/16 × 16 ROI/short exposure, acquires exactly
one frame, Disconnects, and exits. It records exact PID, elapsed times, image
dimensions, dtype/range, and the source process exit.

Every mutation and process start is inside `try/finally`. The `finally` block
terminates only the exact packaged PID if still alive, runs the camera recovery
helper, compares all 15 snapshot fields, records every restoration error, and
fails if any application listener or exact process remains.

- [ ] **Step 2: Run pre-hardware package verification**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File test/scripts/build_test_release.ps1
powershell -ExecutionPolicy Bypass -File test/scripts/run_test_package_smoke.ps1 -DistDir dist/ToupTek4040-Test
```

Expected: simulator/Web package smoke passes before touching hardware.

- [ ] **Step 3: Run one bounded real-camera stage**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File test/scripts/run_test_package_hardware.ps1 -DistDir dist/ToupTek4040-Test
```

Expected: exact approved identity, one 16 × 16 RAW12 frame, Disconnect, all 15
settings restored, no restoration error, no exact packaged process or listener.
Do not run the full structural matrix.

- [ ] **Step 4: Write final temporary-package summary**

Record:

- source commit and dirty build marker;
- icon and public file checks;
- build command and executable paths;
- simulator management, Web UI and image smoke results;
- physical identity, image size and restoration result;
- ZIP path and SHA-256;
- explicit temporary in-process hang limitation;
- statement that owner-process work resumes next.

Normalize repository paths to `<repo>` and do not claim formal release status.

- [ ] **Step 5: Rebuild ZIP after final public-file verification**

Run:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/packaging -v
Get-FileHash dist/ToupTek4040-Test.zip -Algorithm SHA256
```

Expected: all package tests pass and the reported SHA-256 matches the delivered
ZIP.

- [ ] **Step 6: Commit scripts and concise evidence**

```powershell
git add test/scripts/run_test_package_hardware.ps1 test/results/temporary-package/summary.md
git commit -m "test: verify temporary packaged camera build"
```

---

## Temporary Package Acceptance Checklist

- [ ] `dist/ToupTek4040-Test/` and `dist/ToupTek4040-Test.zip` exist.
- [ ] Both executables carry the approved deep-space-lens icon.
- [ ] JSON, guide, build identity, x64 DLL and `_runtime` are present.
- [ ] No INI, `.lib`, x86 SDK file, SDK Python source or test file is present.
- [ ] Simulator GUI and Alpaca/Web smoke pass from the unpacked directory.
- [ ] The Web console displays one simulator image.
- [ ] One approved-camera 16 × 16 RAW12 exposure passes and all 15 settings restore.
- [ ] No packaged process or dedicated test listener remains.
- [ ] The guide labels the current SDK hang risk and does not claim owner isolation.
- [ ] The ZIP SHA-256 and exact artifact path are recorded.
- [ ] Owner-process implementation progress remains preserved for immediate resumption.
