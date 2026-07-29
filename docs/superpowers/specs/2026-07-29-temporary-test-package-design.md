# ToupTek 4040 Temporary Test Package Design

**Date:** 2026-07-29

**Status:** Approved for implementation

**Product:** ToupTek 4040 ASCOM Alpaca Camera
**Target camera:** sMAX16AM-U3-CL-GPS, 4096 × 4096

## Goal

Produce a clearly labeled Windows x64 temporary test package from the current
runnable worktree. The package provides both the local diagnostic GUI and the
Alpaca/Web service, uses a recognizable application icon, and includes a
complete Chinese usage guide.

This package is an interim operator build. It does not claim that the approved
camera-owner subprocess isolation is complete. After this temporary package is
delivered, owner isolation work resumes and a separate formal release is built.

## Distribution Shape

The unpacked distribution directory is:

```text
dist/ToupTek4040-Test/
├── ToupTek4040GUI.exe
├── ToupTek4040Alpaca.exe
├── toupcam.dll
├── touptek4040.json
├── 使用说明.txt
├── BUILD-INFO.txt
└── _runtime/
```

The same directory is archived as `dist/ToupTek4040-Test.zip`.

The `_runtime` directory contains only the Python and PyInstaller runtime
dependencies required by the two executables. The distribution contains only
the Windows x64 SDK DLL. It does not contain `toupcam.lib`, the x86 DLL, SDK
source, test files, or the legacy INI example. The SDK Python wrapper is frozen
as bytecode rather than distributed as source. The distributed JSON file
contains the approved camera serial and portable relative paths only.

Both executables carry the same application icon. `BUILD-INFO.txt` records the
source commit, dirty-worktree status, build time, and the temporary-test
designation so operator feedback can be traced to the exact artifact.

## Application Roles

`ToupTek4040GUI.exe` is the local camera diagnostic application. It exposes
Connect, Disconnect, single exposure, preview acquisition statistics, Abort,
and cooling controls.

`ToupTek4040Alpaca.exe` starts the ASCOM Alpaca service, discovery listener, and
Web image console. The local Web console is available at:

```text
http://127.0.0.1:11111/ui/
```

Both programs load `touptek4040.json` from their executable directory by
default. A real-camera connection failure is reported to the operator and must
never select simulation automatically.

The real camera is single-owner hardware. Operators must not run the GUI and
Alpaca service against the physical camera at the same time. The guide directs
them to close one program before starting the other.

## Temporary-Version Safety Boundary

The temporary package uses the current in-process real-camera runtime. Native
SDK calls can still hang the process. The package and guide must state this
limitation clearly and must not claim hard timeout containment.

If the temporary build becomes unresponsive, the documented recovery is:

1. close the application;
2. if it cannot close, end only that application process;
3. reconnect the camera USB connection if necessary;
4. restart one application and reconnect explicitly.

The formal release will replace this boundary with the already approved
supervised owner subprocess. Completion of the temporary package does not
close or supersede the owner-process implementation plan.

## Icon Design

The selected direction is **A — Deep-space lens**:

- dark navy rounded-square camera body;
- luminous cyan optical lens;
- simplified sensor geometry inside the lens;
- no text, letters, model numbers, or watermark;
- strong silhouette and contrast at 16 × 16;
- transparent outer background;
- restrained professional scientific-camera appearance.

The project retains a high-resolution PNG source and a Windows ICO containing
16, 24, 32, 48, 64, 128, and 256 pixel sizes. Both executables use the ICO.

## Chinese Usage Guide

`使用说明.txt` documents:

- supported Windows x64 environment and ToupTek driver installation;
- USB connection and the approved physical camera identity;
- JSON file location and every distributed configuration field;
- GUI launch, connection, single exposure, preview statistics, cooling, Abort,
  and shutdown;
- Alpaca service launch and the Web console URL;
- browser Connect, preview, image display, stretch, pixel inspection, and FITS
  download;
- N.I.N.A. Alpaca host, port, device number, and connection sequence;
- RAW 8-bit and RAW 12-bit modes, ROI, binning, exposure, gain, and cooling;
- logs and diagnostic information to attach to a fault report;
- the one-program physical-camera ownership rule;
- the temporary build's in-process SDK hang risk and recovery procedure;
- the future formal owner-isolated release and explicit reconnect behavior.

## Build and Packaging

PyInstaller builds two Windows GUI/console entry points from `run_gui.py` and
`run_alpaca.py`. The frozen application includes the Web assets, required
Python modules, the x64 `toupcam.dll`, the JSON configuration, the Chinese
guide, and the application icon.

The build script removes only its known temporary output and previous
`dist/ToupTek4040-Test` artifact. It must not clean the repository or touch the
existing Task 13 and owner-process worktree changes.

The build source is the current runnable worktree. The build identity records
that the source was dirty when applicable; the temporary package is therefore
not represented as a reproducible formal release.

## Verification

Automated distribution checks require:

- both expected executable names;
- x64 `toupcam.dll`;
- `touptek4040.json`;
- `使用说明.txt`;
- `BUILD-INFO.txt`;
- one private `_runtime` dependency directory;
- absence of `touptek4040.ini`, `.lib`, x86 SDK files, and test sources;
- valid multi-size ICO association;
- executable-directory JSON resolution;
- simulator start and shutdown without a real-camera fallback;
- Alpaca management endpoint and `/ui/` Web console response;
- ZIP contents matching the unpacked distribution.

Operator verification uses the approved physical camera for one bounded stage:

1. snapshot the camera settings;
2. start only one packaged application;
3. Connect;
4. acquire one short, small-ROI RAW12 image;
5. Disconnect and close;
6. restore and re-read all camera settings;
7. confirm no application or test listener remains.

Any restoration failure is reported honestly and fails package acceptance.
The temporary package does not run the known high-risk full-frame structural
transition matrix.

## Acceptance

The temporary package is accepted when:

- the icon is legible in Explorer, the taskbar, and the executable properties;
- both applications start from the unpacked directory;
- configuration and Web assets resolve from the packaged application;
- simulator and Web smoke checks pass;
- the bounded physical-camera smoke and restoration check pass;
- the Chinese guide matches the delivered filenames and observed behavior;
- the ZIP can be extracted to a new directory and run without the source tree;
- every temporary limitation is labeled without making owner-isolation claims.
