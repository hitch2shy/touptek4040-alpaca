# Task 10 Report: Management, Setup, Discovery, and Protocol Conformance

## Initial implementation

- Added the three official Alpaca Management API endpoints with exact JSON
  envelopes, approved server metadata, one configured Camera device, and a
  stable UUID derived from the configured camera serial.
- Added browser-safe root and Camera setup information pages. Unsupported
  setup paths return a bounded HTML 403 response without exposing configuration
  or hardware details.
- Added shared-port IPv4 Alpaca discovery and optional IPv6 multicast
  discovery. Exact discovery queries receive the compact
  `{"AlpacaPort":11111}` response; malformed, oversized, and hostile datagrams
  are ignored.
- Registered the management and setup routes in the Falcon application and
  retained the protocol error split between HTTP request errors and HTTP 200
  ASCOM device errors.

Initial implementation commit:

`7be985185ef2666df8ea75f1d9afbc043de7fcf4`

Commit subject:

`feat: add Alpaca management and discovery`

## Review fix round 1/5

Five Important findings were closed. The separate listener busy-loop Minor was
intentionally left unchanged and was not added to this task's scope.

### Independent Management query validation

All three Management GET endpoints now parse `ClientID` and
`ClientTransactionID` as independent optional unsigned 32-bit decimal values.
Missing values become zero. Zero and `4294967295` are accepted; empty,
negative, signed, Boolean-like, non-ASCII decimal, fractional, and overflowing
values produce a stable HTTP 400 response. Identical duplicate values are
accepted and conflicting duplicates are rejected.

This parser is isolated from the device API, whose existing transaction-ID
range remains 1 through `4294967295`.

The new Management cases were written before the parser change and initially
failed. After implementation, the combined Management and existing device
request-parsing set passed `107` tests.

### Discovery generation health and IPv6 membership

Each responder run now owns its own generation number, stop event, sockets, and
threads. Start and stop transitions are serialized, while health checks require
the current IPv4 listener to remain alive. An unexpected mandatory IPv4
listener failure atomically retires both address families; a subsequent start
builds a fresh generation. A deterministic overlapping stop/start barrier test
confirms that an old generation cannot clear or leak into the new generation.

IPv6 discovery enumerates all indexes returned by the injectable
`socket.if_nameindex` boundary and joins `ff12::a1:9aca` separately on each
valid index. A failed interface join does not prevent later interfaces from
joining. If every join fails, the IPv6 socket is closed, IPv4 stays healthy,
and the responder emits its one unavailable warning. Scoped IPv6 request
source tuples are passed back unchanged when sending responses.

The focused discovery set now passes `19` tests.

### One package version source

`touptek4040.__version__` is the single runtime source for driver and
manufacturer version metadata. `pyproject.toml` now obtains project metadata
from the same attribute through Setuptools dynamic version configuration.
Focused version, common API, and Management coverage passed `198` tests.

### Safe root setup configuration

The root `/setup` page remains information-only when no explicit
`config_path` exists, and POST is forbidden in that mode. With an explicit
path, it exposes only listen host, server port, and discovery port.

POST accepts URL-encoded form data only. Host and both port values are
strictly validated before any filesystem operation; missing or conflicting
fields return bounded HTML errors without reflecting input. Successful writes
load the existing INI, preserve unrelated sections and fields, update only the
three server settings, write and flush a temporary file in the destination
directory, then replace the destination atomically. A missing source file,
invalid body, or failed replacement cannot create or partially modify the
configuration, and temporary files are removed. The workflow never calls the
camera.

The setup tests were introduced before the POST implementation. The RED result
was `15 failed, 6 passed`, with the expected missing form and HTTP 405
responses. The GREEN result is `21 passed`.

## Round-1 verification

Focused Management, Setup, Discovery, protocol-error, and version set:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/alpaca/test_management.py test/alpaca/test_setup_pages.py test/alpaca/test_discovery.py test/alpaca/test_protocol_errors.py test/unit/test_version_metadata.py -q
```

Result: `134 passed`.

Complete unit and Alpaca suites:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/unit test/alpaca -q
```

Result: `727 passed`.

Complete test tree:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test -q
```

Result: `727 passed, 4 skipped`. The skips remain the existing
`TOUPTEK_HARDWARE_TEST=1` opt-in cases; the environment variable was unset and
no hardware access was attempted.

Compilation and whitespace validation both exited `0`:

```powershell
.\.venv64\Scripts\python.exe -m compileall -q touptek4040 test
git diff --check
```

No listener busy-loop Minor, ledger, approved plan, or approved design file was
modified.

## Fix commit

Fix commit subject:

`fix: harden Alpaca management and discovery`

Fix commit reference: `HEAD`. The exact immutable hash is recorded in the
Task 10 round-1 handoff after commit because a Git commit cannot contain its own
final hash without changing that hash.
