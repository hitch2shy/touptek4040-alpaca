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
exposure/orientation/TEC target/TEC/fan snapshot in a `finally` path. It never
falls back to simulation.

Logs, machine-readable ConformU results, server diagnostics, and the hardware
restoration record are written to `test/results/conformu/`.
