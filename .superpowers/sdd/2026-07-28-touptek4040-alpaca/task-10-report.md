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

## Review fix round 2/5

The two remaining Important findings were closed. The listener busy-loop Minor
remains intentionally unchanged.

### Bounded IPv6 interface degradation logging

IPv6 multicast membership still uses the injectable interface enumeration
boundary, validates and deduplicates indexes, attempts every supplied
applicable index, preserves all successful memberships, and replies to scoped
IPv6 source tuples unchanged.

Individual join failures no longer emit warnings. When at least one interface
joins, failures are summarized at debug level and IPv6 remains active. When
every supplied interface fails, the IPv6 socket closes, IPv4 remains active,
and the responder's existing lifecycle warning gate emits at most one
actionable degradation warning across every later stop and restart.

A 54-interface fake failure probe repeated across three complete starts
initially observed `163` warnings. The round-2 implementation emits exactly
one. A partial failure with another successful membership emits no warning.
A real Windows socket integration test binds both families to loopback only,
performs no discovery send, repeats the responder lifecycle, and verifies at
most one warning.

### Resolved setup write target

An explicit `config_path` enables the setup form only when it resolves at
application construction to an existing regular file. Every GET confirms that
the path still resolves to that same approved target. Missing paths,
directories, broken or non-file symlink targets, and resolution failures render
the root page read-only without a form.

POST checks the same resolved target before parsing or writing, again at the
write boundary, and once more immediately before atomic replacement. If the
path disappears or resolves elsewhere, the request receives the stable HTML
403 unavailable response. No file is created, no temporary file remains, and
the condition is never reported as an internal 500. Successful writes and
genuine replacement failures retain the round-1 atomic-write behavior.

The round-2 RED focused result was `7 failed, 39 passed`: two warning-bound
failures, two read-only GET failures, and three unavailable-target POST
failures. The focused Discovery and Setup GREEN result is `46 passed`.

## Round-2 verification

Focused Management, Setup, Discovery, protocol-error, and version set:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test/alpaca/test_management.py test/alpaca/test_setup_pages.py test/alpaca/test_discovery.py test/alpaca/test_protocol_errors.py test/unit/test_version_metadata.py -q
```

Result: `140 passed`.

Complete test tree:

```powershell
.\.venv64\Scripts\python.exe -m pytest -c test/pytest.ini test -q
```

Result: `733 passed, 4 skipped`. The skips remain the existing hardware opt-in
tests; no hardware access was attempted.

Compilation and whitespace validation both exited `0`:

```powershell
.\.venv64\Scripts\python.exe -m compileall -q touptek4040 test
git diff --check
```

No listener busy-loop Minor, ledger, approved plan, or approved design file was
modified.

Round-2 fix commit subject:

`fix: bound discovery warnings and setup writes`

Round-2 commit reference: `HEAD`; its exact immutable hash is recorded in the
handoff after commit.
