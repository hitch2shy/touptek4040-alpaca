"""Atomic, owner-scoped shared-memory frame transfer."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import json
from multiprocessing import shared_memory
import os
from pathlib import Path
import secrets
import tempfile
import threading
import re

import numpy as np

from .owner_protocol import (
    MAX_UINT64,
    PRODUCT_SHARED_MEMORY_PREFIX,
    PROTOCOL_VERSION,
    FrameDescriptor,
    JsonValue,
    OwnerProtocolError,
)


class SharedFrameError(ValueError):
    """Raised for unsafe or unavailable owner-frame shared memory."""


_FRAME_NAME = re.compile(
    r"^touptek4040-g(?P<owner_generation>[1-9][0-9]*)-r(?P<request_id>[0-9]+)-(?P<nonce>[0-9a-f]{32})$"
)
_RECORD_FIELDS = {
    "name",
    "owner_pid",
    "protocol_version",
    "owner_generation",
    "request_id",
    "shape",
    "dtype",
    "nbytes",
    "max_adu",
    "metadata",
    "state",
}


def _require_uint64(value: object, field: str, *, allow_zero: bool) -> int:
    if type(value) is not int or value < (0 if allow_zero else 1) or value > MAX_UINT64:
        raise SharedFrameError(f"registry {field} must be an unsigned 64-bit integer")
    return value


@dataclass(frozen=True, slots=True)
class _RegistryRecord:
    descriptor: FrameDescriptor
    owner_pid: int
    state: str

    @classmethod
    def from_payload(cls, payload: object) -> "_RegistryRecord":
        if type(payload) is not dict or set(payload) != _RECORD_FIELDS:
            raise SharedFrameError("shared-frame registry has an invalid frame entry")
        shape = payload["shape"]
        metadata = payload["metadata"]
        if type(shape) is not list or len(shape) != 2:
            raise SharedFrameError("registry shape must be a two-item JSON list")
        if type(metadata) is not dict:
            raise SharedFrameError("registry metadata must be a JSON object")
        for field in ("protocol_version", "owner_generation", "request_id", "nbytes", "max_adu"):
            _require_uint64(payload[field], field, allow_zero=field in {"request_id", "max_adu"})
        _require_uint64(payload["owner_pid"], "owner_pid", allow_zero=False)
        if type(payload["name"]) is not str or type(payload["dtype"]) is not str:
            raise SharedFrameError("registry name and dtype must be strings")
        if type(payload["state"]) is not str or payload["state"] not in {"published", "consumed"}:
            raise SharedFrameError("registry state is invalid")
        if any(type(dimension) is not int for dimension in shape):
            raise SharedFrameError("registry shape dimensions must be integers")
        try:
            descriptor = FrameDescriptor(
                protocol_version=payload["protocol_version"],
                owner_generation=payload["owner_generation"],
                request_id=payload["request_id"],
                name=payload["name"],
                shape=(shape[0], shape[1]),
                dtype=payload["dtype"],
                nbytes=payload["nbytes"],
                max_adu=payload["max_adu"],
                metadata=metadata,
            )
        except OwnerProtocolError as exc:
            raise SharedFrameError("registry descriptor is invalid") from exc
        name_match = _FRAME_NAME.fullmatch(descriptor.name)
        if name_match is None:
            raise SharedFrameError("registry frame name is not application-created")
        if int(name_match["owner_generation"]) != descriptor.owner_generation:
            raise SharedFrameError("registry frame name owner generation is inconsistent")
        if int(name_match["request_id"]) != descriptor.request_id:
            raise SharedFrameError("registry frame name request ID is inconsistent")
        if descriptor.dtype == "uint8":
            expected_format, expected_max_adu = "RAW8", 255
        else:
            expected_format, expected_max_adu = "RAW12", 4095
        if descriptor.max_adu != expected_max_adu or descriptor.metadata.get("pixel_format") != expected_format:
            raise SharedFrameError("registry raw format metadata is inconsistent")
        if descriptor.metadata.get("max_adu") != expected_max_adu:
            raise SharedFrameError("registry max_adu metadata is inconsistent")
        return cls(descriptor, payload["owner_pid"], payload["state"])

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.descriptor.name,
            "owner_pid": self.owner_pid,
            "protocol_version": self.descriptor.protocol_version,
            "owner_generation": self.descriptor.owner_generation,
            "request_id": self.descriptor.request_id,
            "shape": list(self.descriptor.shape),
            "dtype": self.descriptor.dtype,
            "nbytes": self.descriptor.nbytes,
            "max_adu": self.descriptor.max_adu,
            "metadata": dict(self.descriptor.metadata),
            "state": self.state,
        }


class _RegistryMutex:
    """A process-wide lock for one atomically replaced registry file."""

    def __init__(self, registry_path: Path) -> None:
        canonical_path = str(registry_path.resolve(strict=False)).encode("utf-8")
        self._name = f"Local\\touptek4040-registry-{hashlib.sha256(canonical_path).hexdigest()}"
        self._lock_path = Path(f"{registry_path}.lock")
        self._handle: object | None = None
        self._file: object | None = None

    def __enter__(self) -> "_RegistryMutex":
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_mutex = kernel32.CreateMutexW
            create_mutex.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
            create_mutex.restype = wintypes.HANDLE
            wait_for_single_object = kernel32.WaitForSingleObject
            wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            wait_for_single_object.restype = wintypes.DWORD
            handle = create_mutex(None, False, self._name)
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
            result = wait_for_single_object(handle, 0xFFFFFFFF)
            if result not in {0, 0x80}:  # WAIT_OBJECT_0, WAIT_ABANDONED
                kernel32.CloseHandle(handle)
                raise OSError(ctypes.get_last_error(), "WaitForSingleObject failed")
            self._handle = handle
            return self

        import fcntl

        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._lock_path.open("a+b")
        fcntl.flock(self._file.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            if self._handle is not None:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                release_mutex = kernel32.ReleaseMutex
                release_mutex.argtypes = (wintypes.HANDLE,)
                release_mutex.restype = wintypes.BOOL
                close_handle = kernel32.CloseHandle
                close_handle.argtypes = (wintypes.HANDLE,)
                close_handle.restype = wintypes.BOOL
                release_mutex(self._handle)
                close_handle(self._handle)
                self._handle = None
            return

        import fcntl

        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None


class SharedFrameRegistry:
    """Publishes frames once and transfers their ownership to one consumer."""

    def __init__(
        self,
        owner_generation: int,
        registry_path: str | os.PathLike[str] | None = None,
    ) -> None:
        if isinstance(owner_generation, bool) or not isinstance(owner_generation, int) or owner_generation <= 0:
            raise SharedFrameError("owner_generation must be a positive integer")
        if owner_generation > MAX_UINT64:
            raise SharedFrameError("owner_generation exceeds uint64")
        self._owner_generation = owner_generation
        self._registry_path = (
            Path(registry_path)
            if registry_path is not None
            else Path(tempfile.gettempdir()) / "touptek4040-shared-frames.json"
        )
        self._owner_pid = os.getpid()
        self._next_request_id = 1
        self._lock = threading.RLock()
        self._registry_mutex = _RegistryMutex(self._registry_path)
        # Windows removes a named mapping when its last handle closes.  Keep
        # the owner handle until the consumer takes ownership or releases it.
        self._owned_mappings: dict[str, shared_memory.SharedMemory] = {}
        self._cleanup_dead_owner_entries()

    def publish(self, pixels: np.ndarray, metadata: Mapping[str, JsonValue]) -> FrameDescriptor:
        """Copy a supported raw frame into a newly owned shared-memory mapping."""
        if not isinstance(pixels, np.ndarray):
            raise SharedFrameError("pixels must be a numpy array")
        if pixels.ndim != 2 or any(dimension <= 0 for dimension in pixels.shape):
            raise SharedFrameError("pixels must be a non-empty two-dimensional array")
        if pixels.dtype == np.dtype("uint8"):
            expected_format, expected_max_adu = "RAW8", 255
        elif pixels.dtype == np.dtype("uint16"):
            expected_format, expected_max_adu = "RAW12", 4095
        else:
            raise SharedFrameError("only uint8 RAW8 and uint16 RAW12 frames are supported")
        if not isinstance(metadata, Mapping):
            raise SharedFrameError("metadata must be a mapping")
        if metadata.get("pixel_format") != expected_format:
            raise SharedFrameError("metadata pixel_format does not match the pixel dtype")
        max_adu = metadata.get("max_adu")
        if isinstance(max_adu, bool) or not isinstance(max_adu, int) or max_adu != expected_max_adu:
            raise SharedFrameError("metadata max_adu does not match the pixel dtype")
        if pixels.dtype == np.dtype("uint16") and int(pixels.max()) > expected_max_adu:
            raise SharedFrameError("RAW12 pixels exceed max_adu")

        shape = (int(pixels.shape[0]), int(pixels.shape[1]))
        nbytes = int(pixels.nbytes)
        if nbytes <= 0 or nbytes > MAX_UINT64:
            raise SharedFrameError("frame byte length is outside the protocol limit")
        with self._registry_transaction():
            request_id = self._reserve_request_id()
            name = (
                f"{PRODUCT_SHARED_MEMORY_PREFIX}g{self._owner_generation}-r{request_id}-"
                f"{secrets.token_hex(16)}"
            )
            try:
                descriptor = FrameDescriptor(
                    protocol_version=PROTOCOL_VERSION,
                    owner_generation=self._owner_generation,
                    request_id=request_id,
                    name=name,
                    shape=shape,
                    dtype=str(pixels.dtype),
                    nbytes=nbytes,
                    max_adu=max_adu,
                    metadata=metadata,
                )
            except OwnerProtocolError as exc:
                raise SharedFrameError(str(exc)) from exc

            mapping: shared_memory.SharedMemory | None = None
            try:
                mapping = shared_memory.SharedMemory(name=name, create=True, size=nbytes)
                target = np.ndarray(shape, dtype=pixels.dtype, buffer=mapping.buf)
                np.copyto(target, pixels)
                records = self._read_records()
                records.append(_RegistryRecord(descriptor, self._owner_pid, "published"))
                self._replace_records(records)
            except Exception:
                if mapping is not None:
                    self._close_and_unlink(mapping)
                raise
            else:
                self._owned_mappings[name] = mapping
                return descriptor

    def consume(self, descriptor: FrameDescriptor) -> np.ndarray:
        """Copy one exactly-described frame, then close and unlink its mapping."""
        with self._registry_transaction():
            descriptor = self._validated_descriptor(descriptor)
            records = self._read_records()
            record = next((item for item in records if item.descriptor.name == descriptor.name), None)
            if record is None or record.descriptor != descriptor:
                raise SharedFrameError("frame descriptor is not the published descriptor")
            if record.state != "published":
                raise SharedFrameError("frame has already been consumed")
            try:
                mapping = shared_memory.SharedMemory(name=descriptor.name)
            except FileNotFoundError as exc:
                raise SharedFrameError("published frame mapping is unavailable") from exc
            try:
                # Windows reports the page-rounded capacity after reopening a
                # named mapping.  The descriptor and registry record enforce
                # the exact pixel byte count; the operating-system mapping
                # must at least hold that many bytes.
                if mapping.size < descriptor.nbytes:
                    raise SharedFrameError("shared-memory byte length is shorter than descriptor")
                pixels = np.ndarray(descriptor.shape, dtype=np.dtype(descriptor.dtype), buffer=mapping.buf).copy()
                pixels.setflags(write=False)
                mapping.unlink()
            finally:
                mapping.close()
            self._replace_records([replace(item, state="consumed") if item == record else item for item in records])
            return pixels

    def release(self, descriptor: FrameDescriptor) -> None:
        """Release a published mapping exactly once; repeated releases are harmless."""
        with self._registry_transaction():
            descriptor = self._validated_descriptor(descriptor)
            records = self._read_records()
            record = next((item for item in records if item.descriptor.name == descriptor.name), None)
            if record is None:
                return
            if record.descriptor != descriptor:
                raise SharedFrameError("frame descriptor is not the published descriptor")
            if record.owner_pid != self._owner_pid:
                raise SharedFrameError("only the publishing owner can release a frame")
            mapping = self._owned_mappings.pop(descriptor.name, None)
            if mapping is None:
                raise SharedFrameError("publishing owner handle is unavailable")
            self._close_and_unlink(mapping)
            self._replace_records([item for item in records if item != record])

    def _reserve_request_id(self) -> int:
        if self._next_request_id > MAX_UINT64:
            raise SharedFrameError("shared-frame request IDs are exhausted")
        request_id = self._next_request_id
        self._next_request_id += 1
        return request_id

    @contextmanager
    def _registry_transaction(self):
        with self._lock:
            with self._registry_mutex:
                yield

    def _validated_descriptor(self, descriptor: FrameDescriptor) -> FrameDescriptor:
        if not isinstance(descriptor, FrameDescriptor):
            raise SharedFrameError("descriptor must be a FrameDescriptor")
        if descriptor.owner_generation != self._owner_generation:
            raise SharedFrameError("descriptor belongs to a stale owner generation")
        if not descriptor.name.startswith(PRODUCT_SHARED_MEMORY_PREFIX):
            raise SharedFrameError("descriptor shared-memory name is not product-prefixed")
        return descriptor

    @staticmethod
    def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        decoded: dict[str, object] = {}
        for key, value in pairs:
            if key in decoded:
                raise SharedFrameError(f"duplicate shared-frame registry key {key!r}")
            decoded[key] = value
        return decoded

    def _read_records(self) -> list[_RegistryRecord]:
        try:
            raw = self._registry_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        try:
            document = json.loads(raw, object_pairs_hook=self._reject_duplicate_json_keys)
        except (json.JSONDecodeError, SharedFrameError) as exc:
            raise SharedFrameError("shared-frame registry is invalid JSON") from exc
        if type(document) is not dict or set(document) != {"frames"} or type(document["frames"]) is not list:
            raise SharedFrameError("shared-frame registry has an invalid schema")
        return [_RegistryRecord.from_payload(record) for record in document["frames"]]

    def _replace_records(self, records: list[_RegistryRecord]) -> None:
        self._registry_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._registry_path.name}.", suffix=".tmp", dir=self._registry_path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as temporary:
                json.dump(
                    {"frames": [record.to_payload() for record in records]},
                    temporary,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, self._registry_path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    def _cleanup_dead_owner_entries(self) -> None:
        with self._registry_transaction():
            records = self._read_records()
            remaining: list[_RegistryRecord] = []
            changed = False
            for record in records:
                if not self._pid_is_dead(record.owner_pid):
                    remaining.append(record)
                    continue
                self._unlink_named_mapping(record.descriptor.name)
                changed = True
            if changed:
                self._replace_records(remaining)

    @staticmethod
    def _pid_is_dead(owner_pid: int) -> bool:
        if owner_pid <= 0:
            return True
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            open_process.restype = wintypes.HANDLE
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL
            get_exit_code_process = kernel32.GetExitCodeProcess
            get_exit_code_process.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            get_exit_code_process.restype = wintypes.BOOL
            ctypes.set_last_error(0)
            handle = open_process(process_query_limited_information, False, owner_pid)
            if handle:
                try:
                    exit_code = wintypes.DWORD()
                    if not get_exit_code_process(handle, ctypes.byref(exit_code)):
                        return False
                    return exit_code.value != 259  # STILL_ACTIVE
                finally:
                    close_handle(handle)
            return ctypes.get_last_error() in {87, 1168}
        try:
            os.kill(owner_pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False

    @staticmethod
    def _close_and_unlink(mapping: shared_memory.SharedMemory) -> None:
        try:
            mapping.unlink()
        except FileNotFoundError:
            pass
        finally:
            mapping.close()

    @staticmethod
    def _unlink_named_mapping(name: str) -> None:
        try:
            mapping = shared_memory.SharedMemory(name=name)
        except FileNotFoundError:
            return
        try:
            mapping.unlink()
        except FileNotFoundError:
            pass
        finally:
            mapping.close()
