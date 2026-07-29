# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Astrophotographers and observatory operators using the ToupTek 4040 camera from a Windows 64-bit workstation. They operate it through ASCOM Alpaca clients such as N.I.N.A. and through a same-origin local Web console for setup, exposure, preview, cooling, image inspection, and diagnosis.

## Product Purpose

Provide a strict ASCOM Camera V4 Alpaca service and a focused local control surface for the 4096 × 4096 ToupTek `sMAX16AM-U3-CL-GPS`. Success means one process safely owns the camera, standards clients can complete the full imaging workflow, and operators can inspect the same immutable scientific image without creating a second SDK owner.

## Positioning

The product unifies standards-compliant Alpaca control, native ToupTek acquisition, and scientific image transport behind one formally coordinated camera state machine. The Web, Alpaca, FITS, preview, and diagnostics paths all consume that same owner and the same immutable image generations.

## Operating Context

- Windows 64-bit acquisition workstation connected to a ToupTek camera over USB.
- Trusted local network operation; the Web console has no public-Internet security model.
- N.I.N.A., ASCOM ConformU, browser-based control, FITS workflows, and hardware diagnosis.
- Long exposures and slow image downloads must not block state queries or create a second camera handle.

## Capabilities and Constraints

- Sensor: 4096 × 4096, 9.0 µm pixels.
- Readout: RAW 12-bit and RAW 8-bit.
- Symmetric binning: 1 through 8.
- Numeric gain: 100 through 1650.
- ROI, cooling, preview, exposure, Abort, ImageBytes, JSON, FITS, discovery, and management.
- Formal exposure and Preview are mutually exclusive.
- Display stretching never changes scientific pixel data.
- All test artifacts live under `test/`.
- The real camera is the default; the simulator is enabled only explicitly.

## Brand Commitments

- Product name: ToupTek 4040 Alpaca Camera.
- Preserve the approved COSMOS66 image-first, three-column workstation concept while replacing all COSMOS66/PICam/8120/HDR assumptions with real ToupTek capabilities and terminology.
- Copy is concise, operational, and diagnostic. It does not make marketing claims.

## Evidence on Hand

- Approved product design: `docs/superpowers/specs/2026-07-28-touptek4040-alpaca-design.md`
- Approved implementation plan: `docs/superpowers/plans/2026-07-28-touptek4040-alpaca.md`
- ToupTek SDK documentation: `toupcamsdk.20250623/doc/en.html`
- Implemented and tested Camera V4, coordinator, simulator, ImageBytes, JSON, FITS, management, and discovery layers.
- No logo, commercial claims, customer claims, or benchmark claims are supplied; future work must not fabricate them.

## Product Principles

1. One camera owner, many safe consumers.
2. Scientific pixels remain immutable and traceable to their acquisition settings.
3. Standards behavior and error semantics are explicit, not guessed.
4. Operational state and recovery actions are visible within seconds.
5. Real-hardware failure never silently falls back to simulation.
