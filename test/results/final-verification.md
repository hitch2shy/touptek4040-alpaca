# ToupTek4040Control formal release verification

Date: 2026-07-30 (Asia/Shanghai)

## Artifact

- Distribution: `<repo>/dist/ToupTek4040Control`
- Archive: `<repo>/dist/ToupTek4040Control.zip`
- Source commit: `faec12fcb2691d5f21958a997b3daecbffbce560`
- Release-input manifest SHA-256:
  `B9D240D01DAEAB1E9ECB439332CC9D619A0599DB362554A5F90F375922FF45E3`
- ZIP size: 38,137,118 bytes
- ZIP SHA-256:
  `BA3B2296A90DB82F81929026D3B3F833420CF83EE82D074D6E8071EFD3D231C6`
- Executables: `ToupTek4040Alpaca.exe`, `ToupTek4040GUI.exe`
- Runtime: one x64 `toupcam.dll`; no x86 DLL, `.lib`, SDK source wrapper,
  legacy INI or test tree.
- Distributed configuration: strict `touptek4040.json`, exact model/serial
  target and all eight owner fields including
  `structural_reopen_settle_s=0.5` and
  `exposure_overhead_s=10.0` (minimum accepted overhead 0.252 seconds).

## Exact verification commands and results

1. `powershell -ExecutionPolicy Bypass -File test/scripts/build_release.ps1`
   - exit 0; built only from the clean Git archive of the recorded source
     commit; dirty release inputs are rejected before distribution mutation.
2. `powershell -ExecutionPolicy Bypass -File test/scripts/run_package_smoke.ps1`
   - 17/17 formal distribution, provenance, icon, simulator, GUI, frozen
     spawn/shared-memory and HTTP tests;
   - packaged real-camera smoke passed.
3. `powershell -ExecutionPolicy Bypass -File test/scripts/run_all.ps1`
   - 1,427 passed, 36 hardware-gated skips, 0 failed.
4. `powershell -ExecutionPolicy Bypass -File test/scripts/run_owner_faults.ps1`
   - 25 passed, 2 intentional outer-runner skips;
   - official 14/14 boundary/leak cases PASS.
5. `powershell -ExecutionPolicy Bypass -File test/scripts/run_hardware.ps1`
   - first stage 2/2, matrix 45/45, owner recovery 1/1.
6. `powershell -ExecutionPolicy Bypass -File test/scripts/run_conformu.ps1`
   - overall strict PASS.
   - Protocol: 0 errors, exactly 34 allow-listed API V12 PUT common-parameter
     casing findings.
   - Camera V4: 0 errors, 0 alerts, exactly 18 allow-listed findings
     (PercentCompleted idle, idle StopExposure, ConformU ImageArrayVariant
     interpretation).
7. `python -m pytest ... test_raw12_4096_square_copy... -v -s`
   - 1/1 passed; frame 33,554,432 bytes; traced Python peak 33,599,127 bytes.

## Real camera, formats and restoration

- Camera: `sMAX16AM-U3-CL-GPS`,
  `TP241025143218C92B01151AEC40CB7`, USB 3.x SuperSpeed.
- Full-frame RAW12 and RAW8 transfers: 4096 × 4096.
- Native bins 1–8, exact software Bin 3 1365 × 1365, odd/right-edge ROI,
  repeated acquisition, Abort, reconnect, cooling reads and array orientation
  passed.
- Packaged real mode ran in an isolated spawned owner, returned RAW12 pixels,
  Alpaca JSON ImageArray and ImageBytes, Web PNG and FITS, and completed
  Abort/Disconnect. Package PID 28136 and owner PID 20360 differed; the owner
  remained supervised after Disconnect and was dead after exact package
  shutdown. Its shared-frame registry and live-segment lists were empty.
- Packaged heater: HEAT/HEAT_MAX began 4/4 and the alternate 0 was deliberately
  left active. No manual pre-Disconnect restore occurred. Disconnect restored
  4 automatically, reconnect confirmed 4, then a clean Disconnect completed.
  Hardware run `23e8af4d26fc2ba8507a5e989a6437f1` and both ConformU profiles
  independently restored complete initial snapshots with HEAT/HEAT_MAX 4/4.
- Owner fault audit covered startup, Open, get, set, Trigger, heater read,
  heater write, frame publication, Abort and Close. Each hung operation ran
  once, the exact child died within its derived finite bound, replacement
  generation was disconnected and no frame was published.
- Shared-memory crash points before creation, after creation, after descriptor
  send and after parent copy all left no registry or OS segment.
- The frozen executable probe independently spawned a Windows
  `multiprocessing` child, exchanged exactly one shared-memory publication and
  ACK, then proved the child dead and the segment absent.
- `git diff faec12fcb2691d5f21958a997b3daecbffbce560..HEAD` over every
  release-input path is empty; the later commit contains tests and normalized
  evidence only.

## Preserved user service

- The pre-existing service on TCP 11111 remained the same PID 26732 throughout;
  it was never stopped.
- It was temporarily disconnected only for exclusive hardware/package/ConformU
  stages under the user's authorization, then explicitly reconnected.
- Final state: connected, idle, exact approved model and serial.

## Remaining manual N.I.N.A. operator checks

- On the observing workstation, add Alpaca Camera 0 at
  `http://127.0.0.1:11111`, connect and take one short light frame.
- Confirm N.I.N.A. receives the expected 4096 × 4096 geometry and FITS metadata
  for the intended observing workflow.
- While N.I.N.A. remains connected, confirm the Web-only heater selector is
  visible and can make one operator-approved change; Disconnect must return it
  to the connection-time level.
- Confirm Windows Firewall permits TCP 11111 and UDP 32227 only on the trusted
  local/private network; do not expose the service to the public Internet.
