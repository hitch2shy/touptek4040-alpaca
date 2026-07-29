# Task 11 Web Image Services and Control Console Report

## Scope

- Added `/ui/` and the native no-build ES module console on the same Falcon
  application and port as Alpaca.
- Added strict `/web/v1/*` state, capabilities, acquisition settings, cooling,
  connection, Preview, exposure, Abort, viewport PNG, raw pixel, statistics,
  FITS, logs, and SSE services.
- Kept all camera actions on the injected `CameraCoordinator`; no Web camera
  handle, fallback owner, or GET-side camera action was introduced.
- Consumed the coordinator's immutable formal and Preview image generations for
  display, analysis, raw pixels, and FITS.

## Scientific image services

- Display PNG rendering is row-streamed, dimension-bounded to 2048 pixels per
  edge, generation-tagged, and uses a display-only black/white/gamma stretch.
- Statistics, a fixed 256-bin histogram, peak position, and axis FWHM are
  deterministic and calculated in bounded row chunks.
- Full-frame 4096 × 4096 regression coverage constrains PNG temporary memory
  below 16 MiB and statistics temporary memory below 20 MiB.
- FITS downloads reuse the Task 9 unsigned 16-bit streaming implementation.
- Display rendering and analysis never mutate the immutable scientific pixels.

## Security boundary

- Every mutation requires an exact same-origin `Origin`/`Host` match,
  `application/json`, and `X-ToupTek-Console: 1`.
- Opaque, cross-site, missing-header, oversized, malformed, wrong-type,
  out-of-range, and unknown-field requests are rejected before camera action.
- Acquisition and cooling mutations are independent. Initial settings hydrate
  the live target temperature and cooler state, so applying geometry cannot
  overwrite cooling changed by another client.
- Static files use an allowlist and cannot traverse outside the embedded Web
  package.
- Responses set a CSP without inline script/eval, `nosniff`,
  `frame-ancestors 'none'`, `X-Frame-Options: DENY`, a no-referrer policy, and
  restrictive permissions policy.
- Logs and all backend/user values are inserted by the frontend with
  `textContent`; the trusted-LAN/no TLS/no account-authentication warning remains
  visible on desktop and mobile.

## SSE lifecycle

- Each client uses the coordinator's existing fixed-size, drop-oldest
  `EventBroker` queue.
- Streams send an initial state containing the current image generation, state
  changes, and 15-second heartbeats.
- Generator finalization and explicit close both unsubscribe idempotently.
  Focused tests cover state/generation events, heartbeat, slow-queue behavior,
  close-before-iteration, and disconnect cleanup with zero active subscribers.

## Interface and accessibility

- Desktop follows “The Calibrated Control Room”: persistent status strip,
  bounded acquisition and analysis rails, image-majority center viewport, and
  persistent bottom log timeline.
- The UI exposes the real 4096 × 4096, 9 µm, RAW 12/8-bit, bin 1–8, gain
  100–1650, ROI, and cooling capabilities without legacy device assumptions.
- Loading, empty, error, disabled, connected, Preview, exposing, and diagnostic
  states are explicit.
- Native controls have labels, visible focus, keyboard operation, readable
  contrast, and reduced-motion handling.
- Raw-pixel inspection supports pointer input plus Arrow/Shift+Arrow, Home,
  End, Enter, and Space; coordinates and ADU are announced through an
  `aria-live` readout.
- Mobile retains Link, CameraState, RAW mode, CCD temperature, and serial
  identity. Session controls and Abort are sticky and visible before the
  image, while the image remains the dominant first-workflow viewport.

## Visual QA

- Desktop: `test/results/task11-desktop.png`
  - 1920 × 911 viewport, no horizontal overflow.
  - Image center width 1301 px, larger than the 284 px and 320 px rails.
- Mobile: `test/results/task11-mobile.png`
  - 390 × 844 viewport, full-page artifact 375 × 2692, no horizontal overflow.
  - Status bottom 108 px; Session/Abort bottom 279 px and visible in the first
    viewport; image follows immediately.
- Controlled local interactions verified connection/disconnection, Preview
  start/stop, exposure, Abort availability, binning/ROI synchronization,
  acquisition settings, independent cooling, display stretch, statistics,
  raw-pixel pointer and keyboard inspection, and log updates.
- The controlled QA server used only `127.0.0.1`, produced no application
  console errors, and was stopped after verification.

## Impeccable review and detector

- The independent finish review found three material issues: cooling
  round-trip safety, mobile operational-state/Abort visibility, and
  keyboard pixel inspection. All three were corrected and re-verified in the
  browser.
- The mechanical detector was run exactly once. It reported:
  - dynamic image without an initial `src`: corrected with a hidden valid data
    placeholder;
  - width-based progress animation: corrected to transform-only `scaleX`;
  - em-dash overuse advisory: retained because all nine occurrences are compact
    measurement placeholders/ranges, not body-copy cadence.
- `DESIGN.md` now records the implemented tokens, typography, layout,
  components, and responsive rules. `.impeccable/design.json` contains the
  matching extension metadata and rendered component snippets.

## RED evidence

The initial focused run is captured in `test/results/task11-red.txt`:

- 21 tests collected;
- 20 failed because the Web routes, image services, and security boundary were
  absent;
- the existing invalid-path sink already rejected traversal.

Supplemental RED cycles covered missing image dimensions, live cooling
hydration, and the independent cooling mutation before their implementations.

## Verification

- Focused Web suite: 26 passed.
- Full automated suite: 765 passed, 4 real-hardware tests skipped by their
  explicit `TOUPTEK_HARDWARE_TEST=1` gate.
- Python compilation: passed.
- Native ES module syntax checks: passed.
- Sidecar JSON validation: passed.
- `git diff --check`: passed.
- Production Web legacy-copy scan: no matches.

## Handoff

Task 12 can serve the existing `create_app()` normally; Web registration is
already conditional on `config.web.enabled`. A synchronous WSGI deployment
must provision enough worker threads for concurrent long-lived SSE clients and
ordinary Alpaca/Web requests.

Real-camera image appearance, physical cooling behavior, TLS termination,
authentication, and public-Internet deployment remain outside Task 11. The
console deliberately identifies its trusted-LAN boundary instead of implying
those protections.
