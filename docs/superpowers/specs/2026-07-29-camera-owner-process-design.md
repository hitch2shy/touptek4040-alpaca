# Camera Owner Process Design

## Status and decision

The real `sMAX16AM-U3-CL-GPS` camera, serial
`TP241025143218C92B01151AEC40CB7`, has demonstrated two independent native
SDK failure modes:

- repeated structural `Stop` / `Close` / `Open` can block forever inside
  `Toupcam.Open`;
- keeping one handle and using `Stop` / reconfigure / `Start` can still lose
  the first software trigger after a structural change, even with a measured
  15 ms recovery interval.

Python cannot safely terminate a thread blocked inside the vendor DLL. The
approved design therefore moves all real-camera SDK ownership into a child
process. The Alpaca, Web, GUI, discovery, logging and `CameraCoordinator`
interfaces remain in the main process.

The simulator remains in-process.

## Goals

- A blocked vendor call must not block the Alpaca/Web process indefinitely.
- Only one process may load the SDK and own the real camera handle.
- The existing synchronous camera contract remains the boundary consumed by
  `CameraCoordinator`.
- A timed-out exposure must fail exactly once and must never be automatically
  retried.
- Large 4096 x 4096 frames must not travel through pickled IPC messages.
- Owner crashes, timeouts and shared-memory leaks must be recoverable and
  observable.
- The packaged executables must start the owner correctly under Windows
  `spawn` and PyInstaller.
- Runtime configuration is supplied primarily by `touptek4040.json`; legacy
  INI files remain readable for compatibility.

## Non-goals

- No network-accessible camera-owner protocol.
- No automatic retry of exposure, Abort, setting mutations or other commands
  whose native side effects may already have occurred.
- No changes to ASCOM Camera V4 member names, Alpaca routes, Web endpoints or
  the GUI workflow.
- No process isolation for `SimCamera`.
- No general-purpose distributed worker framework.

## Considered approaches

### Selected: `multiprocessing.Connection` plus shared memory

Control requests and small responses use a private inherited
`multiprocessing.Connection` that is never exposed as a network listener.
Pixel buffers use
`multiprocessing.shared_memory.SharedMemory`. This is available in the
supported Python runtime, works with Windows `spawn`, avoids an extra local
port and keeps 32 MiB RAW12 frames out of pickle payloads.

### Rejected: local TCP or named-pipe protocol

This provides a more language-neutral boundary, but requires additional
framing, authentication, endpoint discovery and cleanup. No second-language
owner is required.

### Rejected: one temporary process per exposure

This maximizes isolation but repeatedly initializes the SDK and camera, loses
cooler/preview continuity and cannot efficiently support preview.

## Components and responsibilities

### `CameraOwnerProxy`

`CameraOwnerProxy` implements the existing camera contract in the main process.
It:

- starts and supervises one owner process;
- serializes one normal command at a time;
- assigns monotonically increasing unsigned request IDs and owner generations;
- validates response request ID, generation, operation and payload schema;
- applies operation-specific hard timeouts;
- maps structured owner errors to existing `CameraError` subclasses;
- validates and copies shared-memory frames into immutable `Frame` objects;
- acknowledges and unlinks frame segments;
- terminates the complete owner process after timeout or protocol corruption;
- automatically creates a new idle owner after failure, while remaining
  disconnected until the client explicitly reconnects.

It never loads `toupcam.py` or `toupcam.dll`.

### Owner process

The owner process is the only process that loads the SDK. It:

- performs raw `EnumV2` gating before opening a handle;
- opens only `sn:TP241025143218C92B01151AEC40CB7`;
- owns `ToupTekCamera`, its SDK handle and all native callbacks;
- executes validated commands sequentially;
- maintains the connection-time hardware snapshot used for normal disconnect
  restoration;
- writes completed image arrays to uniquely named shared-memory segments;
- reports structured state, results and errors;
- services Abort through a separate control connection;
- cleans all owned shared-memory segments and the camera handle in `finally`.

The owner has no HTTP server, user interface or configuration parser.

### Owner launcher and frozen entry point

An explicit top-level owner entry function is importable under Windows
`spawn`. Frozen executables call `multiprocessing.freeze_support()`. The
PyInstaller specification includes the owner module, SDK Python wrapper and
only the x64 DLL.

### Shared-memory registry

The main process records each segment in an atomically replaced registry file
under the configured runtime/log directory using:

- product prefix;
- owner PID and owner generation;
- request ID;
- random nonce;
- size and creation time;
- acknowledged/released state.

The owner unlinks segments that were never published. The proxy unlinks a
published segment after validation and copy. At startup, the proxy reads its
registry and removes only product-prefixed segments whose recorded owner PID
is no longer alive; it never attempts to enumerate or delete arbitrary
shared-memory names.

## IPC protocol

Messages are versioned dictionaries containing only JSON-like primitive
values. Pickle is an implementation transport and is not treated as a trusted
schema.

Every normal request contains:

```text
protocol_version
owner_generation
request_id
operation
arguments
```

Every response contains the same version, generation, request ID and
operation, plus exactly one of:

```text
result
error = {type, message, hresult?, diagnostics?}
```

Frame results contain metadata rather than pixels:

```text
shared_memory_name
byte_length
shape
dtype
readout_mode
max_adu
exposure_started_utc
exposure_duration_s
gain
binning
roi
```

The proxy rejects unknown fields, wrong types, stale generations, duplicate
responses, impossible shapes, byte-length mismatches and unsupported dtypes.
A protocol violation terminates the owner.

## Command and concurrency model

- One normal request may be outstanding.
- Abort uses a separate connection so it can reach an owner whose command loop
  is waiting in an interruptible acquisition.
- The owner sends a command-start acknowledgement before entering a native
  operation. This is diagnostic only and never authorizes an automatic retry.
- `CameraCoordinator` remains the authority for formal exposure, preview,
  settings and connection state.
- Preview and formal exposure continue to be mutually exclusive through the
  coordinator.
- A proxy generation changes whenever an owner is replaced. Responses or
  shared-memory frames from an older generation are discarded.

## Timeout defaults

All values are finite and configurable:

| Operation | Default |
|---|---:|
| Owner startup and handshake | 10 seconds |
| Connect / native Open | 20 seconds |
| Ordinary read or setting mutation | 5 seconds |
| Exposure | requested duration x 1.02 + 10 seconds |
| Abort | 2 seconds |
| Disconnect, restoration and close | 10 seconds |

The exposure timeout is calculated after dark/bias requests are clamped to the
hardware minimum. Configuration validation rejects non-finite, boolean, zero
or negative timeout values.

## Failure semantics

### Timeout or hung native call

The proxy:

1. marks the current request failed;
2. terminates the complete owner process;
3. cleans registered shared memory;
4. returns a stable `CameraTimeoutError` or mapped driver error;
5. transitions the coordinator to error/disconnected state;
6. starts a fresh idle owner for a later explicit Connect.

The timed-out command is never retried. In particular, an exposure is never
repeated and no image from that generation can be published.

### Owner crash or malformed response

The behavior is the same as a timeout, except the error identifies owner exit
or IPC corruption. Exit codes and the last bounded owner diagnostics are
logged.

### Abort

If native Abort completes within two seconds, normal acquisition cancellation
semantics apply. If Abort or the main native call does not respond, the proxy
terminates the owner and reports the exposure as aborted/failed without an
image. The client must reconnect.

### Disconnect

The owner stops acquisition, restores the connection-time hardware snapshot
and closes the handle. If this does not finish within ten seconds, the proxy
terminates the owner and records that restoration could not be verified. It
must not claim successful restoration.

### Main-process shutdown

Shutdown first rejects new work, then requests owner disconnect. After the
finite disconnect deadline it terminates the owner, releases IPC objects and
cleans registered shared memory. No background thread is allowed to retain a
camera handle.

## JSON configuration

`touptek4040.json` is the primary distributed configuration. A representative
shape is:

```json
{
  "server": {
    "host": "0.0.0.0",
    "port": 11111,
    "discovery_port": 32227
  },
  "camera": {
    "source": "toupcam",
    "serial": "TP241025143218C92B01151AEC40CB7",
    "readout_mode": "RAW 12-bit",
    "gain": null
  },
  "owner": {
    "startup_timeout_s": 10.0,
    "connect_timeout_s": 20.0,
    "command_timeout_s": 5.0,
    "exposure_overhead_s": 10.0,
    "exposure_multiplier": 1.02,
    "abort_timeout_s": 2.0,
    "disconnect_timeout_s": 10.0
  },
  "web": {
    "enabled": true
  },
  "logging": {
    "level": "INFO",
    "directory": "logs"
  },
  "security": {
    "trusted_lan_only": true
  }
}
```

Unknown sections or keys, duplicate JSON keys, non-finite numbers and wrong
types are rejected. Relative paths resolve against the JSON file directory.
Command-line overrides remain highest priority.

The loader continues to read legacy `.ini` files when explicitly selected.
The release contains the JSON example, not an INI example. Setup writes use
the JSON format and preserve the existing atomic/stable-file-identity
guarantees.

## Test strategy

### Unit tests

- strict JSON and legacy INI loading, precedence and timeout validation;
- request/response schema, IDs, generations and error mapping;
- timeout termination with no automatic retry;
- stale and duplicate response rejection;
- shared-memory dimension, dtype, range and byte-length validation;
- shared-memory ACK, owner crash cleanup and stale-segment cleanup;
- dark/bias exposure timeout calculation;
- explicit reconnect requirement after owner replacement.

### Process integration tests

A simulated owner SDK runs in a real spawned process. Tests inject hangs in
Open, setting mutation, Trigger, image transfer and Close. Each test proves:

- the main process responds within the configured deadline;
- the exact owner process exits;
- the current request fails once;
- no exposure is automatically repeated;
- a new owner has a new generation and remains disconnected;
- shared memory is reclaimed.

### Existing contract regression

All coordinator, Alpaca Camera V4, Web, ImageBytes, JSON, FITS, GUI, discovery
and simulator tests remain applicable. The real-camera proxy must be
substitutable for the current camera contract.

### Real hardware

Testing proceeds in bounded stages:

1. unique read-only gate and identity;
2. connect, one exposure, disconnect and verified restoration;
3. RAW8/RAW12, bins 1-8, ROI and software-bin fallback;
4. Abort and reconnect;
5. repeated structural transitions, with injected owner termination if a
   native call exceeds its deadline;
6. cooler, target, fan and gain persistence;
7. final snapshot equality and process/shared-memory leak audit.

No real-camera stage starts unless the previous stage restored and closed the
camera.

### ConformU and packaging

Each ConformU profile receives a fresh owner generation and restoration
record. Missing, stale or unexplained findings fail validation. Packaged smoke
tests exercise both executables, Windows spawn, shared memory, simulator mode
and one bounded real-camera round trip.

## Acceptance criteria

- A deliberately hung fake native Open or Trigger cannot hang the main
  service beyond its configured timeout plus process-termination allowance.
- The timed-out exposure executes once and publishes no frame.
- The replacement owner uses a new generation and requires explicit Connect.
- A 4096 x 4096 RAW12 frame crosses the process boundary through shared
  memory, validates successfully and matches JSON/ImageBytes/FITS/Web pixels.
- Owner termination leaves no registered shared-memory segment or live child.
- Real hardware completes the approved matrix and is restored exactly.
- Fresh ConformU output contains zero unexplained findings.
- Packaged applications use `touptek4040.json`; legacy INI remains readable
  only for compatibility.
