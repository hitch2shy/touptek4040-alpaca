# Task 10 Management, Setup, Discovery, and Protocol Report

## Scope

- Added the three Alpaca Management API v1 JSON endpoints with exact fields,
  types, casing, transaction/error envelope, and approved metadata.
- Added the required main and Camera 0 setup pages as read-only, escaped HTML.
- Added shared-port IPv4 discovery and optional IPv6 multicast discovery.
- Preserved Task 7 request parsing precedence and Task 9 image transport routes.
- Added stable protocol-level HTTP 400/405 and device-level HTTP 200 coverage.

## Identity rule

- Real cameras use UUIDv5 over the resolved physical camera serial.
- Real `toupcam` startup fails before app creation if the bootstrap has not
  resolved a non-empty serial; no placeholder or Python process hash is used.
- The simulator uses the fixed serial identity `SIM-4040`.
- The resulting UID is stable across restarts and differs for different serials.

## RED evidence

The initial focused run is captured in `test/results/task10-red.txt`:

- 43 tests collected
- 26 failed because management/setup/discovery were absent
- 17 existing protocol boundaries passed

Two supplemental regression cycles were also observed independently:

- unexpected discovery listener exit left its socket open before the lifecycle
  fix;
- unresolved real-camera serial did not fail before the identity rule was
  tightened.

## Official contract checked

- ASCOM Alpaca Management API v1 OpenAPI:
  `https://ascom-standards.org/api/AlpacaManagementAPI_v1.yaml`
- ASCOM Alpaca API Reference v12:
  `https://ascom-standards.org/AlpacaDeveloper/ASCOMAlpacaAPIReference.html`
- ASCOM AlpycaDevice Platform 7 reference:
  `https://github.com/ASCOMInitiative/AlpycaDevice`

The discovery request is accepted only as the exact 16-byte ASCII
`alpacadiscovery1` message. The response is the compact ASCII JSON
`{"AlpacaPort":n}` and is sent only to the datagram source.

## Verification

- Focused Task 10: 46 passed
- Unit + Alpaca: 639 passed
- Full automated suite: 639 passed, 4 hardware tests skipped by their explicit
  `TOUPTEK_HARDWARE_TEST=1` gate
- Python compilation: passed
- `git diff --check`: passed

## Handoff

Task 12 bootstrap must enumerate the selected real camera and inject its
resolved serial into the frozen application configuration before calling
`create_app()`. Simulator startup needs no serial override.

Real UDP broadcast/multicast firewall behavior, physical-camera tests, and
ConformU remain later acceptance tasks; Task 10 tests use controlled sockets
and do not transmit to public or LAN addresses.
