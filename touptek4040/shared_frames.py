"""Atomic, owner-scoped shared-memory frame transfer."""

from __future__ import annotations

from collections.abc import Mapping
import json
from multiprocessing import shared_memory
import os
from pathlib import Path
import secrets
import tempfile
import threading

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
        with self._lock:
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
                records.append(self._record_for(descriptor))
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
        with self._lock:
            descriptor = self._validated_descriptor(descriptor)
            records = self._read_records()
            record = next((item for item in records if item.get("name") == descriptor.name), None)
            if record is None or not self._record_matches(record, descriptor):
                raise SharedFrameError("frame descriptor is not the published descriptor")
            try:
                mapping = shared_memory.SharedMemory(name=descriptor.name)
            except FileNotFoundError as exc:
                self._replace_records([item for item in records if item.get("name") != descriptor.name])
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
                self._close_owned_mapping(descriptor.name)
            self._replace_records([item for item in records if item.get("name") != descriptor.name])
            return pixels

    def release(self, descriptor: FrameDescriptor) -> None:
        """Release a published mapping exactly once; repeated releases are harmless."""
        with self._lock:
            descriptor = self._validated_descriptor(descriptor)
            records = self._read_records()
            record = next((item for item in records if item.get("name") == descriptor.name), None)
            if record is None:
                return
            if not self._record_matches(record, descriptor):
                raise SharedFrameError("frame descriptor is not the published descriptor")
            self._unlink_named_mapping(descriptor.name)
            self._close_owned_mapping(descriptor.name)
            self._replace_records([item for item in records if item.get("name") != descriptor.name])

    def _reserve_request_id(self) -> int:
        if self._next_request_id > MAX_UINT64:
            raise SharedFrameError("shared-frame request IDs are exhausted")
        request_id = self._next_request_id
        self._next_request_id += 1
        return request_id

    def _validated_descriptor(self, descriptor: FrameDescriptor) -> FrameDescriptor:
        if not isinstance(descriptor, FrameDescriptor):
            raise SharedFrameError("descriptor must be a FrameDescriptor")
        if descriptor.owner_generation != self._owner_generation:
            raise SharedFrameError("descriptor belongs to a stale owner generation")
        if not descriptor.name.startswith(PRODUCT_SHARED_MEMORY_PREFIX):
            raise SharedFrameError("descriptor shared-memory name is not product-prefixed")
        return descriptor

    def _record_for(self, descriptor: FrameDescriptor) -> dict[str, object]:
        return {
            "name": descriptor.name,
            "owner_pid": self._owner_pid,
            "protocol_version": descriptor.protocol_version,
            "owner_generation": descriptor.owner_generation,
            "request_id": descriptor.request_id,
            "shape": list(descriptor.shape),
            "dtype": descriptor.dtype,
            "nbytes": descriptor.nbytes,
            "max_adu": descriptor.max_adu,
            "metadata": dict(descriptor.metadata),
        }

    def _record_matches(self, record: Mapping[str, object], descriptor: FrameDescriptor) -> bool:
        return record == self._record_for(descriptor)

    def _read_records(self) -> list[dict[str, object]]:
        try:
            raw = self._registry_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SharedFrameError("shared-frame registry is invalid JSON") from exc
        if not isinstance(document, dict) or set(document) != {"frames"} or not isinstance(document["frames"], list):
            raise SharedFrameError("shared-frame registry has an invalid schema")
        records: list[dict[str, object]] = []
        for record in document["frames"]:
            if not isinstance(record, dict):
                raise SharedFrameError("shared-frame registry has an invalid frame entry")
            name = record.get("name")
            if not isinstance(name, str) or not name.startswith(PRODUCT_SHARED_MEMORY_PREFIX):
                raise SharedFrameError("shared-frame registry contains a non-product mapping")
            records.append(record)
        return records

    def _replace_records(self, records: list[dict[str, object]]) -> None:
        self._registry_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._registry_path.name}.", suffix=".tmp", dir=self._registry_path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as temporary:
                json.dump({"frames": records}, temporary, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
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
        with self._lock:
            records = self._read_records()
            remaining: list[dict[str, object]] = []
            changed = False
            for record in records:
                owner_pid = record.get("owner_pid")
                if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or not self._pid_is_dead(owner_pid):
                    remaining.append(record)
                    continue
                self._unlink_named_mapping(str(record["name"]))
                changed = True
            if changed:
                self._replace_records(remaining)

    @staticmethod
    def _pid_is_dead(owner_pid: int) -> bool:
        if owner_pid <= 0:
            return True
        if os.name == "nt":
            import ctypes

            process_query_limited_information = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, owner_pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return False
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

    def _close_owned_mapping(self, name: str) -> None:
        mapping = self._owned_mappings.pop(name, None)
        if mapping is not None:
            mapping.close()
