# Real hardware matrix

- Command: `powershell -ExecutionPolicy Bypass -File test/scripts/run_hardware.ps1`
- Environment: `TOUPTEK_HARDWARE_TEST=1` (script process only)
- Approved camera: `sMAX16AM-U3-CL-GPS` / `TP241025143218C92B01151AEC40CB7`
- Pytest: `============================= 40 passed in 30.48s =============================`
- RAW/bin combinations recorded: 16/16
- Native hardware-bin matrix rule: `((4096 // bin) // 2) * 2` on both axes
- ASCOM full-frame bin 3 fallback: exact `1365 x 1365` verified in RAW12 and RAW8
- Cooler policy: state/target setters receive their existing values only; no cooler or fan transition
- Physical scene orientation: not calibrated because no known optical orientation target was supplied
- Restoration: `verified: final snapshot exactly equals initial snapshot`

Detailed per-case evidence is in `cases.jsonl`; no raw image buffers are stored.
Root-cause and bounded-memory details are in `root-cause-summary.md`.
