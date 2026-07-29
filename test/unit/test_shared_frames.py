"""Integration tests for real owner-process shared-memory frames."""

from __future__ import annotations

from dataclasses import replace
import json
from multiprocessing import shared_memory
from pathlib import Path
import tracemalloc
import uuid

import numpy as np
import pytest

from touptek4040.owner_protocol import FrameDescriptor, OwnerProtocolError, PROTOCOL_VERSION
from touptek4040.shared_frames import SharedFrameError, SharedFrameRegistry


def _registry(tmp_path: Path) -> SharedFrameRegistry:
    return SharedFrameRegistry(owner_generation=7, registry_path=tmp_path / "frames.json")


def _mapping_exists(name: str) -> bool:
    try:
        mapping = shared_memory.SharedMemory(name=name)
    except FileNotFoundError:
        return False
    else:
        mapping.close()
        return True


def _unlink_if_present(name: str) -> None:
    try:
        mapping = shared_memory.SharedMemory(name=name)
    except FileNotFoundError:
        return
    try:
        mapping.unlink()
    finally:
        mapping.close()


def test_raw8_publish_consume_copies_pixels_makes_them_read_only_and_releases_mapping(tmp_path: Path):
    """Returning a shared, mutable, or unlinked-too-late RAW8 frame is a bug."""
    registry = _registry(tmp_path)
    pixels = np.arange(12, dtype=np.uint8).reshape(3, 4)

    descriptor = registry.publish(pixels, {"pixel_format": "RAW8", "max_adu": 255, "tag": "guide"})
    received = registry.consume(descriptor)

    assert descriptor.name.startswith("touptek4040-g7-r1-")
    assert descriptor.nbytes == 12
    assert np.array_equal(received, pixels)
    assert received is not pixels
    assert received.flags.writeable is False
    assert _mapping_exists(descriptor.name) is False


def test_raw12_round_trip_preserves_uint16_values_and_metadata(tmp_path: Path):
    """Converting RAW12 values or losing their frame metadata is a bug."""
    registry = _registry(tmp_path)
    pixels = np.array([[0, 1, 2048], [4094, 4095, 17]], dtype=np.uint16)

    descriptor = registry.publish(
        pixels,
        {"pixel_format": "RAW12", "max_adu": 4095, "gain": 100, "cooler": None},
    )
    received = registry.consume(descriptor)

    assert descriptor.dtype == "uint16"
    assert descriptor.max_adu == 4095
    assert descriptor.metadata == {"pixel_format": "RAW12", "max_adu": 4095, "gain": 100, "cooler": None}
    assert received.dtype == np.dtype("uint16")
    assert np.array_equal(received, pixels)
    assert received.flags.writeable is False


def test_consume_rejects_a_descriptor_with_wrong_byte_length(tmp_path: Path):
    """Trusting a descriptor whose declared byte length changed is a bug."""
    registry = _registry(tmp_path)
    descriptor = registry.publish(
        np.arange(12, dtype=np.uint8).reshape(3, 4),
        {"pixel_format": "RAW8", "max_adu": 255},
    )

    with pytest.raises(SharedFrameError):
        registry.consume(replace(descriptor, shape=(1, 11), nbytes=11))

    registry.release(descriptor)


def test_consume_rejects_a_descriptor_with_a_wrong_shape_even_when_bytes_match(tmp_path: Path):
    """Reinterpreting bytes as another same-size image shape is a bug."""
    registry = _registry(tmp_path)
    descriptor = registry.publish(
        np.arange(12, dtype=np.uint8).reshape(2, 6),
        {"pixel_format": "RAW8", "max_adu": 255},
    )

    with pytest.raises(SharedFrameError):
        registry.consume(replace(descriptor, shape=(3, 4)))

    registry.release(descriptor)


def test_publish_rejects_unsupported_pixel_dtypes(tmp_path: Path):
    """Publishing float pixels as a camera raw frame is a bug."""
    registry = _registry(tmp_path)

    with pytest.raises(SharedFrameError):
        registry.publish(
            np.zeros((2, 2), dtype=np.float32),
            {"pixel_format": "RAW8", "max_adu": 255},
        )


def test_frame_descriptor_rejects_impossible_dimensions():
    """Accepting zero or overflowing frame dimensions is a bug."""
    with pytest.raises(OwnerProtocolError):
        FrameDescriptor(
            protocol_version=PROTOCOL_VERSION,
            owner_generation=7,
            request_id=1,
            name="touptek4040-g7-r1-impossible",
            shape=(0, 12),
            dtype="uint8",
            nbytes=0,
            max_adu=255,
            metadata={"pixel_format": "RAW8", "max_adu": 255},
        )

    with pytest.raises(OwnerProtocolError):
        FrameDescriptor(
            protocol_version=PROTOCOL_VERSION,
            owner_generation=7,
            request_id=1,
            name="touptek4040-g7-r1-overflow",
            shape=(2**62, 4),
            dtype="uint8",
            nbytes=2**64,
            max_adu=255,
            metadata={"pixel_format": "RAW8", "max_adu": 255},
        )


@pytest.mark.parametrize(
    ("pixels", "metadata"),
    [
        (np.zeros((2, 2), dtype=np.uint8), {"pixel_format": "RAW8", "max_adu": 4095}),
        (np.zeros((2, 2), dtype=np.uint16), {"pixel_format": "RAW12", "max_adu": 4096}),
    ],
)
def test_publish_rejects_metadata_with_a_mismatched_max_adu(
    tmp_path: Path, pixels: np.ndarray, metadata: dict[str, object]
):
    """Letting metadata claim an incompatible raw bit depth is a bug."""
    with pytest.raises(SharedFrameError):
        _registry(tmp_path).publish(pixels, metadata)


def test_owner_crash_before_publication_leaves_no_registry_file(tmp_path: Path):
    """Creating a registry entry before a frame exists would leak after an owner crash."""
    registry_path = tmp_path / "frames.json"
    SharedFrameRegistry(owner_generation=7, registry_path=registry_path)

    assert registry_path.exists() is False


def test_startup_cleans_only_dead_owner_entries_that_the_registry_lists(tmp_path: Path):
    """Cleanup must remove an abandoned listed mapping without touching an unlisted mapping."""
    listed = shared_memory.SharedMemory(name=f"touptek4040-dead-{uuid.uuid4().hex}", create=True, size=8)
    unlisted = shared_memory.SharedMemory(name=f"touptek4040-unlisted-{uuid.uuid4().hex}", create=True, size=8)
    registry_path = tmp_path / "frames.json"
    registry_path.write_text(
        json.dumps({"frames": [{"name": listed.name, "owner_pid": 0}]}), encoding="utf-8"
    )

    try:
        SharedFrameRegistry(owner_generation=8, registry_path=registry_path)

        assert json.loads(registry_path.read_text(encoding="utf-8")) == {"frames": []}
        assert _mapping_exists(unlisted.name) is True
    finally:
        listed.close()
        unlisted.close()
        _unlink_if_present(listed.name)
        _unlink_if_present(unlisted.name)


def test_release_is_idempotent_and_removes_the_real_mapping_and_registry_entry(tmp_path: Path):
    """A second cleanup attempt must be harmless after the exact frame is released."""
    registry = _registry(tmp_path)
    descriptor = registry.publish(
        np.arange(4, dtype=np.uint8).reshape(2, 2),
        {"pixel_format": "RAW8", "max_adu": 255},
    )

    registry.release(descriptor)
    registry.release(descriptor)

    assert _mapping_exists(descriptor.name) is False
    assert json.loads((tmp_path / "frames.json").read_text(encoding="utf-8")) == {"frames": []}


def test_registry_file_is_replaced_when_a_new_frame_is_published(tmp_path: Path):
    """Writing the registry in place could leave a crash-truncated registry file."""
    registry = _registry(tmp_path)
    first = registry.publish(np.zeros((1, 1), dtype=np.uint8), {"pixel_format": "RAW8", "max_adu": 255})
    registry_path = tmp_path / "frames.json"
    first_file_id = registry_path.stat().st_ino

    second = registry.publish(np.ones((1, 1), dtype=np.uint8), {"pixel_format": "RAW8", "max_adu": 255})

    recorded = json.loads(registry_path.read_text(encoding="utf-8"))
    assert registry_path.stat().st_ino != first_file_id
    assert {entry["name"] for entry in recorded["frames"]} == {first.name, second.name}

    registry.release(first)
    registry.release(second)


def test_raw12_4096_square_copy_has_the_exact_size_and_bounded_python_peak_memory(tmp_path: Path):
    """A full-sensor RAW12 copy must stay a 32 MiB frame rather than accidentally expanding."""
    registry = _registry(tmp_path)
    pixels = np.full((4096, 4096), 4095, dtype=np.uint16)
    tracemalloc.start()
    try:
        descriptor = registry.publish(pixels, {"pixel_format": "RAW12", "max_adu": 4095})
        received = registry.consume(descriptor)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    frame_bytes = 4096 * 4096 * 2
    print(f"RAW12 4096x4096 frame_bytes={frame_bytes} traced_peak_bytes={peak}")
    assert descriptor.nbytes == frame_bytes
    assert received.nbytes == frame_bytes
    assert np.array_equal(received, pixels)
    assert peak <= frame_bytes * 4 + 8 * 1024 * 1024
