# Reproducing ConformU validation

Prerequisites are deliberately local-only:

- the approved camera `sMAX16AM-U3-CL-GPS`,
  serial `TP241025143218C92B01151AEC40CB7`;
- bundled SDK `toupcamsdk.20250623/`;
- ASCOM Conform Universal 2.0.0.16486, 64-bit, installed in its standard
  Windows location.

Run from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File test/scripts/run_conformu.ps1
```

The script starts a loopback-only Alpaca server at
`http://127.0.0.1:11113/api/v1/camera/0`, runs the `alpacaprotocol` and
`conformance` command-line profiles, requests a bounded graceful shutdown, and
restores the pre-run RAW/bit depth/gain/ROI/binning/trigger/exposure/auto
exposure/orientation/TEC target/TEC/fan/HEAT/HEAT_MAX snapshot in a `finally`
path. It never falls back to simulation.

The 34 allowed Protocol issues are the two common-parameter casing probes on
each of 17 PUT endpoints. This service rejects incorrectly cased PUT form
parameter names with HTTP 400 as required by Alpaca API v12 section 2.2.3;
ConformU 2.0.0.16486 expects the older HTTP 200 behavior and therefore reports
those compliant rejections as issues. Camera V4 also reports one allowed idle
`StopExposure` issue: the Camera contract defines an idle call as a successful
no-op, while `CanStopExposure=False` still rejects an in-progress stop because
this camera cannot return a valid early-read image.

Logs, machine-readable ConformU results, server diagnostics, and the hardware
restoration record are written to `test/results/conformu/`.
