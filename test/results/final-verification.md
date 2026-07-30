# ToupTek4040Control formal release verification

Date: 2026-07-30 (Asia/Shanghai)

## Artifact

- Distribution: `<repo>/dist/ToupTek4040Control`
- Archive: `<repo>/dist/ToupTek4040Control.zip`
- Source commit: `549351f0086add3d7c6992247449cc81130d12b9`
- Release-input manifest SHA-256:
  `5922C4B79329EDCC230A2C55B2ED90BD2CA566339FDF9E2538F83E129C024DD5`
- Official SDK lock SHA-256:
  `D29F9569262264D900991B8CEC6C420F2FE95188335333E3E802DF5C3B471DF5`
- Official SDK aggregate SHA-256:
  `EB807DA73513B59BF839EFD3891F406AFD41CC616648A5097B588898B5005046`
- Official `python/toupcam.py`: version `59.28760.20250623`,
  171,655 bytes, SHA-256
  `B472D455D4506A3CA642A1DF0B77986FEA42D97F7DEFFE1EA33B6B1BB06F5520`.
- Official `win/x64/toupcam.dll`: version `59.28760.20250623`,
  27,495,712 bytes, SHA-256
  `F81D9705C1C6AF3B5D5C07FEDEC2CF09A8A113DC6D6EFEBD58798E9B12C04460`.
- ZIP size: 38,818,131 bytes
- ZIP SHA-256:
  `1E29E168614443672FB266EA4FA8F76571CFCC1FA47C0E4F279EB5C10CE677AF`
- Executables: `ToupTek4040Alpaca.exe`, `ToupTek4040GUI.exe`
- Runtime: one x64 `toupcam.dll`; no x86 DLL, `.lib`, SDK source wrapper,
  legacy INI or test tree.
- Distributed configuration: strict `touptek4040.json`, exact model/serial
  target and all eight owner fields including
  `structural_reopen_settle_s=0.5` and
  `exposure_overhead_s=10.0` (minimum accepted overhead 0.252 seconds).

## Exact verification commands and results

1. `powershell -ExecutionPolicy Bypass -File test/scripts/build_release.ps1`
   - exit 0; built from the clean Git archive of the recorded source commit
     plus the two official external SDK files verified against the committed
     lock before any distribution mutation;
   - separate one-byte wrapper and DLL mutation tests both rejected the build
     before changing the existing BUILD-INFO or ZIP.
2. `python -m pytest test/packaging -q` and
   `test/scripts/run_test_package_hardware.ps1 ... -VerifyFormalContract`
   - 21/21 formal distribution, locked SDK provenance, icon, simulator, GUI,
     frozen spawn/shared-memory and HTTP tests;
   - packaged real-camera smoke passed.
3. `powershell -ExecutionPolicy Bypass -File test/scripts/run_all.ps1`
   - 1,431 passed, 36 hardware-gated skips, 0 failed.
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
  Abort/Disconnect. The final formal run used package PID 37480 and owner PID
  37132. PID 37132 was the current supervisor owner after Disconnect and
  Reconnect; after exact package shutdown every observed owner PID was dead
  (`alive=[]`). A preceding diagnostic run observed an Abort/recovery
  replacement from stale PID 21824 to current PID 37676; the smoke therefore
  tracks every observed generation instead of treating the initial PID as the
  permanent owner.
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
  ACK, read the live registry name, then attempted to reopen that exact name
  after child exit and proved the child, registry entry and segment absent.
- The real packaged smoke observed three descriptor/registry segment names
  during acquisition, probed all three after top-level shutdown and measured
  empty final registry and live-segment lists. A forged live-mapping regression
  test proves the reopen probe fails closed when a named segment still exists.
- `git diff 549351f0086add3d7c6992247449cc81130d12b9..HEAD` over every
  release-input path is empty; the later commit contains tests and normalized
  evidence only.
- The integration harness allows 180 seconds only for its nested complete
  owner-fault audit. Product owner startup, command, exposure, Abort and
  Disconnect timeout contracts were not relaxed.

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
