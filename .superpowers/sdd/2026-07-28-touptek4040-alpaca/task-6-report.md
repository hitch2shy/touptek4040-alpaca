# Task 6 Report: Alpaca Protocol Primitives and Complete API Manifest

## TDD failure evidence

The three Task 6 suites were written before any `touptek4040.alpaca` production
module existed.

Command:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/alpaca/test_responses.py test/alpaca/test_request_parsing.py test/alpaca/test_camera_manifest.py -v
```

Result: pytest exited 1 with `0 collected / 3 collection errors`. Each error
was the intended missing-production failure:

- `ModuleNotFoundError: No module named 'touptek4040.alpaca'` from
  `test_responses.py`;
- the same missing package from `test_request_parsing.py`;
- the same missing package from `test_camera_manifest.py`.

This RED proved that the response, request, and manifest contracts did not
exist before their tests.

During the first implementation run, the focused suite produced
`1 failed, 32 passed`: an explicitly supplied
`ClientTransactionID=None` was incorrectly treated as an omitted field. The
parser was corrected to distinguish an absent key (default `0`) from any
present invalid value (`AlpacaError` number `0x401`). The unchanged regression
test then passed.

## Approved API alignment

The manifest was aligned to the approved design's ASCOM Platform 7 sources:

- ASCOM Alpaca Device API v1 OpenAPI definition:
  `https://ascom-standards.org/api/AlpacaDeviceAPI_v1.yaml`;
- ASCOM Master ICameraV4 Interface:
  `https://ascom-standards.org/newdocs/camera.html`;
- official Platform 7 AlpycaDevice request/response template:
  `https://github.com/ASCOMInitiative/AlpycaDevice`.

The official Alpaca v1 table contains 16 common method-command pairs (15
paths, with both GET and PUT for `connected`) and 72 Camera-specific
method-command pairs, for exactly 88 entries. `imagearray` and
`imagearrayvariant` are classified as `image`; all entries preserve the
official stable path/method order.

## Implementation summary

- Added exact Alpaca success envelopes. Property responses contain `Value`
  followed by `ClientTransactionID`, `ServerTransactionID`, `ErrorNumber`, and
  `ErrorMessage`; method responses contain the four standard non-value fields.
- Added `AlpacaError(number, message)` and the required official errors:
  `0x400`, `0x401`, `0x402`, `0x407`, `0x40B`, `0x40C`, and `0x40E`.
- Added a lock-protected server transaction allocator. It starts at `1`,
  allocates uniquely under concurrent access, returns unsigned 32-bit values,
  and wraps `0xFFFFFFFF` to `1` without emitting `0`.
- Added case-insensitive GET and PUT parameter parsing. PUT merges query fields
  first and form/media fields second, so media explicitly wins on a
  case-insensitive collision. GET ignores media.
- `ClientTransactionID` is parsed case-insensitively from either request type,
  defaults to `0` only when absent, and rejects non-decimal, signed, boolean,
  null, non-scalar, negative, and greater-than-uint32 values with `0x401`.
- Framework query/media access failures and non-mapping media are normalized
  to `AlpacaError(0x401, ...)`; native parser exceptions do not escape.
- Added the immutable, typed, duplicate-free 88-entry
  `CAMERA_V4_ENDPOINTS` manifest with `common`, `camera`, and `image`
  categories.
- No Falcon application, routes, responders, coordinator behavior, or Camera
  business endpoint was added in this task.

## Verification

Focused Task 6 suite:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/alpaca/test_responses.py test/alpaca/test_request_parsing.py test/alpaca/test_camera_manifest.py -v
```

Result: `33 passed`.

Complete unit and Alpaca suites:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit test/alpaca -v
```

Result: `154 passed`.

Complete current test tree:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test -v
```

Result: `154 passed, 4 skipped`. All four skips are the existing
`TOUPTEK_HARDWARE_TEST=1` hardware opt-in cases.

Compilation and whitespace validation:

```powershell
.\.venv64\Scripts\python.exe -m compileall -q touptek4040 test
git diff --check
```

Results: both commands exited `0`.

Scope validation found changes only in:

- `touptek4040/alpaca/`;
- `test/alpaca/`;
- this Task 6 report.

No progress or approved plan/spec file was modified.

## Commit

Commit subject:

`feat: define Alpaca protocol and Camera V4 manifest`

Commit reference: `HEAD`. The exact immutable hash is recorded in the Task 6
handoff immediately after commit because a Git commit cannot contain its own
final hash without changing that hash.
