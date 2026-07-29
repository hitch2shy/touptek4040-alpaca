# Hardware root-cause summary

## Repeated-trigger timeout

The original implementation started, triggered, and stopped the camera for every
frame. Real hardware testing showed that this lifecycle is unreliable when
settings change or a new trigger follows a completed trigger immediately.

The SDK sample keeps Pull mode running across synchronous triggers. The product
now follows that lifecycle for structurally identical captures, reopens the
handle only for structural changes, and gives the completed-frame pipeline a
measured 15 ms recovery interval before reusing the stream. On the approved
camera, delays below 10 ms failed consistently, 10 ms was borderline, and 15 ms
passed across 1 ms, 50 ms, and 100 ms exposures. The final 40-case hardware
matrix passed with exact setting restoration.

## ASCOM full-frame bin 3

ASCOM defines the legal full-frame output as `CameraXSize // BinX`, which is
`4096 // 3 = 1365`. ToupTek SDK 20250623 documents that digital-binning output
dimensions are rounded down to an even number, so native bin 3 can only produce
1364 pixels from a 4096-pixel axis.

Only when a valid ASCOM ROI cannot be represented by the SDK's native
even-origin, even-size, minimum-16 ROI rules, the implementation captures the
smallest covering unbinned padded interval, crops the requested physical sensor
range, then performs saturating-sum software binning. Regular representable
requests retain hardware binning. The 4096-square RAW12 fallback uses a 32 MiB
SDK buffer, a 1365-square `uint64` accumulator (about 14.2 MiB), and a
1365-square final `uint16` array (about 3.6 MiB); it does not create an unbinned
copy or full-size accumulator.

Real RAW12 and RAW8 tests both returned exactly 1365 by 1365 pixels. Fresh
ConformU Camera V4 also completed the bin-3 full-frame exposure and ImageArray
retrieval without errors.
